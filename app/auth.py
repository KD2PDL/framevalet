"""Local accounts: argon2 hashes, opaque session tokens stored server-side.
Cookie carries only the random token; nothing to forge, nothing to decode.
"""
import secrets
import sqlite3

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import cloudflare, config, db as dbm

ph = PasswordHasher()
_DUMMY_HASH = ph.hash("timing-equalization-placeholder")
_login_fails: dict[str, list[float]] = {}
LOGIN_WINDOW, LOGIN_MAX = 300, 10   # max 10 failures per IP per 5 min
SESSION_DAYS = 90
COOKIE = "fv_session"


# Granular permissions. 'admin' role implies all of them plus managing roles.
PERMS = {
    "upload":     "Upload photos",
    "delete_any": "Delete anyone's photos",
    "tvs":        "Manage TVs (pairing, art mode, schedules, import)",
    "users":      "Manage users",
    "settings":   "Settings, remote access, maintenance",
    "logs":       "View logs",
}
ADMIN_PERMS = ("tvs", "users", "settings", "logs")   # any of these opens the Admin/TVs nav


def perms_of(user) -> set:
    if user is None:
        return set()
    if user["role"] == "admin":
        return set(PERMS)
    return {p for p in (user["perms"] or "").split(",") if p}


def can(user, perm: str) -> bool:
    return perm in perms_of(user)


def set_perms(db, uid: int, perms) -> None:
    perms = [p for p in PERMS if p in set(perms)]
    db.execute("UPDATE users SET perms=?, can_upload=?, can_delete_any=? WHERE id=?",
               (",".join(perms), int("upload" in perms), int("delete_any" in perms), uid))
    db.commit()


def create_user(db, username, password, role="member", can_upload=True, can_delete_any=False,
                email=None, sso=False, perms=None):
    if perms is None:
        perms = (["upload"] if can_upload else []) + (["delete_any"] if can_delete_any else [])
    perms = [p for p in PERMS if p in set(perms)]
    db.execute(
        "INSERT INTO users(username, pw_hash, role, can_upload, can_delete_any, email, sso, perms, created) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (username.strip(), ph.hash(password), role, int("upload" in perms), int("delete_any" in perms),
         email, int(sso), ",".join(perms), dbm.now()))
    db.commit()


def sso_user(db, claims: dict):
    """Map verified Access claims to a user row, provisioning on first sight.
    Browser logins carry `email`; service tokens carry `common_name` (the
    token's Client ID) and must match an existing local username."""
    email = (claims.get("email") or "").strip().lower()
    if not email:
        cn = (claims.get("common_name") or "").strip()
        return db.execute("SELECT * FROM users WHERE username=?", (cn,)).fetchone() if cn else None
    row = db.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
    if row or config.get(db, "cf_access_autoprovision") != "true":
        return row
    username = email.split("@", 1)[0]
    if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        username = email                      # local part taken by someone else: no merge
    role = "admin" if config.get(db, "cf_access_default_role") == "admin" else "member"
    create_user(db, username, secrets.token_urlsafe(32), role=role, email=email, sso=True)
    cloudflare.log.info("provisioned %s user %s for %s", role, username, email)
    return db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def sso_claims(request, db) -> dict | None:
    """Verified Access claims for this request, or None."""
    token = request.headers.get("cf-access-jwt-assertion")
    if token and cloudflare.access_enabled(db):
        return cloudflare.verify(db, token)
    return None


def rate_limited(ip: str) -> bool:
    import time
    now = time.time()
    _login_fails[ip] = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
    return len(_login_fails[ip]) >= LOGIN_MAX


def note_login_failure(ip: str):
    import time
    _login_fails.setdefault(ip, []).append(time.time())


def check_login(db, username, password):
    row = db.execute("SELECT * FROM users WHERE username=? AND disabled=0",
                     (username.strip(),)).fetchone()
    if not row:
        try:  # equalize timing so a missing user isn't faster than a wrong password
            ph.verify(_DUMMY_HASH, password)
        except VerifyMismatchError:
            pass
        return None
    try:
        ph.verify(row["pw_hash"], password)
    except VerifyMismatchError:
        return None
    if ph.check_needs_rehash(row["pw_hash"]):
        db.execute("UPDATE users SET pw_hash=? WHERE id=?", (ph.hash(password), row["id"]))
        db.commit()
    return row


def start_session(db, user_id) -> str:
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions(token, user_id, expires) VALUES(?,?,?)",
               (token, user_id, dbm.now() + SESSION_DAYS * 86400))
    db.execute("DELETE FROM sessions WHERE expires < ?", (dbm.now(),))
    db.commit()
    return token


def end_session(db, token):
    db.execute("DELETE FROM sessions WHERE token=?", (token,))
    db.commit()


def set_cookie(resp, token, request=None):
    # Behind the tunnel uvicorn already rewrote scheme/client from the
    # X-Forwarded-* headers cloudflared sends from loopback, so "https" here is
    # trustworthy and the cookie gets the Secure flag without extra config.
    secure = config.COOKIE_SECURE or (request is not None and request.url.scheme == "https")
    resp.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400,
                    httponly=True, secure=secure, samesite="lax", path="/")


def user_from_request(request, db: sqlite3.Connection):
    """Session cookie first (local login). Otherwise a verified Cloudflare
    Access JWT, checked on every request: no app session is minted for SSO, so
    removing someone in Cloudflare locks them out immediately and Log out
    can't be undone by the next request's header. Works for WebSockets too."""
    token = request.cookies.get(COOKIE)
    if token:
        row = db.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token=? AND s.expires > ? AND u.disabled=0",
            (token, dbm.now())).fetchone()
        if row:
            return row
    claims = sso_claims(request, db)
    if claims:
        row = sso_user(db, claims)
        if row and not row["disabled"]:
            request.state.sso = True
            return row
    return None


def current_user(request: Request, db=Depends(dbm.get_db)):
    user = user_from_request(request, db)
    if not user:
        # Browser page loads bounce to login; API calls get a clean 401.
        if request.headers.get("accept", "").startswith("application/json") \
           or request.url.path.startswith("/api/"):
            raise HTTPException(401, "not signed in")
        raise HTTPException(307, headers={"Location": "/login"})
    return user


def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "admin only")
    return user


def require(perm: str):
    """Dependency factory: the signed-in user must hold `perm` (admins hold all)."""
    def dep(user=Depends(current_user)):
        if not can(user, perm):
            raise HTTPException(403, f"needs permission: {PERMS.get(perm, perm)}")
        return user
    return dep


def require_any_admin(user=Depends(current_user)):
    if not (perms_of(user) & set(ADMIN_PERMS)):
        raise HTTPException(403, "no administrative permissions")
    return user


def can_delete(user, photo) -> bool:
    if can(user, "delete_any"):
        return True
    return photo["uploaded_by"] == user["id"] and photo["source"] == "upload"
