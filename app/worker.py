"""Background worker: pushes queued photos to the TV when it's reachable,
and periodically reconciles the manifest against what the TV reports.

Queue-first is the whole point: uploads always succeed instantly for the user;
the TV being off just means "queued" until it isn't. State lives in the photos
table, so a restart resumes exactly where it left off.
"""
import asyncio
import contextlib
import logging
import time
from pathlib import Path

from . import config, db as dbm
from .tvservice import TVService, TVError, TVUnauthorized

log = logging.getLogger("framevalet.worker")

status = {  # surfaced on the dashboard
    "tv_ok": False,
    "tv_error": "",
    "last_check": 0.0,
    "last_reconcile": 0.0,
    "pushing": None,
}

_wakeup = asyncio.Event()

import_state = {"running": False, "done": 0, "total": 0, "error": ""}


def start_import():
    asyncio.get_event_loop().create_task(_import_from_tv())


async def _import_from_tv():
    """Adopt everything currently on the TV: manifest entries plus TV-side
    thumbnails. Adopted photos are fully manageable (delete/display/matte)
    but carry no full-resolution original."""
    import_state.update(running=True, done=0, total=0, error="")

    def _run():
        db = dbm.connect()
        svc = _svc(db)
        try:
            items = svc.my_photos()
            known = {r["tv_content_id"] for r in db.execute(
                "SELECT tv_content_id FROM photos WHERE tv_content_id IS NOT NULL")}
            todo = [x for x in items if x["content_id"] not in known]
            import_state["total"] = len(todo)
            for x in todo:
                cid = x["content_id"]
                thumb_path = None
                data = svc.thumbnail(cid)
                if data:
                    from . import pipeline
                    thumb_path = config.THUMBS_DIR / f"tv_{cid}.jpg"
                    try:
                        pipeline.make_thumb_from_bytes(data, thumb_path)
                    except Exception:
                        thumb_path = None
                db.execute(
                    "INSERT INTO photos(filename, thumb_path, taken_date, matte, "
                    "source, tv_content_id, status, width, height, created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cid, str(thumb_path) if thumb_path else None,
                     x.get("image_date"), x.get("matte_id"), "import", cid,
                     "on_tv", x.get("width"), x.get("height"), dbm.now()))
                db.commit()
                import_state["done"] += 1
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


def kick():
    """Called after an upload/delete so the worker reacts immediately."""
    _wakeup.set()


def _svc(db) -> TVService:
    return TVService(config.get(db, "tv_host"), config.get(db, "tv_client_name"))


def _push_queued(db, svc: TVService) -> int:
    rows = db.execute(
        "SELECT * FROM photos WHERE status IN ('queued','failed') AND proc_path IS NOT NULL "
        "ORDER BY id").fetchall()
    pushed = 0
    for p in rows:
        status["pushing"] = p["filename"]
        try:
            data = Path(p["proc_path"]).read_bytes()
            cid = svc.upload(data, p["matte"] or config.get(db, "default_matte"),
                             p["taken_date"])
            db.execute("UPDATE photos SET tv_content_id=?, status='on_tv', error=NULL "
                       "WHERE id=?", (cid, p["id"]))
            db.commit()
            pushed += 1
            if config.get(db, "keep_originals") != "true" and p["orig_path"]:
                with contextlib.suppress(OSError):
                    Path(p["orig_path"]).unlink()
                db.execute("UPDATE photos SET orig_path=NULL WHERE id=?", (p["id"],))
                db.commit()
        except (TVError, OSError) as e:
            db.execute("UPDATE photos SET status='failed', error=? WHERE id=?",
                       (str(e)[:300], p["id"]))
            db.commit()
            raise
        finally:
            status["pushing"] = None
    return pushed


def _reconcile(db, svc: TVService):
    """Diff TV state against ours. External photos (SmartThings etc.) are recorded
    but never touched automatically; our missing photos get re-queued."""
    on_tv = {x["content_id"]: x for x in svc.my_photos()}
    ours = db.execute("SELECT id, tv_content_id, status FROM photos "
                      "WHERE tv_content_id IS NOT NULL").fetchall()
    our_ids = set()
    for p in ours:
        our_ids.add(p["tv_content_id"])
        if p["tv_content_id"] not in on_tv and p["status"] == "on_tv":
            # deleted on the TV itself (remote/SmartThings): reflect, don't re-push
            db.execute("UPDATE photos SET status='removed', tv_content_id=NULL "
                       "WHERE id=?", (p["id"],))
    known_external = {r["tv_content_id"] for r in db.execute(
        "SELECT tv_content_id FROM photos WHERE source='external' "
        "AND tv_content_id IS NOT NULL")}
    for cid, item in on_tv.items():
        if cid not in our_ids and cid not in known_external:
            db.execute(
                "INSERT INTO photos(filename, source, tv_content_id, status, "
                "taken_date, matte, created) VALUES(?,?,?,?,?,?,?)",
                (cid, "external", cid, "on_tv", item.get("image_date"),
                 item.get("matte_id"), dbm.now()))
    db.commit()
    status["last_reconcile"] = time.time()


async def run():
    log.info("worker started")
    while True:
        db = dbm.connect()
        try:
            host = config.get(db, "tv_host")
            svc = _svc(db)
            if host and svc.has_token():
                try:
                    svc.port_open() or (_ for _ in ()).throw(TVError("unreachable"))
                    _push_queued(db, svc)
                    every = int(config.get(db, "reconcile_minutes")) * 60
                    if time.time() - status["last_reconcile"] > every:
                        _reconcile(db, svc)
                    status.update(tv_ok=True, tv_error="")
                except TVUnauthorized as e:
                    status.update(tv_ok=False, tv_error=f"not authorized: {e}")
                except TVError as e:
                    status.update(tv_ok=False, tv_error=str(e))
                finally:
                    svc.reset()
            else:
                status.update(tv_ok=False,
                              tv_error="TV not configured" if not host else "not paired")
            status["last_check"] = time.time()
        except Exception:
            log.exception("worker loop error")
        finally:
            db.close()
        _wakeup.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wakeup.wait(), timeout=60)
