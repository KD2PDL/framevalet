"""Logging: an in-memory ring buffer for the live Admin view, plus rotating
log files on disk (data/logs/framevalet.log, 5 x 5 MB) that admins can
download. The level is a setting (LOG_LEVEL env or Admin > Logs) and applies
at runtime without a restart. uvicorn's own loggers are routed into the same
file so request errors sit next to app events.
"""
import collections
import logging
import logging.handlers
import time
import zipfile
import io

from . import config

BUFFER: collections.deque = collections.deque(maxlen=500)
LOG_DIR = config.DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "framevalet.log"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_file_handler: logging.Handler | None = None


class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            BUFFER.append({
                "ts": time.time(),
                "level": record.levelname,
                "logger": record.name.replace("framevalet.", ""),
                "message": self.format(record)[:500],
            })
        except Exception:
            pass


def install():
    global _file_handler
    root = logging.getLogger()
    h = BufferHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(h)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(_file_handler)
    for name in ("uvicorn.error", "uvicorn.access"):   # uvicorn doesn't propagate
        logging.getLogger(name).addHandler(_file_handler)


def set_level(level: str):
    level = (level or "INFO").upper()
    if level not in LEVELS:
        raise ValueError(f"log level must be one of {', '.join(LEVELS)}")
    logging.getLogger().setLevel(level)
    logging.getLogger("framevalet").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel("INFO" if level == "DEBUG" else "WARNING")


def current_level() -> str:
    return logging.getLevelName(logging.getLogger().level)


def recent(limit: int = 200) -> list[dict]:
    return list(BUFFER)[-limit:]


def files() -> list[dict]:
    """Newest first: framevalet.log, then .1, .2, ..."""
    out = []
    for f in sorted(LOG_DIR.glob("framevalet.log*"), key=lambda f: f.name):
        if f.is_file():
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
    return out


def zip_all() -> bytes:
    if _file_handler:
        _file_handler.flush()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files():
            z.write(LOG_DIR / f["name"], f["name"])
    return buf.getvalue()
