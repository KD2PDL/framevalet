"""Background engine: per-TV push queues, reconcile, app-side schedules,
folder watcher (non-destructive import), and the rclone sync runner.

Queue-first is the whole point: uploads always succeed instantly; a TV being
off just means "queued" until it isn't. All state lives in the DB, so a
restart resumes exactly where it left off.
"""
import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import random
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from . import config, db as dbm, maint, pipeline, ws
from .tvservice import TVService, TVError, TVUnauthorized

log = logging.getLogger("framevalet.worker")

status = {          # dashboard state
    "tvs": {},      # tv_id -> {ok, error, last_check, pushing}
    "watch": {"last_scan": 0.0, "last_error": "", "seen": 0},
    "rclone": {"last_sync": 0.0, "last_error": "", "available": bool(shutil.which("rclone")),
               "healthy": None},   # None=never run, True=last sync ok, False=last failed
}
import_state = {"running": False, "tv_id": None, "done": 0, "total": 0, "error": ""}

_wakeup = asyncio.Event()
_reconciled: dict[int, float] = {}


def kick():
    # signalled from request threads; asyncio.Event.set() must run on the loop
    if ws._loop is not None:
        ws._loop.call_soon_threadsafe(_wakeup.set)
    else:
        _wakeup.set()


def _set_tv_state(st: dict, tv_id: int, ok: bool, error: str):
    changed = (st.get("ok") != ok) or (st.get("error") != error)
    st.update(ok=ok, error=error)
    if changed:
        ws.broadcast({"type": "tv_status", "tv_id": tv_id, "ok": ok, "error": error})


def tv_status(tv_id: int) -> dict:
    return status["tvs"].setdefault(
        int(tv_id), {"ok": False, "error": "not checked yet", "last_check": 0.0,
                     "pushing": None})


def assign_photo(db, photo_id: int, tv_ids=None):
    """Queue a photo to given TVs (default: all enabled auto-assign TVs)."""
    if tv_ids is None:
        tv_ids = [r["id"] for r in db.execute(
            "SELECT id FROM tvs WHERE enabled=1 AND auto_assign=1")]
    for tid in tv_ids:
        db.execute(
            "INSERT INTO tv_photos(tv_id, photo_id, status) VALUES(?,?,'queued') "
            "ON CONFLICT(tv_id, photo_id) DO UPDATE SET status='queued', error=NULL "
            "WHERE tv_photos.status IN ('failed','removed')", (tid, photo_id))
    db.commit()


# ---------------------------------------------------------------- rendering
def ensure_render(db, photo, tv) -> tuple[Path, str]:
    """Render (or reuse cached render) of a photo for a TV. Returns (path, key)."""
    quality = int(config.get(db, "jpeg_quality"))
    unsharp = config.get(db, "unsharp") == "true"
    key = pipeline.render_key(photo["sha256"], photo["edits"], photo["style"],
                              tv["output_res"], quality, unsharp)
    out = config.RENDERS_DIR / f"{key}.jpg"
    if not out.exists():
        pipeline.render(Path(photo["orig_path"]), out, photo["edits"], photo["style"],
                        tv["output_res"], quality, unsharp)
    return out, key


# ---------------------------------------------------------------- per-TV work
def _drain_pending_deletes(db, tv, svc: TVService):
    for r in db.execute("SELECT content_id FROM pending_tv_deletes WHERE tv_id=?",
                        (tv["id"],)).fetchall():
        try:
            svc.delete(r["content_id"], attempts=1)
        except TVError as e:
            if "does not exist" not in str(e):
                continue        # still unreachable/busy; retry next loop
        db.execute("DELETE FROM pending_tv_deletes WHERE tv_id=? AND content_id=?",
                   (tv["id"], r["content_id"]))
        db.commit()


def _push_tv(db, tv, svc: TVService) -> int:
    st = tv_status(tv["id"])
    rows = db.execute(
        "SELECT tp.photo_id, tp.status, tp.content_id, tp.render_key, p.matte AS photo_matte, p.* "
        "FROM tv_photos tp JOIN photos p ON p.id = tp.photo_id "
        "WHERE tp.tv_id=? AND (tp.status IN ('queued','failed') "
        "  OR (tp.status='on_tv' AND p.orig_path IS NOT NULL)) ORDER BY tp.photo_id",
        (tv["id"],)).fetchall()
    pushed = 0
    for row in rows:
        if row["status"] == "on_tv":
            if not row["sha256"]:
                continue
            quality = int(config.get(db, "jpeg_quality"))
            unsharp = config.get(db, "unsharp") == "true"
            current = pipeline.render_key(row["sha256"], row["edits"], row["style"],
                                          tv["output_res"], quality, unsharp)
            if current == row["render_key"]:
                continue        # up to date; nothing to do
        if not row["orig_path"]:
            continue            # imported photo without original: nothing to render
        st["pushing"] = row["filename"]
        try:
            path, key = ensure_render(db, row, tv)
            matte = row["photo_matte"] or tv["default_matte"]
            cid = svc.upload(path.read_bytes(), matte, row["taken_date"])
            if row["content_id"]:   # edit re-push: replace the old copy
                with contextlib.suppress(TVError):
                    svc.delete(row["content_id"])
            db.execute(
                "UPDATE tv_photos SET content_id=?, status='on_tv', error=NULL, "
                "render_key=?, matte=? WHERE tv_id=? AND photo_id=?",
                (cid, key, matte, tv["id"], row["photo_id"]))
            db.commit()
            pushed += 1
            ws.broadcast({"type": "pushed", "tv_id": tv["id"],
                          "photo_id": row["photo_id"], "filename": row["filename"]})
        except (TVError, OSError, ValueError, pipeline.PipelineError) as e:
            db.execute(
                "UPDATE tv_photos SET status='failed', error=? WHERE tv_id=? AND photo_id=?",
                (str(e)[:300], tv["id"], row["photo_id"]))
            db.commit()
            log.warning("push failed [%s -> %s]: %s", row["filename"], tv["name"],
                        str(e)[:200])
            ws.broadcast({"type": "push_failed", "tv_id": tv["id"],
                          "photo_id": row["photo_id"], "filename": row["filename"],
                          "error": str(e)[:120]})
            if isinstance(e, TVError):
                raise
        finally:
            st["pushing"] = None
    return pushed


def _reconcile_tv(db, tv, svc: TVService):
    """Diff TV state against our per-TV manifest. External photos are recorded
    but never touched automatically."""
    on_tv = {x["content_id"]: x for x in svc.my_photos()}
    ours = db.execute(
        "SELECT photo_id, content_id, status FROM tv_photos "
        "WHERE tv_id=? AND content_id IS NOT NULL", (tv["id"],)).fetchall()
    our_ids = set()
    for p in ours:
        our_ids.add(p["content_id"])
        if p["content_id"] not in on_tv and p["status"] == "on_tv":
            db.execute("UPDATE tv_photos SET status='removed', content_id=NULL "
                       "WHERE tv_id=? AND photo_id=?", (tv["id"], p["photo_id"]))
    pending = {r["content_id"] for r in db.execute(
        "SELECT content_id FROM pending_tv_deletes WHERE tv_id=?", (tv["id"],))}
    known_external = {r["c"] for r in db.execute(
        "SELECT tp.content_id AS c FROM tv_photos tp JOIN photos p ON p.id=tp.photo_id "
        "WHERE tp.tv_id=? AND p.source='external' AND tp.content_id IS NOT NULL",
        (tv["id"],))}
    for cid, item in on_tv.items():
        if cid not in our_ids and cid not in known_external and cid not in pending:
            cur = db.execute(
                "INSERT INTO photos(filename, taken_date, source, created) "
                "VALUES(?,?,?,?)", (cid, item.get("image_date"), "external", dbm.now()))
            db.execute(
                "INSERT INTO tv_photos(tv_id, photo_id, content_id, status, matte) "
                "VALUES(?,?,?,?,?)",
                (tv["id"], cur.lastrowid, cid, "on_tv", item.get("matte_id")))
    db.commit()
    _reconciled[tv["id"]] = time.time()


# ---------------------------------------------------------------- scheduler
def _fire_schedules(db, tv, svc: TVService):
    now = dt.datetime.now()
    for s in db.execute("SELECT * FROM schedules WHERE tv_id=? AND enabled=1",
                        (tv["id"],)).fetchall():
        if time.time() - s["last_fired"] < s["interval_minutes"] * 60:
            continue
        if str(now.weekday()) not in s["days"]:
            continue
        if s["time_start"] and s["time_end"]:
            t = now.strftime("%H:%M")
            a, b = s["time_start"], s["time_end"]
            inside = (a <= t <= b) if a <= b else (t >= a or t <= b)  # overnight windows
            if not inside:
                continue
        q = ("SELECT tp.content_id, tp.photo_id, p.favorite FROM tv_photos tp "
             "JOIN photos p ON p.id=tp.photo_id WHERE tp.tv_id=? AND tp.status='on_tv' "
             "AND tp.content_id IS NOT NULL")
        args = [tv["id"]]
        if s["tag"]:
            q += (" AND tp.photo_id IN (SELECT pt.photo_id FROM photo_tags pt "
                  "JOIN tags t ON t.id=pt.tag_id WHERE t.name=?)")
            args.append(s["tag"])
        pool = db.execute(q + " ORDER BY tp.photo_id", args).fetchall()
        if not pool:
            continue
        advance = None
        if s["mode"] == "sequential":
            pick = pool[s["cursor"] % len(pool)]
            advance = (s["cursor"] + 1) % len(pool)
        elif s["mode"] == "favorites":
            weights = [5 if r["favorite"] else 1 for r in pool]
            pick = random.choices(pool, weights=weights, k=1)[0]
        else:
            pick = random.choice(pool)
        try:
            svc.select(pick["content_id"])
            if advance is not None:
                db.execute("UPDATE schedules SET cursor=? WHERE id=?", (advance, s["id"]))
            db.execute("UPDATE schedules SET last_fired=? WHERE id=?",
                       (time.time(), s["id"]))
            db.commit()
            ws.broadcast({"type": "schedule_fired", "tv_id": tv["id"],
                          "photo_id": pick["photo_id"]})
        except TVError as e:
            log.warning("schedule %s fire failed: %s", s["id"], e)


# ---------------------------------------------------------------- watcher
def ingest_file(db, path: Path, rel: str | None, user_id=None, filename=None) -> int | None:
    return ingest_bytes(db, path.read_bytes(), filename=filename or path.name,
                        user_id=user_id, rel=rel)


def ingest_bytes(db, data: bytes, filename: str, user_id=None,
                 rel: str | None = None) -> int | None:
    """Shared ingest for uploads + watcher. Returns photo id or None (duplicate)."""
    sha = hashlib.sha256(data).hexdigest()
    dup = db.execute("SELECT id FROM photos WHERE sha256=?", (sha,)).fetchone()
    if dup:
        if rel:  # same content re-appeared under the watcher: track new location
            db.execute("UPDATE photos SET folder_rel=? WHERE id=?", (rel, dup["id"]))
            db.commit()
        return None
    pid = secrets.token_hex(8)
    orig = config.ORIGINALS_DIR / f"{pid}.jpg"
    thumb = config.THUMBS_DIR / f"{pid}.jpg"
    meta = pipeline.ingest(data, orig, thumb)
    import sqlite3 as _sqlite3
    try:
        cur = db.execute(
        "INSERT INTO photos(filename, sha256, orig_path, thumb_path, width, height, "
        "taken_date, style, source, folder_rel, uploaded_by, created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (filename, meta["sha256"], str(orig), str(thumb),
         meta["width"], meta["height"], meta["taken_date"],
         config.get(db, "default_style"), "folder" if rel else "upload",
         rel, user_id, dbm.now()))
    except _sqlite3.IntegrityError:      # concurrent upload of the same file won the race
        db.rollback()
        for f in (orig, thumb):
            with contextlib.suppress(OSError):
                f.unlink()
        return None
    db.commit()
    assign_photo(db, cur.lastrowid)
    return cur.lastrowid


def _scan_watch_folder(db):
    """Non-destructive import: new files in the watched folder are ingested and
    pushed. A file DISAPPEARING from the folder NEVER deletes anything -- a
    transient empty/failed cloud sync or an unmounted share can't wipe the
    library. Removing a photo is always an explicit action in the UI.
    (A count of missing source files is surfaced for visibility only.)"""
    root = config.WATCH_DIR
    seen = {}
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix.lower() in pipeline.ACCEPTED:
            seen[str(f.relative_to(root))] = f
    status["watch"]["seen"] = len(seen)
    known = {r["folder_rel"]: r for r in db.execute(
        "SELECT folder_rel FROM photos WHERE source='folder' AND folder_rel IS NOT NULL")}
    new_count = 0
    for rel, f in seen.items():
        if rel not in known:
            try:
                if ingest_file(db, f, rel) is not None:
                    new_count += 1
            except (pipeline.PipelineError, OSError) as e:
                log.warning("watcher skipped %s: %s", rel, e)
    if new_count:
        ws.broadcast({"type": "ingested", "count": new_count})

    missing = sum(rel not in seen for rel in known)
    status["watch"]["last_error"] = (
        f"{missing} source file(s) no longer in the folder; photos kept "
        "(remove them in the app if you want them gone)" if missing else "")
    status["watch"]["last_scan"] = time.time()


def delete_photo_everywhere(db, photo):
    """Remove a photo from the library now; TV-side copies are deleted live when
    the TV answers quickly, otherwise queued in pending_tv_deletes and drained
    by the worker when the TV comes back. Never blocks the caller for minutes."""
    for tp in db.execute("SELECT tp.*, t.* FROM tv_photos tp JOIN tvs t ON t.id=tp.tv_id "
                         "WHERE tp.photo_id=? AND tp.content_id IS NOT NULL",
                         (photo["id"],)).fetchall():
        svc = TVService(tp)
        done = False
        if svc.port_open():
            with contextlib.suppress(TVError):
                svc.delete(tp["content_id"], attempts=1)
                done = True
        svc.reset()
        if not done:
            db.execute("INSERT OR IGNORE INTO pending_tv_deletes(tv_id, content_id) "
                       "VALUES(?,?)", (tp["tv_id"], tp["content_id"]))
    for key in ("orig_path", "thumb_path"):
        if photo[key]:
            with contextlib.suppress(OSError):
                Path(photo[key]).unlink()
    # cache renders/previews/crop-thumbs derived from this photo
    if photo["sha256"]:
        pk = pipeline.preview_key(photo["sha256"], photo["edits"])
        for f in (config.RENDERS_DIR / f"preview_{pk}.jpg",
                  config.THUMBS_DIR / f"crop_{pk}.jpg"):
            with contextlib.suppress(OSError):
                f.unlink()
    for r in db.execute("SELECT render_key FROM tv_photos WHERE photo_id=? "
                        "AND render_key IS NOT NULL AND render_key != ''",
                        (photo["id"],)).fetchall():
        with contextlib.suppress(OSError):
            (config.RENDERS_DIR / f"{r['render_key']}.jpg").unlink()
    db.execute("DELETE FROM photos WHERE id=?", (photo["id"],))
    db.commit()


def _rclone_sync(db):
    remote = config.get(db, "rclone_remote")
    if not remote or not status["rclone"]["available"]:
        return
    dest = config.WATCH_DIR / "remote"
    dest.mkdir(exist_ok=True)
    try:
        r = subprocess.run(
            ["rclone", "sync", remote, str(dest), "--exclude", ".*/**",
             "--max-delete", "50"],
            capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        status["rclone"]["last_error"] = "sync timed out"
        status["rclone"]["healthy"] = False
        log.warning("rclone sync timed out")
        return
    if r.returncode != 0:
        status["rclone"]["last_error"] = r.stderr.strip()[-300:]
        status["rclone"]["healthy"] = False
        log.warning("rclone sync failed: %s", r.stderr.strip()[:300])
    else:
        status["rclone"]["last_error"] = ""
        status["rclone"]["healthy"] = True
        status["rclone"]["last_sync"] = time.time()


# ---------------------------------------------------------------- import
def start_import(tv_id: int):
    if ws._loop is None:
        raise RuntimeError("worker loop not ready")
    asyncio.run_coroutine_threadsafe(_import_from_tv(tv_id), ws._loop)


async def _import_from_tv(tv_id: int):
    """Adopt everything currently on a TV: per-TV manifest entries plus TV-side
    thumbnails. Adopted photos are manageable but carry no original."""
    import_state.update(running=True, tv_id=tv_id, done=0, total=0, error="")

    def _run():
        db = dbm.connect()
        tv = db.execute("SELECT * FROM tvs WHERE id=?", (tv_id,)).fetchone()
        svc = TVService(tv)
        try:
            items = svc.my_photos()
            known = {r["content_id"] for r in db.execute(
                "SELECT content_id FROM tv_photos WHERE tv_id=? AND content_id IS NOT NULL",
                (tv_id,))}
            todo = [x for x in items if x["content_id"] not in known]
            import_state["total"] = len(todo)
            for x in todo:
                cid = x["content_id"]
                thumb_path = None
                data = svc.thumbnail(cid)
                if data:
                    thumb_path = config.THUMBS_DIR / f"tv{tv_id}_{cid}.jpg"
                    try:
                        pipeline.make_thumb_from_bytes(data, thumb_path)
                    except Exception:
                        thumb_path = None
                cur = db.execute(
                    "INSERT INTO photos(filename, thumb_path, taken_date, width, height, "
                    "source, created) VALUES(?,?,?,?,?,?,?)",
                    (cid, str(thumb_path) if thumb_path else None, x.get("image_date"),
                     x.get("width"), x.get("height"), "import", dbm.now()))
                db.execute(
                    "INSERT INTO tv_photos(tv_id, photo_id, content_id, status, matte) "
                    "VALUES(?,?,?,?,?)",
                    (tv_id, cur.lastrowid, cid, "on_tv", x.get("matte_id")))
                db.commit()
                import_state["done"] += 1
                if import_state["done"] % 10 == 0 or import_state["done"] == import_state["total"]:
                    ws.broadcast({"type": "import", **import_state})
        finally:
            svc.reset()
            db.close()

    try:
        await asyncio.to_thread(_run)
    except Exception as e:
        log.exception("import failed")
        import_state["error"] = str(e)[:300]
    finally:
        import_state["running"] = False


# ---------------------------------------------------------------- main loop
def _sweep_cache(db):
    """Delete cache files (renders, previews, crop-thumbs) not referenced by any
    live photo's current key set. Bounded work; runs daily."""
    keep = set()
    quality = int(config.get(db, "jpeg_quality"))
    unsharp = config.get(db, "unsharp") == "true"
    reslist = {r["output_res"] for r in db.execute("SELECT DISTINCT output_res FROM tvs")} \
              or {"4k"}
    for ph in db.execute("SELECT sha256, edits, style FROM photos WHERE sha256 IS NOT NULL"):
        keep.add(f"preview_{pipeline.preview_key(ph['sha256'], ph['edits'])}.jpg")
        keep.add(f"crop_{pipeline.preview_key(ph['sha256'], ph['edits'])}.jpg")
        for res in reslist:
            keep.add(pipeline.render_key(ph["sha256"], ph["edits"], ph["style"],
                                         res, quality, unsharp) + ".jpg")
    for r in db.execute("SELECT render_key FROM tv_photos WHERE render_key IS NOT NULL "
                        "AND render_key != ''"):
        keep.add(r["render_key"] + ".jpg")
    removed = 0
    for d in (config.RENDERS_DIR, config.THUMBS_DIR):
        for f in d.glob("*.jpg"):
            if f.name.startswith(("preview_", "crop_")) or (
                    d == config.RENDERS_DIR and len(f.stem) == 40):  # render_key = sha1
                if f.name not in keep:
                    with contextlib.suppress(OSError):
                        f.unlink(); removed += 1
    if removed:
        log.info("cache sweep removed %d orphaned files", removed)


async def run():
    log.info("worker started")
    last_watch = last_rclone = last_sweep = 0.0
    last_backup = time.time()          # first scheduled backup one interval after boot
    while True:
        db = dbm.connect()
        try:
            # remote + folder ingestion
            if config.get(db, "rclone_remote") and \
               time.time() - last_rclone > int(config.get(db, "rclone_interval")):
                await asyncio.to_thread(_rclone_sync, db)
                last_rclone = time.time()
            if config.get(db, "watch_enabled") == "true" and \
               time.time() - last_watch > int(config.get(db, "watch_interval")):
                await asyncio.to_thread(_scan_watch_folder, db)
                last_watch = time.time()

            hours = int(config.get(db, "backup_interval") or 0)
            if hours and time.time() - last_backup > hours * 3600:
                await asyncio.to_thread(maint.scheduled_backup, db)
                last_backup = time.time()

            if time.time() - last_sweep > 86400:      # daily orphan-cache sweep
                await asyncio.to_thread(_sweep_cache, db)
                last_sweep = time.time()

            for tv in db.execute("SELECT * FROM tvs WHERE enabled=1").fetchall():
                st = tv_status(tv["id"])
                svc = TVService(tv)
                try:
                    if not svc.has_token():
                        _set_tv_state(st, tv["id"], False, "not paired")
                        continue
                    if not svc.port_open():
                        _set_tv_state(st, tv["id"], False, "unreachable")
                        continue
                    await asyncio.to_thread(_drain_pending_deletes, db, tv, svc)
                    await asyncio.to_thread(_push_tv, db, tv, svc)
                    every = int(config.get(db, "reconcile_minutes")) * 60
                    if time.time() - _reconciled.get(tv["id"], 0) > every:
                        await asyncio.to_thread(_reconcile_tv, db, tv, svc)
                    await asyncio.to_thread(_fire_schedules, db, tv, svc)
                    _set_tv_state(st, tv["id"], True, "")
                except TVUnauthorized as e:
                    _set_tv_state(st, tv["id"], False, f"not authorized: {e}")
                except TVError as e:
                    _set_tv_state(st, tv["id"], False, str(e))
                finally:
                    st["last_check"] = time.time()
                    svc.reset()
        except Exception:
            log.exception("worker loop error")
        finally:
            db.close()
        _wakeup.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wakeup.wait(), timeout=60)
