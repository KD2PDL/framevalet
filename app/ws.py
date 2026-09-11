"""One WebSocket channel for live UI updates.

Events broadcast as {"type": ..., ...}:
  tv_status      {tv_id, ok, error}           on state transitions
  pushed         {tv_id, photo_id, filename}  after each successful TV upload
  push_failed    {tv_id, photo_id, filename, error}
  import         {tv_id, done, total, running, error}
  schedule_fired {tv_id, photo_id}
  ingested       {count}                      watcher/rclone brought in photos
  counts_dirty   {}                           anything that changes grid badges

Worker code runs partly in threads, so broadcast() is thread-safe: it hops onto
the main loop with call_soon_threadsafe. The browser keeps its polling fallback;
when the socket is healthy the client stretches the poll to 60s.
"""
import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("framevalet.ws")
router = APIRouter()

_loop: asyncio.AbstractEventLoop | None = None
_clients: set[WebSocket] = set()


def init(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


def broadcast(payload: dict):
    """Safe from any thread."""
    if not _loop or not _clients:
        return
    msg = json.dumps(payload)
    _loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_send_all(msg)))


async def _send_all(msg: str):
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # Auth: same session cookie as the pages.
    from . import auth, db as dbm
    token = websocket.cookies.get(auth.COOKIE)
    db = dbm.connect()
    try:
        row = None
        if token:
            row = db.execute(
                "SELECT u.id FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=? AND s.expires > ? AND u.disabled=0",
                (token, dbm.now())).fetchone()
    finally:
        db.close()
    if not row:
        await websocket.close(code=4401)
        return
    from .security import same_origin
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    if not same_origin(websocket.headers.get("origin"), scheme,
                       websocket.headers.get("host")):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    _clients.add(websocket)
    try:
        while True:
            # Client sends pings/keepalives; also re-validate the session so an
            # expired/disabled login is dropped promptly.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(websocket.receive_text(), timeout=25)
            vdb = dbm.connect()
            try:
                ok = vdb.execute(
                    "SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id "
                    "WHERE s.token=? AND s.expires>? AND u.disabled=0",
                    (token, dbm.now())).fetchone()
            finally:
                vdb.close()
            if not ok:
                await websocket.close(code=4401)
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("ws closed uncleanly", exc_info=True)
    finally:
        _clients.discard(websocket)
