"""Maintenance: self-update, restart, backup, restore.

Update works on a git checkout run under systemd (the Proxmox installer): the
app launches the same four steps the installer prints, detached through
systemd-run so restarting the service doesn't kill the updater. Inside Docker
there is no checkout; the card says to pull a new image instead.

Backup is the small, irreplaceable part of the data dir: the SQLite database
(settings, users, library metadata), TV pairing tokens, and branding. It is a
tar.gz you can download, and optionally copied to an rclone remote on a
schedule together with an incremental sync of originals/. Renders and thumbs
are derived and rebuild themselves.

Restore stages the uploaded archive and applies it on the next start, before
the database is opened; the UI triggers that restart.
"""
import io
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

log = logging.getLogger("framevalet.maint")

def _find_repo() -> Path:
    """The git checkout to update. Under the installer the package is pip-installed
    into the venv, so __file__ points at site-packages; the checkout is the
    service's working directory (/opt/framevalet). FRAMEVALET_REPO overrides."""
    for c in (os.environ.get("FRAMEVALET_REPO"), Path(__file__).resolve().parent.parent,
              Path.cwd(), Path("/opt/framevalet")):
        if c and (Path(c) / ".git").exists() and (Path(c) / "pyproject.toml").exists():
            return Path(c)
    return Path(__file__).resolve().parent.parent


REPO = _find_repo()
BACKUP_DIR = config.DATA_DIR / "backups"
PENDING_DIR = config.DATA_DIR / "restore-pending"
PREVIOUS_FILE = config.DATA_DIR / "update-previous"   # sha we were on before the last update
KEEP_LOCAL = 7
MAGIC = b"FVB1"      # encrypted archive: MAGIC + 16B salt + 12B nonce + AES-256-GCM(tar.gz)

status = {"last_backup": 0.0, "last_error": "", "last_file": ""}


# -------------------------------------------------------------------- update
def _git(*args, timeout=20) -> str:
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-200:] or f"git {args[0]} failed")
    return r.stdout.strip()


def is_checkout() -> bool:
    return (REPO / ".git").exists() and bool(shutil.which("git"))


def can_self_manage() -> bool:
    return bool(shutil.which("systemd-run")) and os.environ.get("INVOCATION_ID") is not None


_update_cache = {"at": 0.0, "data": None}


def update_status(refresh=False) -> dict:
    """Current commit, and how far behind origin/main we are (fetch cached 10 min)."""
    if not is_checkout():
        return {"checkout": False, "docker": Path("/.dockerenv").exists()}
    if not refresh and _update_cache["data"] and time.time() - _update_cache["at"] < 600:
        return _update_cache["data"]
    d = {"checkout": True, "can_update": can_self_manage(), "commit": "", "behind": None,
         "latest": "", "error": "", "previous": ""}
    try:
        d["commit"] = _git("rev-parse", "--short", "HEAD")
        prev = PREVIOUS_FILE.read_text().strip() if PREVIOUS_FILE.exists() else ""
        if prev and not _git("rev-parse", "--short", "HEAD").startswith(prev[:7]):
            d["previous"] = prev[:7]
        d["date"] = _git("log", "-1", "--format=%cs")
        _git("fetch", "-q", "origin", "main", timeout=30)
        d["behind"] = int(_git("rev-list", "--count", "HEAD..origin/main"))
        d["latest"] = _git("log", "-1", "--format=%s", "origin/main")
    except Exception as e:   # offline, or not a clone of origin: still show the commit
        d["error"] = str(e)[:200]
    _update_cache.update(at=time.time(), data=d)
    return d


def _detached(unit: str, script: str):
    """Run a shell script as its own transient systemd unit so it outlives us."""
    subprocess.run(["systemd-run", "--unit", unit, "--collect", "--quiet",
                    "/bin/bash", "-c", script], check=True, timeout=20)


def start_update():
    if not (is_checkout() and can_self_manage()):
        raise RuntimeError("self-update needs a git checkout running under systemd")
    unit = os.environ.get("SYSTEMD_UNIT", "framevalet")
    pip = Path(sys.executable).parent / "pip"
    PREVIOUS_FILE.write_text(_git("rev-parse", "HEAD"))     # for Roll back
    _detached(f"framevalet-update-{int(time.time())}", (
        f"cd {REPO} && git checkout -q main && git pull -q --ff-only && {pip} install -q . "
        f"&& systemctl restart {unit}"))
    _update_cache["at"] = 0.0
    log.info("update started (origin/main)")


def rollback():
    """Check out the commit we were on before the last update (detached), so a
    bad release is one click away from undone. Update now moves forward again."""
    if not (is_checkout() and can_self_manage()):
        raise RuntimeError("rollback needs a git checkout running under systemd")
    prev = PREVIOUS_FILE.read_text().strip() if PREVIOUS_FILE.exists() else ""
    if not prev:
        raise RuntimeError("no previous version recorded")
    _git("cat-file", "-e", f"{prev}^{{commit}}")
    unit = os.environ.get("SYSTEMD_UNIT", "framevalet")
    pip = Path(sys.executable).parent / "pip"
    _detached(f"framevalet-rollback-{int(time.time())}", (
        f"cd {REPO} && git checkout -q {prev} && {pip} install -q . && systemctl restart {unit}"))
    _update_cache["at"] = 0.0
    log.warning("rollback to %s started", prev[:7])


def restart():
    """Restart under systemd via a detached unit; under Docker just exit and let
    the restart policy bring us back."""
    if can_self_manage():
        unit = os.environ.get("SYSTEMD_UNIT", "framevalet")
        _detached(f"framevalet-restart-{int(time.time())}", f"sleep 1 && systemctl restart {unit}")
    else:
        threading.Timer(1.0, lambda: os._exit(0)).start()
    log.info("restart requested")


# -------------------------------------------------------------------- backup
def _key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())


def encrypt(data: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    return MAGIC + salt + nonce + AESGCM(_key(passphrase, salt)).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC):
        return blob
    if not passphrase:
        raise ValueError("this backup is encrypted: enter its passphrase")
    salt, nonce, ct = blob[4:20], blob[20:32], blob[32:]
    try:
        return AESGCM(_key(passphrase, salt)).decrypt(nonce, ct, MAGIC)
    except Exception as e:
        raise ValueError("wrong passphrase (or corrupt archive)") from e


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def ping(url: str, ok: bool, msg: str = ""):
    """Uptime Kuma push semantics: a plain GET means up; status=down flags a
    failure. Other webhook receivers simply get a GET either way."""
    if not url:
        return
    try:
        q = {"status": "up" if ok else "down", "msg": (msg or "OK")[:200]}
        sep = "&" if "?" in url else "?"
        urllib.request.urlopen(f"{url}{sep}{urllib.parse.urlencode(q)}", timeout=10).read(64)
    except Exception as e:
        log.warning("backup ping failed: %s", str(e)[:200])


def make_backup(passphrase: str = "") -> Path:
    """Write backups/framevalet-<stamp>.tar.gz(.enc) and return its path. Safe
    while the app runs: the DB is copied through SQLite's online backup API."""
    import sqlite3
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"framevalet-{stamp}.tar.gz"
    tmp_db = BACKUP_DIR / f".db-{stamp}"
    src = sqlite3.connect(config.DB_PATH)
    try:
        dst = sqlite3.connect(tmp_db)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    try:
        with tarfile.open(out, "w:gz") as tar:
            tar.add(tmp_db, arcname="framevalet.db")
            for sub in ("tokens", "branding"):
                d = config.DATA_DIR / sub
                if d.is_dir():
                    for f in sorted(d.iterdir()):
                        if f.is_file():
                            tar.add(f, arcname=f"{sub}/{f.name}")
            meta = json.dumps({"created": time.time(), "stamp": stamp,
                               "note": "framevalet backup: db, TV tokens, branding"}).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size, ti.mtime = len(meta), int(time.time())
            tar.addfile(ti, io.BytesIO(meta))
    finally:
        tmp_db.unlink(missing_ok=True)
    if passphrase:
        enc = out.with_suffix(out.suffix + ".enc")
        enc.write_bytes(encrypt(out.read_bytes(), passphrase))
        out.unlink()
        out = enc
    for old in sorted(BACKUP_DIR.glob("framevalet-*.tar.gz*"))[:-KEEP_LOCAL]:
        old.unlink(missing_ok=True)
    status.update(last_backup=time.time(), last_file=out.name)
    return out


def list_backups() -> list[str]:
    if not BACKUP_DIR.is_dir():
        return []
    return sorted((f.name for f in BACKUP_DIR.glob("framevalet-*.tar.gz*")), reverse=True)


def scheduled_backup(db):
    """Worker hook: local archive, then copy it (and sync originals) to the
    rclone remote if one is configured. Pings the health URL either way."""
    url = config.get(db, "backup_ping_url").strip()
    try:
        out = make_backup(config.get(db, "backup_passphrase"))
        remote = config.get(db, "backup_remote").strip()
        if remote and shutil.which("rclone"):
            for args in (["copy", str(out), f"{remote}/archives"],
                         ["sync", str(config.ORIGINALS_DIR), f"{remote}/originals"]):
                r = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=3600)
                if r.returncode != 0:
                    raise RuntimeError(f"rclone {args[0]}: {r.stderr.strip()[-300:]}")
        status["last_error"] = ""
        log.info("backup written: %s%s", out.name, f" and copied to {remote}" if remote else "")
        ping(url, True, out.name)
    except Exception as e:
        status["last_error"] = str(e)[:300]
        log.warning("backup failed: %s", e)
        ping(url, False, str(e))


# ------------------------------------------------------------------- restore
_ALLOWED_DIRS = ("tokens", "branding")


def stage_restore(data: bytes, passphrase: str = "") -> dict:
    """Validate an uploaded archive and unpack it into restore-pending/.
    Only the exact members a backup contains are accepted, and the database
    inside must match this build's schema (update first, then restore)."""
    data = decrypt(data, passphrase)
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as e:
        raise ValueError(f"not a backup archive: {e}") from e
    members = []
    with tar:
        for m in tar.getmembers():
            if not m.isfile():
                raise ValueError(f"unexpected entry in archive: {m.name}")
            parts = Path(m.name).parts
            ok = (m.name in ("framevalet.db", "manifest.json")
                  or (len(parts) == 2 and parts[0] in _ALLOWED_DIRS and ".." not in parts))
            if not ok:
                raise ValueError(f"unexpected file in archive: {m.name}")
            members.append(m)
        if not any(m.name == "framevalet.db" for m in members):
            raise ValueError("archive has no framevalet.db")
        if PENDING_DIR.exists():
            shutil.rmtree(PENDING_DIR)
        PENDING_DIR.mkdir(parents=True)
        for m in members:
            dest = PENDING_DIR / m.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
    try:
        _check_schema(PENDING_DIR / "framevalet.db")
    except ValueError:
        shutil.rmtree(PENDING_DIR, ignore_errors=True)
        raise
    return {"files": len(members)}


def _check_schema(db_path: Path):
    import sqlite3
    from . import db as dbm
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        con.execute("SELECT 1 FROM users LIMIT 1")
        con.close()
    except sqlite3.Error as e:
        raise ValueError(f"archive's database is not a framevalet database: {e}") from e
    if ver > dbm.SCHEMA_VERSION:
        raise ValueError(f"backup is from a newer framevalet (schema v{ver}, this build is "
                         f"v{dbm.SCHEMA_VERSION}): update first, then restore")
    if ver not in (0, dbm.SCHEMA_VERSION):
        raise ValueError(f"backup schema v{ver} is older than this build supports")


def apply_pending_restore():
    """Called before the database is opened on startup."""
    if not PENDING_DIR.is_dir():
        return
    log.warning("applying staged restore from %s", PENDING_DIR)
    for suffix in ("", "-wal", "-shm"):
        Path(str(config.DB_PATH) + suffix).unlink(missing_ok=True)
    shutil.move(PENDING_DIR / "framevalet.db", config.DB_PATH)
    for sub in _ALLOWED_DIRS:
        src = PENDING_DIR / sub
        if src.is_dir():
            dst = config.DATA_DIR / sub
            dst.mkdir(exist_ok=True)
            for f in src.iterdir():
                shutil.move(f, dst / f.name)
    shutil.rmtree(PENDING_DIR, ignore_errors=True)
    log.warning("restore applied")
