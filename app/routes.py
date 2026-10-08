"""All routes: pages render Jinja templates; actions are POSTs (form or JSON).
"""
import contextlib
import json
import json as _json
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, cloudflare, config, db as dbm, discovery, pipeline, worker
from .tvservice import (TVService, TVError, doctor as run_doctor,
                        SLIDESHOW_PRESETS, MOTION_TIMER_VALUES,
                        MOTION_SENSITIVITY, MATTE_TYPES, MATTE_COLORS)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def render_page(request, db, name, user=None, **ctx):
    brand_logo = config.get(db, "brand_logo")
    brand_name = config.get(db, "brand_name")
    if brand_logo:
        logo_url = f"/branding/{brand_logo}"
    elif brand_name == "framevalet":
        logo_url = "/static/logo.png"   # stock wordmark; custom brand names get text
    else:
        logo_url = None
    tvs = [dict(t) | {"status": worker.tv_status(t["id"])}
           for t in db.execute("SELECT * FROM tvs ORDER BY id").fetchall()] if user else []
    static = Path(__file__).parent / "static"
    try:  # mtime-based cache busting: any asset edit invalidates browser caches
        asset_v = int(max((static / f).stat().st_mtime for f in ("app.js", "style.css")))
    except OSError:
        asset_v = 0
    ctx.update(
        request=request, user=user, tvs=tvs, asset_v=asset_v,
        brand={"name": brand_name, "accent": config.get(db, "brand_accent"),
               "logo_url": logo_url},
    )
    return templates.TemplateResponse(request, name, ctx)


def _tv(db, tv_id: int):
    tv = db.execute("SELECT * FROM tvs WHERE id=?", (tv_id,)).fetchone()
    if not tv:
        raise HTTPException(404, "no such TV")
    return tv


# ---------------------------------------------------------------- setup/login
@router.get("/setup")
def setup_page(request: Request, db=Depends(dbm.get_db)):
    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return RedirectResponse("/", 303)
    return render_page(request, db, "setup.html", error=None)


@router.post("/setup")
def setup_post(request: Request, username: str = Form(...), password: str = Form(...),
               db=Depends(dbm.get_db)):
    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        raise HTTPException(403)
    if len(password) < 8:
        return render_page(request, db, "setup.html", error="Password must be 8+ characters")
    auth.create_user(db, username, password, role="admin", can_delete_any=True)
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    resp = RedirectResponse("/tvs", 303)
    auth.set_cookie(resp, auth.start_session(db, user["id"]), request)
    return resp


@router.get("/login")
def login_page(request: Request, db=Depends(dbm.get_db)):
    if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return RedirectResponse("/setup", 303)
    # Came through Cloudflare Access but no account matched (auto-provision off
    # or the account is disabled): say so instead of inviting a password guess.
    claims = auth.sso_claims(request, db)
    sso_email = (claims or {}).get("email") or (claims or {}).get("common_name") or ""
    return render_page(request, db, "login.html", error=None, sso_email=sso_email)


@router.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...),
               db=Depends(dbm.get_db)):
    ip = request.client.host if request.client else "?"
    if auth.rate_limited(ip):
        return render_page(request, db, "login.html",
                           error="Too many attempts. Wait a few minutes and try again.")
    user = auth.check_login(db, username, password)
    if not user:
        auth.note_login_failure(ip)
        return render_page(request, db, "login.html", error="Wrong username or password")
    resp = RedirectResponse("/", 303)
    auth.set_cookie(resp, auth.start_session(db, user["id"]), request)
    return resp


@router.post("/logout")
def logout(request: Request, db=Depends(dbm.get_db)):
    token = request.cookies.get(auth.COOKIE)
    if token:
        auth.end_session(db, token)
    # Through Access the identity lives in Cloudflare's cookie, not ours, so
    # hand off to its logout endpoint on this hostname or they're back in at once.
    dest = "/cdn-cgi/access/logout" if auth.sso_claims(request, db) else "/login"
    resp = RedirectResponse(dest, 303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ------------------------------------------------------------------- library
def _photo_dicts(db, user):
    photos = db.execute(
        "SELECT p.*, u.username AS uploader FROM photos p "
        "LEFT JOIN users u ON u.id=p.uploaded_by "
        "ORDER BY p.taken_date DESC, p.id DESC").fetchall()
    tvstates = {}
    for r in db.execute("SELECT tv_id, photo_id, status, error FROM tv_photos"):
        tvstates.setdefault(r["photo_id"], {})[r["tv_id"]] = \
            {"status": r["status"], "error": r["error"]}
    tagmap = {}
    for r in db.execute("SELECT pt.photo_id, t.name FROM photo_tags pt "
                        "JOIN tags t ON t.id=pt.tag_id"):
        tagmap.setdefault(r["photo_id"], []).append(r["name"])
    out = []
    for p in photos:
        aspect = None
        if p["width"] and p["height"]:
            w, h = p["width"], p["height"]
            if p["edits"]:
                try:
                    c = _json.loads(p["edits"]).get("crop")
                    if c:
                        w, h = c[2] * w, c[3] * h
                except Exception:
                    pass
            if w and h:
                aspect = round(w / h, 4)
        out.append(dict(p) | {
            "tv_states": tvstates.get(p["id"], {}),
            "tags": tagmap.get(p["id"], []),
            "can_delete": auth.can_delete(user, p),
            "croppable": bool(p["orig_path"]),
            "aspect": aspect,
        })
    return out


@router.get("/")
def home(request: Request, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    all_tags = [r["name"] for r in db.execute("SELECT name FROM tags ORDER BY name")]
    return render_page(request, db, "grid.html", user,
                       photos=_photo_dicts(db, user), all_tags=all_tags,
                       matte_types=MATTE_TYPES, matte_colors=MATTE_COLORS,
                       import_state=dict(worker.import_state))


@router.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...),
                 db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    if not user["can_upload"]:
        raise HTTPException(403, "uploads not allowed for this account")
    if len(files) > 200:
        raise HTTPException(413, "too many files in one upload (max 200)")
    import asyncio
    results = []
    for f in files:
        data = await f.read(80_000_001)
        if len(data) > 80_000_000:          # 80 MB per photo is generous for 4K
            results.append({"file": (f.filename or "photo")[:80], "ok": False,
                            "error": "file too large (max 80 MB)"})
            continue
        name = Path(f.filename or "photo").name
        if Path(name).suffix.lower() not in pipeline.ACCEPTED:
            results.append({"file": name, "ok": False, "error": "unsupported format"})
            continue
        try:
            pid = await asyncio.to_thread(worker.ingest_bytes, db, data,
                                          filename=name, user_id=user["id"])
        except pipeline.PipelineError as e:
            results.append({"file": name, "ok": False, "error": str(e)})
            continue
        if pid is None:
            results.append({"file": name, "ok": False, "error": "duplicate photo"})
            continue
        results.append({"file": name, "ok": True, "id": pid})
    worker.kick()
    return JSONResponse({"results": results})


@router.post("/photo/{pid}/delete")
def delete_photo(pid: int, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    p = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404)
    if not auth.can_delete(user, p):
        raise HTTPException(403, "you can only delete your own uploads")
    worker.delete_photo_everywhere(db, p)
    return JSONResponse({"ok": True})


@router.post("/photo/{pid}/favorite")
def favorite(pid: int, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    db.execute("UPDATE photos SET favorite=1-favorite WHERE id=?", (pid,))
    db.commit()
    row = db.execute("SELECT favorite FROM photos WHERE id=?", (pid,)).fetchone()
    return JSONResponse({"ok": True, "favorite": bool(row and row["favorite"])})


@router.post("/photo/{pid}/tags")
def set_tags(pid: int, body: dict = Body(...), db=Depends(dbm.get_db),
                   user=Depends(auth.current_user)):
    names = [t.strip() for t in body.get("tags", []) if t.strip()][:20]
    db.execute("DELETE FROM photo_tags WHERE photo_id=?", (pid,))
    for n in names:
        db.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (n,))
        tid = db.execute("SELECT id FROM tags WHERE name=?", (n,)).fetchone()["id"]
        db.execute("INSERT OR IGNORE INTO photo_tags(photo_id, tag_id) VALUES(?,?)",
                   (pid, tid))
    db.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM photo_tags)")
    db.commit()
    return JSONResponse({"ok": True, "tags": names})


@router.post("/photo/{pid}/crop")
def save_crop(pid: int, body: dict = Body(...), db=Depends(dbm.get_db),
                    user=Depends(auth.current_user)):
    """Body: {"crop": [x,y,w,h] normalized 0..1} or {"crop": null} to clear.
    The push loop notices the stale render_key and re-pushes automatically."""
    p = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404)
    if not p["orig_path"]:
        raise HTTPException(400, "no original stored for this photo; crop unavailable")
    crop = body.get("crop")
    if crop is not None:
        if (not isinstance(crop, list) or len(crop) != 4
                or not all(isinstance(v, (int, float)) for v in crop)):
            raise HTTPException(400, "crop must be [x,y,w,h]")
        x, y, w, h = crop
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x and 0 < h <= 1 - y):
            raise HTTPException(400, "crop out of bounds")
        edits = json.dumps({"crop": [round(v, 5) for v in crop]})
    else:
        edits = None
    db.execute("UPDATE photos SET edits=? WHERE id=?", (edits, pid))
    db.commit()
    worker.kick()
    return JSONResponse({"ok": True})


@router.post("/photo/{pid}/style")
def set_style(pid: int, style: str = Form(...), db=Depends(dbm.get_db),
              user=Depends(auth.current_user)):
    if style not in ("fit", "blurfill"):
        raise HTTPException(400, "style must be fit or blurfill")
    db.execute("UPDATE photos SET style=? WHERE id=?", (style, pid))
    db.commit()
    worker.kick()
    return JSONResponse({"ok": True})



@router.post("/photo/{pid}/matte")
def set_matte(pid: int, body: dict = Body(...), db=Depends(dbm.get_db),
                    user=Depends(auth.current_user)):
    """Body: {"matte": "type_color"} or {"matte": null} for TV default.
    Applied live via change_matte where the photo is already on a TV; queued
    copies pick it up at push time."""
    matte = body.get("matte")
    if matte is not None and not isinstance(matte, str):
        raise HTTPException(400, "matte must be a string or null")
    if matte:
        try:
            mtype, mcolor = matte.split("_", 1)
        except ValueError:
            mtype, mcolor = matte, ""
        if mtype not in MATTE_TYPES or (mtype != "none" and mcolor not in
                                        [c for c, _ in MATTE_COLORS]):
            raise HTTPException(400, "unknown matte")
    p = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404)
    db.execute("UPDATE photos SET matte=? WHERE id=?", (matte, pid))
    live, deferred = 0, 0
    for tp in db.execute(
            "SELECT tp.*, t.* FROM tv_photos tp JOIN tvs t ON t.id=tp.tv_id "
            "WHERE tp.photo_id=? AND tp.status='on_tv' AND tp.content_id IS NOT NULL",
            (pid,)).fetchall():
        svc = TVService(tp)
        applied_live = False
        if svc.port_open():
            try:
                svc.change_matte(tp["content_id"], matte or tp["default_matte"], attempts=1)
                db.execute("UPDATE tv_photos SET matte=? WHERE tv_id=? AND photo_id=?",
                           (matte or tp["default_matte"], tp["tv_id"], pid))
                applied_live = True
            except TVError:
                pass
        svc.reset()
        if applied_live:
            live += 1
        else:
            # TV off right now: stale the render so the worker re-pushes with it
            db.execute("UPDATE tv_photos SET render_key='' WHERE tv_id=? AND photo_id=?",
                       (tp["tv_id"], pid))
            deferred += 1
    db.commit()
    worker.kick()
    return JSONResponse({"ok": True, "live": live, "deferred": deferred})


@router.post("/photo/{pid}/meta")
def set_meta(pid: int, body: dict = Body(...), db=Depends(dbm.get_db),
                   user=Depends(auth.current_user)):
    """Body: {"filename"?: str, "taken_date"?: "YYYY-MM-DD[THH:MM]" or ""}.
    A date change forces a re-push (the TV stores the date at upload time)."""
    p = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404)
    if "filename" in body:
        name = str(body["filename"]).strip()[:120]
        if not name:
            raise HTTPException(400, "title cannot be empty")
        db.execute("UPDATE photos SET filename=? WHERE id=?", (name, pid))
    if "taken_date" in body:
        raw = str(body["taken_date"] or "").strip()
        if raw:
            from datetime import datetime
            parsed = None
            for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y:%m:%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    pass
            if not parsed:
                raise HTTPException(400, "date must be YYYY-MM-DD or YYYY-MM-DDTHH:MM")
            tvdate = parsed.strftime("%Y:%m:%d %H:%M:%S")
        else:
            tvdate = None
        if tvdate != p["taken_date"]:
            db.execute("UPDATE photos SET taken_date=? WHERE id=?", (tvdate, pid))
            if p["orig_path"]:   # re-push only possible when we hold the original
                db.execute("UPDATE tv_photos SET render_key='' "
                           "WHERE photo_id=? AND status='on_tv'", (pid,))
    db.commit()
    worker.kick()
    return JSONResponse({"ok": True})


@router.post("/photo/{pid}/display/{tv_id}")
def display_photo(pid: int, tv_id: int, db=Depends(dbm.get_db),
                  user=Depends(auth.current_user)):
    tp = db.execute("SELECT content_id FROM tv_photos WHERE tv_id=? AND photo_id=? "
                    "AND status='on_tv'", (tv_id, pid)).fetchone()
    if not tp or not tp["content_id"]:
        raise HTTPException(404, "photo is not on that TV")
    svc = TVService(_tv(db, tv_id))
    try:
        if not svc.port_open():
            raise HTTPException(502, "TV is unreachable right now")
        svc.select(tp["content_id"], attempts=1)
    except TVError as e:
        raise HTTPException(502, str(e)) from e
    finally:
        svc.reset()
    return JSONResponse({"ok": True})


@router.post("/photos/bulk")
def bulk(body: dict = Body(...), db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    """Body: {"ids":[...], "action": "...", "param": ...}"""
    ids = [int(i) for i in body.get("ids", [])][:2000]
    action = body.get("action")
    param = body.get("param")
    done = 0
    for pid in ids:
        p = db.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
        if not p:
            continue
        if action == "delete":
            if auth.can_delete(user, p):
                worker.delete_photo_everywhere(db, p)
                done += 1
        elif action == "favorite":
            db.execute("UPDATE photos SET favorite=1 WHERE id=?", (pid,)); done += 1
        elif action == "unfavorite":
            db.execute("UPDATE photos SET favorite=0 WHERE id=?", (pid,)); done += 1
        elif action == "tag" and param:
            db.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (param,))
            tid = db.execute("SELECT id FROM tags WHERE name=?", (param,)).fetchone()["id"]
            db.execute("INSERT OR IGNORE INTO photo_tags(photo_id, tag_id) VALUES(?,?)",
                       (pid, tid)); done += 1
        elif action == "matte":
            matte = param or None
            db.execute("UPDATE photos SET matte=? WHERE id=?", (matte, pid))
            for tp in db.execute(
                    "SELECT tp.*, t.* FROM tv_photos tp JOIN tvs t ON t.id=tp.tv_id "
                    "WHERE tp.photo_id=? AND tp.status='on_tv' AND tp.content_id IS NOT NULL",
                    (pid,)).fetchall():
                svc = TVService(tp)
                if svc.port_open():
                    try:
                        svc.change_matte(tp["content_id"], matte or tp["default_matte"],
                                         attempts=1)
                        db.execute("UPDATE tv_photos SET matte=? WHERE tv_id=? AND photo_id=?",
                                   (matte or tp["default_matte"], tp["tv_id"], pid))
                    except TVError:
                        db.execute("UPDATE tv_photos SET render_key='' "
                                   "WHERE tv_id=? AND photo_id=?", (tp["tv_id"], pid))
                else:
                    db.execute("UPDATE tv_photos SET render_key='' "
                               "WHERE tv_id=? AND photo_id=?", (tp["tv_id"], pid))
                svc.reset()
            done += 1
        elif action == "untag" and param:
            db.execute("DELETE FROM photo_tags WHERE photo_id=? AND tag_id="
                       "(SELECT id FROM tags WHERE name=?)", (pid, param)); done += 1
        elif action == "send_tv" and param:
            if p["orig_path"]:
                worker.assign_photo(db, pid, [int(param)]); done += 1
        elif action == "remove_tv" and param:
            tp = db.execute("SELECT * FROM tv_photos WHERE tv_id=? AND photo_id=?",
                            (int(param), pid)).fetchone()
            if tp:
                if tp["content_id"]:
                    svc = TVService(_tv(db, int(param)))
                    removed = False
                    if svc.port_open():
                        with contextlib.suppress(TVError):
                            svc.delete(tp["content_id"], attempts=1)
                            removed = True
                    svc.reset()
                    if not removed:
                        db.execute("INSERT OR IGNORE INTO pending_tv_deletes"
                                   "(tv_id, content_id) VALUES(?,?)",
                                   (int(param), tp["content_id"]))
                db.execute("DELETE FROM tv_photos WHERE tv_id=? AND photo_id=?",
                           (int(param), pid)); done += 1
    db.commit()
    worker.kick()
    return JSONResponse({"ok": True, "done": done})


# ------------------------------------------------------------------ serving
@router.get("/thumbs/{pid}.jpg")
def thumb(pid: int, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    p = db.execute("SELECT sha256, thumb_path, orig_path, edits FROM photos WHERE id=?",
                   (pid,)).fetchone()
    if not p:
        raise HTTPException(404)
    if p["edits"] and p["orig_path"] and Path(p["orig_path"]).is_file():
        key = pipeline.preview_key(p["sha256"], p["edits"])
        out = config.THUMBS_DIR / f"crop_{key}.jpg"
        if not out.exists():
            pipeline.render_preview(Path(p["orig_path"]), out, p["edits"], longest=480)
        return FileResponse(out, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})
    if not p["thumb_path"] or not Path(p["thumb_path"]).is_file():
        raise HTTPException(404)
    return FileResponse(p["thumb_path"], media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/photo/{pid}/preview")
def preview(pid: int, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    """The photo with its crop applied, sized for the editor. Cached by edits."""
    p = db.execute("SELECT sha256, orig_path, edits FROM photos WHERE id=?",
                   (pid,)).fetchone()
    if not p or not p["orig_path"] or not Path(p["orig_path"]).is_file():
        raise HTTPException(404)
    if not p["edits"]:
        return FileResponse(p["orig_path"], media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})
    key = pipeline.preview_key(p["sha256"], p["edits"])
    out = config.RENDERS_DIR / f"preview_{key}.jpg"
    if not out.exists():
        pipeline.render_preview(Path(p["orig_path"]), out, p["edits"])
    return FileResponse(out, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/photo/{pid}/original")
def original(pid: int, db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    p = db.execute("SELECT orig_path FROM photos WHERE id=?", (pid,)).fetchone()
    if not p or not p["orig_path"] or not Path(p["orig_path"]).is_file():
        raise HTTPException(404)
    return FileResponse(p["orig_path"], media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/branding/{name}")
def branding(name: str):
    f = (config.BRAND_DIR / Path(name).name)
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


@router.get("/api/status")
def api_status(db=Depends(dbm.get_db), user=Depends(auth.current_user)):
    counts = {s: db.execute("SELECT COUNT(DISTINCT photo_id) c FROM tv_photos "
                            "WHERE status=?", (s,)).fetchone()["c"]
              for s in ("queued", "on_tv", "failed")}
    return {"tvs": {t["id"]: worker.tv_status(t["id"])
                    for t in db.execute("SELECT id FROM tvs")},
            "counts": counts, "import": dict(worker.import_state),
            "watch": status_watch(db)}


def status_watch(db):
    return {"watch": dict(worker.status["watch"]),
            "rclone": dict(worker.status["rclone"])}


# ------------------------------------------------------------------ TV pages
@router.get("/tvs")
def tvs_page(request: Request, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    details = []
    for t in db.execute("SELECT * FROM tvs ORDER BY id").fetchall():
        svc = TVService(t)
        details.append(dict(t) | {
            "status": worker.tv_status(t["id"]),
            "paired": svc.has_token(),
            "photo_count": db.execute(
                "SELECT COUNT(*) c FROM tv_photos WHERE tv_id=? AND status='on_tv'",
                (t["id"],)).fetchone()["c"],
            "schedules": [dict(s) for s in db.execute(
                "SELECT * FROM schedules WHERE tv_id=?", (t["id"],))],
        })
    return render_page(request, db, "tvs.html", user, tv_details=details,
                       slideshow_presets=SLIDESHOW_PRESETS,
                       motion_timer_values=MOTION_TIMER_VALUES,
                       motion_sensitivity=MOTION_SENSITIVITY,
                       import_state=dict(worker.import_state))


@router.get("/tvs/discover")
async def discover_tvs(db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    """SSDP scan (~3s). Returns Samsung TVs found on the LAN, Frames first.
    Inside Docker bridge networking this finds nothing; add by IP instead."""
    import asyncio as _aio
    found = await _aio.to_thread(discovery.scan)
    known = {t["host"] for t in db.execute("SELECT host FROM tvs")}
    for f in found:
        f["already_added"] = f["host"] in known
    return JSONResponse({"results": found})


@router.post("/tvs")
def add_tv(name: str = Form(...), host: str = Form(...), mac: str = Form(""),
           client_name: str = Form("framevalet"),
           db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    db.execute("INSERT INTO tvs(name, host, mac, client_name, created) VALUES(?,?,?,?,?)",
               (name.strip(), host.strip(), mac.strip(), client_name.strip(), dbm.now()))
    db.commit()
    return RedirectResponse("/tvs", 303)


@router.post("/tvs/{tv_id}/edit")
def edit_tv(tv_id: int, name: str = Form(...), host: str = Form(...),
            mac: str = Form(""), default_matte: str = Form("flexible_antique"),
            output_res: str = Form("4k"), auto_assign: bool = Form(False),
            enabled: bool = Form(False),
            db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    _tv(db, tv_id)
    db.execute("UPDATE tvs SET name=?, host=?, mac=?, default_matte=?, output_res=?, "
               "auto_assign=?, enabled=? WHERE id=?",
               (name.strip(), host.strip(), mac.strip(), default_matte,
                "1080p" if output_res == "1080p" else "4k",
                int(auto_assign), int(enabled), tv_id))
    db.commit()
    worker.kick()
    return RedirectResponse("/tvs", 303)


@router.post("/tvs/{tv_id}/delete")
def delete_tv(tv_id: int, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    _tv(db, tv_id)
    db.execute("DELETE FROM tvs WHERE id=?", (tv_id,))
    db.commit()
    with contextlib.suppress(OSError):
        config.token_path(tv_id).unlink()
    return RedirectResponse("/tvs", 303)


@router.get("/tvs/{tv_id}/doctor")
def tv_doctor(request: Request, tv_id: int, db=Depends(dbm.get_db),
              user=Depends(auth.require_admin)):
    tv = _tv(db, tv_id)
    steps = run_doctor(tv)
    return render_page(request, db, "doctor.html", user, steps=steps,
                       tv=dict(tv), paired=TVService(tv).has_token())


@router.post("/tvs/{tv_id}/pair")
def pair(tv_id: int, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    svc = TVService(_tv(db, tv_id))
    try:
        svc.pair()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@router.post("/tvs/{tv_id}/wake")
def wake(tv_id: int, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    tv = _tv(db, tv_id)
    if not tv["mac"]:
        raise HTTPException(400, "no MAC address set for this TV")
    TVService(tv).wake(tv["mac"])
    return JSONResponse({"ok": True})


@router.get("/tvs/{tv_id}/artmode")
def artmode_get(tv_id: int, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    svc = TVService(_tv(db, tv_id))
    try:
        if not svc.port_open():
            raise HTTPException(502, "TV is unreachable right now")
        return JSONResponse(svc.artmode_settings())
    finally:
        svc.reset()


@router.post("/tvs/{tv_id}/artmode")
def artmode_set(tv_id: int, body: dict = Body(...), db=Depends(dbm.get_db),
                      user=Depends(auth.require_admin)):
    """Body: one or more of {artmode, brightness, color_temperature, motion_timer,
    motion_sensitivity, brightness_sensor, slideshow_minutes, slideshow_shuffle}."""
    svc = TVService(_tv(db, tv_id))
    if not svc.port_open():
        svc.reset()
        raise HTTPException(502, "TV is unreachable right now")
    applied, errors = [], {}
    try:
        ops = {
            "artmode": lambda v: svc.set_artmode(bool(v)),  # attempts guarded below
            "brightness": lambda v: svc.set_brightness(int(v)),
            "color_temperature": lambda v: svc.set_color_temperature(int(v)),
            "motion_timer": lambda v: svc.set_motion_timer(str(v)),
            "motion_sensitivity": lambda v: svc.set_motion_sensitivity(str(v)),
            "brightness_sensor": lambda v: svc.set_brightness_sensor(bool(v)),
        }
        for key, fn in ops.items():
            if key in body:
                try:
                    fn(body[key])
                    applied.append(key)
                except (TVError, ValueError) as e:
                    errors[key] = str(e)
        if "slideshow_minutes" in body:
            try:
                svc.set_slideshow(int(body["slideshow_minutes"]),
                                  bool(body.get("slideshow_shuffle", True)))
                applied.append("slideshow")
            except (TVError, ValueError) as e:
                errors["slideshow"] = str(e)
    finally:
        svc.reset()
    return JSONResponse({"ok": not errors, "applied": applied, "errors": errors},
                        status_code=200 if not errors else 502)


@router.post("/tvs/{tv_id}/import")
def import_tv(tv_id: int, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    _tv(db, tv_id)
    if worker.import_state["running"]:
        raise HTTPException(409, "an import is already running")
    worker.start_import(tv_id)
    return RedirectResponse("/", 303)


@router.get("/tvs/{tv_id}/export")
def export_tv(tv_id: int, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    tv = _tv(db, tv_id)
    token = ""
    with contextlib.suppress(OSError):
        token = config.token_path(tv_id).read_text().strip()
    return JSONResponse({"env": "\n".join([
        f"TV_HOST={tv['host']}", f"TV_NAME={tv['name']}",
        f"TV_CLIENT_NAME={tv['client_name']}", f"TV_MAC={tv['mac']}",
        f"TV_TOKEN={token}"])})


# ---------------------------------------------------------------- schedules
@router.post("/tvs/{tv_id}/schedules")
def add_schedule(tv_id: int, mode: str = Form("random"),
                 interval_minutes: int = Form(60), time_start: str = Form(""),
                 time_end: str = Form(""), days: list[str] = Form([]),
                 tag: str = Form(""),
                 db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    _tv(db, tv_id)
    if mode not in ("random", "sequential", "favorites"):
        raise HTTPException(400, "bad mode")
    daystr = "".join(sorted(set(d for d in days if d in "0123456"))) or "0123456"
    db.execute("INSERT INTO schedules(tv_id, mode, interval_minutes, time_start, "
               "time_end, days, tag) VALUES(?,?,?,?,?,?,?)",
               (tv_id, mode, max(1, interval_minutes), time_start.strip(),
                time_end.strip(), daystr, tag.strip()))
    db.commit()
    return RedirectResponse("/tvs", 303)


@router.post("/schedules/{sid}")
def edit_schedule(sid: int, action: str = Form(...),
                  db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    if action == "toggle":
        db.execute("UPDATE schedules SET enabled=1-enabled WHERE id=?", (sid,))
    elif action == "delete":
        db.execute("DELETE FROM schedules WHERE id=?", (sid,))
    else:
        raise HTTPException(400)
    db.commit()
    return RedirectResponse("/tvs", 303)


# --------------------------------------------------------------------- admin
@router.get("/admin")
def admin_page(request: Request, db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    users = db.execute("SELECT * FROM users ORDER BY created").fetchall()
    return render_page(request, db, "admin.html", user,
                       users=[dict(u) for u in users],
                       settings=config.all_settings(db),
                       watch=status_watch(db),
                       tunnel=cloudflare.tunnel.state,
                       access_enabled=cloudflare.access_enabled(db))


@router.get("/admin/logs")
def admin_logs(db=Depends(dbm.get_db), user=Depends(auth.require_admin)):
    from . import logbuf
    return JSONResponse({"logs": logbuf.recent()})


@router.post("/admin/settings")
def save_settings(request: Request, db=Depends(dbm.get_db),
                  user=Depends(auth.require_admin),
                  key: str = Form(...), value: str = Form(""), clear: str = Form("")):
    if key not in config.SETTINGS:
        raise HTTPException(400, "unknown setting")
    value = value.strip()
    if key in config.SECRET_KEYS and not value and not clear:
        return RedirectResponse("/admin", 303)   # masked field left blank: keep it
    try:
        config.set(db, key, value)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if key == "cf_tunnel_token" and cloudflare.tunnel.state["wanted"]:
        cloudflare.tunnel.stop() if not value else cloudflare.tunnel.restart(value)
    if key == "cf_access_team":
        cloudflare._jwks.clear()
    return RedirectResponse("/admin", 303)


@router.post("/admin/tunnel")
def tunnel_control(action: str = Form(...), db=Depends(dbm.get_db),
                   user=Depends(auth.require_admin)):
    if action == "start":
        token = config.get(db, "cf_tunnel_token")
        if not token:
            raise HTTPException(400, "save a tunnel token first")
        cloudflare.tunnel.start(token)
    elif action == "stop":
        cloudflare.tunnel.stop()
    else:
        raise HTTPException(400, "unknown action")
    return RedirectResponse("/admin#remote", 303)


@router.get("/admin/tunnel/status")
def tunnel_status(user=Depends(auth.require_admin)):
    return JSONResponse(cloudflare.tunnel.state)


@router.post("/admin/branding-logo")
async def branding_logo(file: UploadFile = File(...), db=Depends(dbm.get_db),
                        user=Depends(auth.require_admin)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, "logo must be png/jpg/webp")
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
