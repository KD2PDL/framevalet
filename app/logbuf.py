"""In-memory ring buffer of recent log records, surfaced in Admin > Logs.

Holds the last 500 records from the app's own loggers (worker, TV pushes,
watcher, rclone, sources). For full history use the host's journal/docker logs;
this is the "why did that just fail" view, not an archive.
"""
import collections
import logging
import time

BUFFER: collections.deque = collections.deque(maxlen=500)


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
    h = BufferHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    h.setLevel(logging.INFO)
    logging.getLogger().addHandler(h)


def recent(limit: int = 200) -> list[dict]:
    return list(BUFFER)[-limit:]
