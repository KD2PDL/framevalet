"""Configuration with a strict precedence: environment > database > default.

Anything settable in the admin panel can also be pinned by an env var (12-factor,
Docker-friendly). Env-pinned keys are read-only in the UI and flagged as such so
the panel never lies about why a field won't save.
"""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
ORIGINALS_DIR = DATA_DIR / "originals"
PROCESSED_DIR = DATA_DIR / "processed"
THUMBS_DIR = DATA_DIR / "thumbs"
BRAND_DIR = DATA_DIR / "branding"
DB_PATH = DATA_DIR / "framevalet.db"
TOKEN_PATH = DATA_DIR / "tv-token.txt"

# key -> (env var, default). Everything else (users, photos) lives only in the DB.
SETTINGS = {
    "tv_host":          ("TV_HOST", ""),
    "tv_client_name":   ("TV_CLIENT_NAME", "framevalet"),
    "tv_mac":           ("TV_MAC", ""),            # optional, for Wake-on-LAN
    "default_matte":    ("DEFAULT_MATTE", "flexible_antique"),
    "jpeg_quality":     ("JPEG_QUALITY", "85"),
    "keep_originals":   ("KEEP_ORIGINALS", "true"),
    "brand_name":       ("BRAND_NAME", "framevalet"),
    "brand_accent":     ("BRAND_ACCENT", "#2e9688"),
    "brand_logo":       ("BRAND_LOGO", ""),        # filename under data/branding/
    "reconcile_minutes": ("RECONCILE_MINUTES", "15"),
}

APP_SECRET = os.environ.get("APP_SECRET", "")  # generated into DB on first boot if empty
BOOTSTRAP_ADMIN_USER = os.environ.get("ADMIN_USER", "")
BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8470"))
HOST = os.environ.get("HOST", "0.0.0.0")


def ensure_dirs():
    for d in (DATA_DIR, ORIGINALS_DIR, PROCESSED_DIR, THUMBS_DIR, BRAND_DIR):
        d.mkdir(parents=True, exist_ok=True)


def env_pinned(key: str) -> bool:
    env, _ = SETTINGS[key]
    return bool(os.environ.get(env))


def get(db, key: str) -> str:
    env, default = SETTINGS[key]
    v = os.environ.get(env)
    if v:
        return v
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set(db, key: str, value: str):
    if env_pinned(key):
        raise ValueError(f"{key} is pinned by environment variable {SETTINGS[key][0]}")
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def all_settings(db) -> dict:
    return {k: {"value": get(db, k), "env_pinned": env_pinned(k), "env_var": SETTINGS[k][0]}
            for k in SETTINGS}
