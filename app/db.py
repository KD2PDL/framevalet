"""SQLite, stdlib only. One connection per request via FastAPI dependency;
WAL mode so background workers and web requests coexist.

Schema v2: multiple TVs, per-TV photo manifests, tags/favorites, and
originals-as-source-of-truth (renders are derived, cached, disposable).
"""
import secrets
import sqlite3
import time

from . import config

SCHEMA_VERSION = 2

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
  email TEXT,                                   -- set for SSO (Cloudflare Access) users
  sso INTEGER NOT NULL DEFAULT 0,               -- 1 = auto-provisioned, no usable password
  perms TEXT,                                   -- comma list, see auth.PERMS; admins have all
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tvs (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  host TEXT NOT NULL,
  mac TEXT DEFAULT '',
  client_name TEXT NOT NULL DEFAULT 'framevalet',
  default_matte TEXT NOT NULL DEFAULT 'flexible_antique',
  output_res TEXT NOT NULL DEFAULT '4k',        -- '4k' | '1080p'
  auto_assign INTEGER NOT NULL DEFAULT 1,       -- new photos queue here automatically
  enabled INTEGER NOT NULL DEFAULT 1,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL,
  sha256 TEXT,                                  -- of the ORIGINAL file
  orig_path TEXT,                               -- source of truth; NULL only for TV imports
  thumb_path TEXT,
  width INTEGER, height INTEGER,                -- original dimensions
  taken_date TEXT,                              -- 'YYYY:MM:DD HH:MM:SS' (TV format)
  style TEXT NOT NULL DEFAULT 'fit',            -- 'fit' | 'blurfill' (portraits)
  matte TEXT,                                   -- override; NULL = TV default
  edits TEXT,                                   -- JSON: {"crop":[x,y,w,h]} normalized 0..1
  favorite INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'upload',        -- upload | folder | import | external
  folder_rel TEXT,                              -- watcher mirror key (source='folder')
  uploaded_by INTEGER REFERENCES users(id),
  created REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_photos_sha ON photos(sha256) WHERE sha256 IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder_rel) WHERE folder_rel IS NOT NULL;
CREATE TABLE IF NOT EXISTS tv_photos (
  tv_id INTEGER NOT NULL REFERENCES tvs(id) ON DELETE CASCADE,
  photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  content_id TEXT,
  status TEXT NOT NULL DEFAULT 'queued',        -- queued | on_tv | failed | removed
  error TEXT,
  matte TEXT,
  render_key TEXT,                              -- render pushed to the TV (stale => re-push)
  PRIMARY KEY (tv_id, photo_id)
);
CREATE INDEX IF NOT EXISTS idx_tvp_status ON tv_photos(status);
CREATE TABLE IF NOT EXISTS pending_tv_deletes (
  tv_id INTEGER NOT NULL REFERENCES tvs(id) ON DELETE CASCADE,
  content_id TEXT NOT NULL,
  PRIMARY KEY (tv_id, content_id)
);
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS photo_tags (
  photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (photo_id, tag_id)
);
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY,
  tv_id INTEGER NOT NULL REFERENCES tvs(id) ON DELETE CASCADE,
  mode TEXT NOT NULL DEFAULT 'random',          -- random | sequential | favorites
  interval_minutes INTEGER NOT NULL DEFAULT 60,
  time_start TEXT DEFAULT '',                   -- 'HH:MM' window (empty = always)
  time_end TEXT DEFAULT '',
  days TEXT NOT NULL DEFAULT '0123456',         -- 0=Mon..6=Sun, chars present = active
  tag TEXT DEFAULT '',                          -- optional tag filter
  enabled INTEGER NOT NULL DEFAULT 1,
  last_fired REAL NOT NULL DEFAULT 0,
  cursor INTEGER NOT NULL DEFAULT 0             -- sequential position
);
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
    ver = db.execute("PRAGMA user_version").fetchone()[0]
    if ver not in (0, SCHEMA_VERSION):
        raise SystemExit(
            f"framevalet.db is schema v{ver}; this build needs v{SCHEMA_VERSION}. "
            "Pre-release schema change: move the old data dir aside and start fresh.")
    db.executescript(SCHEMA)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    for stmt in ("ALTER TABLE photos ADD COLUMN matte TEXT",       # pre-release column adds
                 "ALTER TABLE users ADD COLUMN email TEXT",
                 "ALTER TABLE users ADD COLUMN sso INTEGER NOT NULL DEFAULT 0",
                 "ALTER TABLE users ADD COLUMN perms TEXT"):
        with __import__("contextlib").suppress(Exception):
            db.execute(stmt)
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE) "
               "WHERE email IS NOT NULL")
    # one-time: fold the old flag columns into the perms list
    db.execute("UPDATE users SET perms = TRIM(CASE WHEN can_upload THEN 'upload,' ELSE '' END || "
               "CASE WHEN can_delete_any THEN 'delete_any' ELSE '' END, ',') WHERE perms IS NULL")
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
