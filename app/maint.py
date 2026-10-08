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
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger("framevalet.maint")

REPO = Path(__file__).resolve().parent.parent
BACKUP_DIR = config.DATA_DIR / "backups"
PENDING_DIR = config.DATA_DIR / "restore-pending"
KEEP_LOCAL = 7

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
         "latest": "", "error": ""}
    try:
        d["commit"] = _git("rev-parse", "--short", "HEAD")
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
    py = Path(sys.executable)
    _detached(f"framevalet-update-{int(time.time())}", (
        f"cd {REPO} && git pull -q --ff-only && {py.parent / 'pip'} install -q . "
        f"&& systemctl restart {unit}"))
    _update_cache["at"] = 0.0
    log.info("update started (origin/main)")


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
def make_backup() -> Path:
    """Write backups/framevalet-<stamp>.tar.gz and return its path. Safe while
    the app runs: the DB is copied through SQLite's online backup API."""
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
    for old in sorted(BACKUP_DIR.glob("framevalet-*.tar.gz"))[:-KEEP_LOCAL]:
        old.unlink(missing_ok=True)
    status.update(last_backup=time.time(), last_file=out.name)
    return out


def scheduled_backup(db):
    """Worker hook: local archive, then copy it (and sync originals) to the
    rclone remote if one is configured."""
    try:
        out = make_backup()
        remote = config.get(db, "backup_remote").strip()
        if remote and shutil.which("rclone"):
            for args in (["copy", str(out), f"{remote}/archives"],
                         ["sync", str(config.ORIGINALS_DIR), f"{remote}/originals"]):
                r = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=3600)
                if r.returncode != 0:
                    raise RuntimeError(f"rclone {args[0]}: {r.stderr.strip()[-300:]}")
        status["last_error"] = ""
        log.info("backup written: %s%s", out.name, f" and copied to {remote}" if remote else "")
    except Exception as e:
        status["last_error"] = str(e)[:300]
        log.warning("backup failed: %s", e)


# ------------------------------------------------------------------- restore
_ALLOWED_DIRS = ("tokens", "branding")


def stage_restore(data: bytes) -> dict:
    """Validate an uploaded archive and unpack it into restore-pending/.
    Only the exact members a backup contains are accepted."""
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
    return {"files": len(members)}


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
