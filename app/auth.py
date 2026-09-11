"""Local accounts: argon2 hashes, opaque session tokens stored server-side.
Cookie carries only the random token; nothing to forge, nothing to decode.
"""
import secrets
import sqlite3

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import db as dbm

ph = PasswordHasher()
_DUMMY_HASH = ph.hash("timing-equalization-placeholder")
_login_fails: dict[str, list[float]] = {}
LOGIN_WINDOW, LOGIN_MAX = 300, 10   # max 10 failures per IP per 5 min
SESSION_DAYS = 90
COOKIE = "fv_session"


def create_user(db, username, password, role="member", can_upload=True, can_delete_any=False):
    db.execute(
        "INSERT INTO users(username, pw_hash, role, can_upload, can_delete_any, created) "
        "VALUES(?,?,?,?,?,?)",
        (username.strip(), ph.hash(password), role, int(can_upload), int(can_delete_any), dbm.now()))
    db.commit()


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


def set_cookie(resp, token):
    resp.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", path="/")


def user_from_request(request: Request, db: sqlite3.Connection):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token=? AND s.expires > ? AND u.disabled=0",
        (token, dbm.now())).fetchone()
    return row


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


def can_delete(user, photo) -> bool:
    if user["role"] == "admin" or user["can_delete_any"]:
        return True
    return photo["uploaded_by"] == user["id"] and photo["source"] == "upload"
