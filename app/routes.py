"""All routes: pages render Jinja templates; actions are POSTs that redirect.
Photo grid does light fetch() calls for status refresh, nothing SPA-shaped.
"""
import contextlib
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, config, db as dbm, pipeline, worker
from .tvservice import TVService, TVError, doctor as run_doctor, SLIDESHOW_PRESETS

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def render(request, db, name, user=None, **ctx):
    brand_logo = config.get(db, "brand_logo")
    brand_name = config.get(db, "brand_name")
    if brand_logo:
        logo_url = f"/branding/{brand_logo}"
    elif brand_name == "framevalet":
        logo_url = "/static/logo.png"   # stock wordmark; custom brand names get text
    else:
        logo_url = None
    ctx.update(
        request=request, user=user,
        brand={"name": brand_name,
               "accent": config.get(db, "brand_accent"),
               "logo_url": logo_url},
        tv=dict(worker.status),
    )
    return templates.TemplateResponse(request, name, ctx)


def _svc(db) -> TVService:
    return TVService(config.get(db, "tv_host"), config.get(db, "tv_client_name"))


# ---------------------------------------------------------------- setup/login
@router.get("/setup")
def setup_page(request: Request, db=Depends(dbm.get_db)):
    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return RedirectResponse("/", 303)
    return render(request, db, "setup.html", error=None)


@router.post("/setup")
def setup_post(request: Request, username: str = Form(...), password: str = Form(...),
               db=Depends(dbm.get_db)):
    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        raise HTTPException(403)
    if len(password) < 8:
        return render(request, db, "setup.html", error="Password must be 8+ characters")
    auth.create_user(db, username, password, role="admin", can_delete_any=True)
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    resp = RedirectResponse("/doctor", 303)
    auth.set_cookie(resp, auth.start_session(db, user["id"]))
    return resp


@router.get("/login")
def login_page(request: Request, db=Depends(dbm.get_db)):
    if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return RedirectResponse("/setup", 303)
    return render(request, db, "login.html", error=None)


@router.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...),
               db=Depends(dbm.get_db)):
    user = auth.check_login(db, username, password)
    if not user:
        return render(request, db, "login.html", error="Wrong username or password")
    resp = RedirectResponse("/", 303)
    auth.set_cookie(resp, auth.start_session(db, user["id"]))
    return resp


@router.post("/logout")
def logout(request: Request, db=Depends(dbm.get_db)):
    token = request.cookies.get(auth.COOKIE)
    if token:
        auth.end_session(db, token)
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ------------------------------------------------------------------- library
@router.get("/")
def home(request: Request, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    photos = db.execute(
        "SELECT p.*, u.username AS uploader FROM photos p "
        "LEFT JOIN users u ON u.id=p.uploaded_by "
        "WHERE p.status != 'removed' ORDER BY p.taken_date DESC, p.id DESC").fetchall()
    items = [dict(p) | {"can_delete": auth.can_delete(user, p)} for p in photos]
    counts = {s: db.execute("SELECT COUNT(*) c FROM photos WHERE status=?", (s,)).fetchone()["c"]
              for s in ("queued", "processing", "on_tv", "failed")}
    return render(request, db, "grid.html", user, photos=items, counts=counts,
                  import_state=dict(worker.import_state))


@router.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...),
                 db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    if not user["can_upload"]:
        raise HTTPException(403, "uploads not allowed for this account")
    results = []
    for f in files:
        data = await f.read()
        name = Path(f.filename or "photo").name
        if Path(name).suffix.lower() not in pipeline.ACCEPTED:
            results.append({"file": name, "ok": False, "error": "unsupported format"})
            continue
        pid = secrets.token_hex(8)
        proc = config.PROCESSED_DIR / f"{pid}.jpg"
        thumb = config.THUMBS_DIR / f"{pid}.jpg"
        try:
            meta = pipeline.process(data, proc, thumb,
                                    int(config.get(db, "jpeg_quality")))
        except pipeline.PipelineError as e:
            results.append({"file": name, "ok": False, "error": str(e)})
            continue
        dup = db.execute("SELECT id FROM photos WHERE sha256=? AND status!='removed'",
                         (meta["sha256"],)).fetchone()
        if dup:
            proc.unlink(missing_ok=True); thumb.unlink(missing_ok=True)
            results.append({"file": name, "ok": False, "error": "duplicate photo"})
            continue
        orig = None
        if config.get(db, "keep_originals") == "true":
            orig = config.ORIGINALS_DIR / f"{pid}{Path(name).suffix.lower()}"
            orig.write_bytes(data)
        db.execute(
            "INSERT INTO photos(filename, sha256, orig_path, proc_path, thumb_path, "
            "width, height, bytes, taken_date, matte, source, uploaded_by, status, created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, meta["sha256"], str(orig) if orig else None, str(proc), str(thumb),
             meta["width"], meta["height"], meta["bytes"], meta["taken_date"],
             config.get(db, "default_matte"), "upload", user["id"], "queued", dbm.now()))
        db.commit()
        results.append({"file": name, "ok": True})
    worker.kick()
    return JSONResponse({"results": results})


@router.post("/photo/{pid}/delete")
def delete_photo(pid: int, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    p = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404)
    if not auth.can_delete(user, p):
        raise HTTPException(403, "you can only delete your own uploads")
    if p["tv_content_id"]:
        try:
            svc = _svc(db)
            svc.delete(p["tv_content_id"])
            svc.reset()
        except TVError as e:
            raise HTTPException(502, f"TV delete failed: {e}") from e
    for key in ("orig_path", "proc_path", "thumb_path"):
        if p[key]:
            with contextlib.suppress(OSError):
                Path(p[key]).unlink()
    db.execute("DELETE FROM photos WHERE id=?", (pid,))
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/photo/{pid}/display")
def display_photo(pid: int, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    p = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not p or not p["tv_content_id"]:
        raise HTTPException(404, "photo is not on the TV")
    try:
        svc = _svc(db)
        svc.select(p["tv_content_id"])
        svc.reset()
    except TVError as e:
        raise HTTPException(502, str(e)) from e
    return JSONResponse({"ok": True})


@router.get("/thumbs/{pid}.jpg")
def thumb(pid: int, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    p = db.execute("SELECT thumb_path FROM photos WHERE id=?", (pid,)).fetchone()
    if not p or not p["thumb_path"] or not Path(p["thumb_path"]).is_file():
        raise HTTPException(404)
    return FileResponse(p["thumb_path"], media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/photo/{pid}/full")
def full(pid: int, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    p = db.execute("SELECT proc_path FROM photos WHERE id=?", (pid,)).fetchone()
    if not p or not p["proc_path"] or not Path(p["proc_path"]).is_file():
        raise HTTPException(404)
    return FileResponse(p["proc_path"], media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/branding/{name}")
def branding(name: str):
    f = (config.BRAND_DIR / Path(name).name)
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


@router.get("/api/status")
def api_status(db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    counts = {s: db.execute("SELECT COUNT(*) c FROM photos WHERE status=?", (s,)).fetchone()["c"]
              for s in ("queued", "processing", "on_tv", "failed")}
    return {"tv": dict(worker.status), "counts": counts,
            "import": dict(worker.import_state)}


# -------------------------------------------------------------------- doctor
@router.get("/doctor")
def doctor_page(request: Request, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    steps = run_doctor(config.get(db, "tv_host"), config.get(db, "tv_client_name"))
    return render(request, db, "doctor.html", user, steps=steps,
                  host=config.get(db, "tv_host"),
                  paired=_svc(db).has_token())


@router.post("/doctor/pair")
def pair(db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    svc = _svc(db)
    try:
        svc.pair()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


# --------------------------------------------------------------------- admin
@router.get("/admin")
def admin_page(request: Request, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    users = db.execute("SELECT * FROM users ORDER BY created").fetchall()
    slideshow = None
    if _svc(db).has_token() and worker.status["tv_ok"]:
        with contextlib.suppress(TVError, Exception):
            svc = _svc(db)
            slideshow = svc.slideshow()
            svc.reset()
    return render(request, db, "admin.html", user, users=[dict(u) for u in users],
                  settings=config.all_settings(db), slideshow=slideshow,
                  presets=SLIDESHOW_PRESETS,
                  export_env=_export_env(db))


def _export_env(db) -> str:
    token = ""
    with contextlib.suppress(OSError):
        token = config.TOKEN_PATH.read_text().strip()
    return "\n".join([
        f"TV_HOST={config.get(db, 'tv_host')}",
        f"TV_CLIENT_NAME={config.get(db, 'tv_client_name')}",
        f"TV_TOKEN={token}",
    ])


@router.post("/admin/settings")
def save_settings(request: Request, db=Depends(dbm.get_db),
                  user=Depends(auth.require_admin),
                  key: str = Form(...), value: str = Form("")):
    if key not in config.SETTINGS:
        raise HTTPException(400, "unknown setting")
    try:
        config.set(db, key, value.strip())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return RedirectResponse("/admin", 303)


@router.post("/admin/branding-logo")
async def branding_logo(file: UploadFile = File(...), db=Depends(dbm.get_db),
                        user=Depends(auth.require_admin)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".png", ".svg", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, "logo must be png/svg/jpg/webp")
    name = f"logo{ext}"
    (config.BRAND_DIR / name).write_bytes(await file.read())
    config.set(db, "brand_logo", name)
    return RedirectResponse("/admin", 303)


@router.post("/admin/users")
def add_user(username: str = Form(...), password: str = Form(...),
             role: str = Form("member"), can_upload: bool = Form(False),
             can_delete_any: bool = Form(False),
             db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    if len(password) < 8:
        raise HTTPException(400, "password must be 8+ characters")
    try:
        auth.create_user(db, username, password, role="admin" if role == "admin" else "member",
                         can_upload=can_upload, can_delete_any=can_delete_any)
    except Exception as e:
        raise HTTPException(400, f"could not create user: {e}") from e
    return RedirectResponse("/admin", 303)


@router.post("/admin/users/{uid}")
def edit_user(uid: int, action: str = Form(...), password: str = Form(""),
              db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    target = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        raise HTTPException(404)
    admins = db.execute("SELECT COUNT(*) c FROM users WHERE role='admin' AND disabled=0"
                        ).fetchone()["c"]
    if action == "toggle_disabled":
        if target["role"] == "admin" and admins <= 1 and not target["disabled"]:
            raise HTTPException(400, "cannot disable the last admin")
        db.execute("UPDATE users SET disabled=1-disabled WHERE id=?", (uid,))
    elif action == "toggle_upload":
        db.execute("UPDATE users SET can_upload=1-can_upload WHERE id=?", (uid,))
    elif action == "toggle_delete_any":
        db.execute("UPDATE users SET can_delete_any=1-can_delete_any WHERE id=?", (uid,))
    elif action == "set_password":
        if len(password) < 8:
            raise HTTPException(400, "password must be 8+ characters")
        db.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.ph.hash(password), uid))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    elif action == "delete":
        if target["role"] == "admin" and admins <= 1:
            raise HTTPException(400, "cannot delete the last admin")
        db.execute("DELETE FROM users WHERE id=?", (uid,))
    else:
        raise HTTPException(400, "unknown action")
    db.commit()
    return RedirectResponse("/admin", 303)


@router.post("/admin/slideshow")
def set_slideshow(minutes: int = Form(...), shuffle: bool = Form(False),
                  db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    try:
        svc = _svc(db)
        svc.set_slideshow(minutes, shuffle)
        svc.reset()
    except (TVError, ValueError) as e:
        raise HTTPException(502, str(e)) from e
    return RedirectResponse("/admin", 303)


@router.post("/admin/import-tv")
def import_tv(db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    if worker.import_state["running"]:
        raise HTTPException(409, "import already running")
    worker.start_import()
    return RedirectResponse("/", 303)
