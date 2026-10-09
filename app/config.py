"""Configuration with a strict precedence: environment > database > default.

App-wide settings live here. Per-TV settings (host, matte, resolution, art mode
controls) live on the tvs table; the TV_* env vars seed the FIRST TV on a fresh
boot so headless Docker deploys still work.
"""
import os
import re as _re
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
ORIGINALS_DIR = DATA_DIR / "originals"
RENDERS_DIR = DATA_DIR / "renders"      # derived, cache-keyed, safe to delete
THUMBS_DIR = DATA_DIR / "thumbs"
BRAND_DIR = DATA_DIR / "branding"
TOKENS_DIR = DATA_DIR / "tokens"        # one token file per TV
WATCH_DIR = DATA_DIR / "watch"          # folder watcher root (rclone syncs here)
DB_PATH = DATA_DIR / "framevalet.db"

# key -> (env var, default). App-wide only.
SETTINGS = {
    "log_level":        ("LOG_LEVEL", "INFO"),         # DEBUG | INFO | WARNING | ERROR
    "default_style":    ("DEFAULT_STYLE", "fit"),      # fit | blurfill
    "jpeg_quality":     ("JPEG_QUALITY", "90"),
    "unsharp":          ("UNSHARP", "false"),          # subtle sharpen on renders
    "brand_name":       ("BRAND_NAME", "framevalet"),
    "brand_accent":     ("BRAND_ACCENT", "#b8892f"),
    "brand_logo":       ("BRAND_LOGO", ""),            # filename under data/branding/
    "reconcile_minutes": ("RECONCILE_MINUTES", "15"),
    "watch_enabled":    ("WATCH_ENABLED", "false"),
    "watch_interval":   ("WATCH_INTERVAL", "120"),     # seconds between folder scans
    "rclone_remote":    ("RCLONE_REMOTE", ""),         # e.g. onedrive:Frame TV Photos
    "rclone_interval":  ("RCLONE_INTERVAL", "600"),    # seconds between rclone syncs
    "unsplash_key":     ("UNSPLASH_KEY", ""),
    "pexels_key":       ("PEXELS_KEY", ""),
    "pixabay_key":      ("PIXABAY_KEY", ""),
    "nasa_key":         ("NASA_KEY", "DEMO_KEY"),
    "rijksmuseum_key":  ("RIJKSMUSEUM_KEY", ""),
    # Cloudflare Tunnel + Access (see cloudflare.py). Dashboard does the rest.
    "cf_tunnel_token":  ("CF_TUNNEL_TOKEN", ""),
    "cf_tunnel_autostart": ("CF_TUNNEL_AUTOSTART", "true"),
    "cf_access_team":   ("CF_ACCESS_TEAM_DOMAIN", ""),  # e.g. acme.cloudflareaccess.com
    "cf_access_aud":    ("CF_ACCESS_AUD", ""),          # application audience tag
    "cf_access_autoprovision": ("CF_ACCESS_AUTO_PROVISION", "true"),
    "cf_access_default_role": ("CF_ACCESS_DEFAULT_ROLE", "member"),
    "auto_update":      ("AUTO_UPDATE", "false"),      # hourly check; pull + restart when behind
    # Scheduled backups (see maint.py). Local archives are always kept (last 7).
    "backup_interval":  ("BACKUP_INTERVAL", "24"),      # hours; 0 = off
    "backup_remote":    ("BACKUP_REMOTE", ""),          # e.g. onedrive:FrameValet Backups
    "backup_passphrase": ("BACKUP_PASSPHRASE", ""),     # set => archives are AES-256-GCM encrypted
    "backup_ping_url":  ("BACKUP_PING_URL", ""),        # Uptime Kuma push URL (or any GET hook)
}
# Never echoed back to the browser; the UI shows "(set)" and a Clear button.
SECRET_KEYS = {"cf_tunnel_token", "backup_passphrase", "unsplash_key", "pexels_key", "pixabay_key",
               "nasa_key", "rijksmuseum_key"}

# Seed values for the first TV on an empty database (Docker-friendly).
SEED_TV_HOST = os.environ.get("TV_HOST", "")
SEED_TV_NAME = os.environ.get("TV_NAME", "Frame TV")
SEED_TV_MAC = os.environ.get("TV_MAC", "")
SEED_TV_CLIENT = os.environ.get("TV_CLIENT_NAME", "framevalet")
SEED_TV_TOKEN = os.environ.get("TV_TOKEN", "")

APP_SECRET = os.environ.get("APP_SECRET", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
BOOTSTRAP_ADMIN_USER = os.environ.get("ADMIN_USER", "")
BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8470"))
HOST = os.environ.get("HOST", "0.0.0.0")


def ensure_dirs():
    for d in (DATA_DIR, ORIGINALS_DIR, RENDERS_DIR, THUMBS_DIR, BRAND_DIR,
              TOKENS_DIR, WATCH_DIR):
        d.mkdir(parents=True, exist_ok=True)


def token_path(tv_id: int) -> Path:
    return TOKENS_DIR / f"tv{tv_id}.txt"


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


_HEX_COLOR = _re.compile(r"^#[0-9a-fA-F]{3,8}$")


def set(db, key: str, value: str):
    if env_pinned(key):
        raise ValueError(f"{key} is pinned by environment variable {SETTINGS[key][0]}")
    if key == "brand_accent" and value and not _HEX_COLOR.match(value):
        raise ValueError("accent must be a hex color like #b8892f")
    if key in ("rclone_remote", "backup_remote") and value.startswith("-"):
        raise ValueError(f"{key} cannot start with '-'")
    if key == "backup_ping_url" and value and not value.startswith(("http://", "https://")):
        raise ValueError("ping URL must start with http:// or https://")
    if key == "backup_interval":
        if not value.isdigit():
            raise ValueError("backup interval must be a whole number of hours (0 = off)")
    if key == "log_level" and value.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError("log level must be DEBUG, INFO, WARNING or ERROR")
    if key == "cf_access_default_role" and value not in ("member", "admin"):
        raise ValueError("default role must be member or admin")
    if key == "cf_access_team" and not _re.match(r"^(https?://)?[a-z0-9.-]*/?$", value, _re.I):
        raise ValueError("team domain looks wrong (expected like acme.cloudflareaccess.com)")
    if key in ("jpeg_quality", "reconcile_minutes", "watch_interval", "rclone_interval"):
        try:
            n = int(value)
        except ValueError:
            raise ValueError(f"{key} must be an integer")
        if n < 1 or (key == "jpeg_quality" and n > 100):
            raise ValueError(f"{key} is out of range")
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def all_settings(db) -> dict:
    return {k: {"value": get(db, k), "env_pinned": env_pinned(k), "env_var": SETTINGS[k][0],
                "secret": k in SECRET_KEYS}
            for k in SETTINGS}
