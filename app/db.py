"""SQLite, stdlib only. One connection per request via FastAPI dependency;
WAL mode so the background worker and web requests coexist.
"""
import secrets
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',          -- 'admin' | 'member'
  can_upload INTEGER NOT NULL DEFAULT 1,
  can_delete_any INTEGER NOT NULL DEFAULT 0,    -- members: own uploads only unless set
  disabled INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL,                       -- display name
  sha256 TEXT,                                  -- of the original upload (dedupe)
  orig_path TEXT,                               -- may be NULL (imported/space-saver)
  proc_path TEXT,                               -- optimized JPEG pushed to the TV
  thumb_path TEXT,
  width INTEGER, height INTEGER, bytes INTEGER,
  taken_date TEXT,                              -- 'YYYY:MM:DD HH:MM:SS' (TV format)
  matte TEXT,
  source TEXT NOT NULL DEFAULT 'upload',        -- 'upload' | 'import' | 'external'
  uploaded_by INTEGER REFERENCES users(id),
  tv_content_id TEXT,
  status TEXT NOT NULL DEFAULT 'queued',        -- queued|processing|on_tv|failed|removed
  error TEXT,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_photos_cid ON photos(tv_content_id)
  WHERE tv_content_id IS NOT NULL;
"""


def connect() -> sqlite3.Connection:
    # check_same_thread off: FastAPI runs sync deps in a threadpool but async
    # route bodies on the loop thread; each request still uses its connection
    # sequentially, which sqlite is fine with.
    db = sqlite3.connect(config.DB_PATH, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init():
    config.ensure_dirs()
    db = connect()
    db.executescript(SCHEMA)
    # session-signing secret: env wins, else generate once into settings
    if not config.APP_SECRET:
        row = db.execute("SELECT value FROM settings WHERE key='app_secret'").fetchone()
        if not row:
            db.execute("INSERT INTO settings(key,value) VALUES('app_secret',?)",
                       (secrets.token_urlsafe(32),))
    db.commit()
    db.close()


def app_secret(db) -> str:
    if config.APP_SECRET:
        return config.APP_SECRET
    return db.execute("SELECT value FROM settings WHERE key='app_secret'").fetchone()["value"]


def now() -> float:
    return time.time()


# FastAPI dependency
def get_db():
    db = connect()
    try:
        yield db
    finally:
        db.close()
