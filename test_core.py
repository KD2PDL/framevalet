"""Smallest checks that fail if the core logic breaks. Run: .venv/bin/python test_core.py"""
import io
import json
import os
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="fv-test-")

from PIL import Image
import pillow_heif

from app import auth, config, db as dbm, pipeline

config.ensure_dirs()
dbm.init()
db = dbm.connect()

# --- ingest: HEIC in, normalized sRGB original + thumb out, full res kept
img = Image.new("RGB", (5000, 3000), (200, 120, 40))
buf = io.BytesIO()
pillow_heif.from_pillow(img).save(buf, format="HEIF")
orig, thumb = config.ORIGINALS_DIR / "t.jpg", config.THUMBS_DIR / "t.jpg"
meta = pipeline.ingest(buf.getvalue(), orig, thumb)
assert orig.exists() and thumb.exists()
assert (meta["width"], meta["height"]) == (5000, 3000), meta   # originals keep full res

# EXIF orientation baked in + date captured
img2 = Image.new("RGB", (400, 200), "white")
b2 = io.BytesIO()
exif = Image.Exif(); exif[274] = 6; exif[306] = "2019:07:04 12:00:00"
img2.save(b2, "JPEG", exif=exif)
m2 = pipeline.ingest(b2.getvalue(), config.ORIGINALS_DIR / "o.jpg",
                     config.THUMBS_DIR / "o.jpg")
assert (m2["width"], m2["height"]) == (200, 400), m2
assert m2["taken_date"] == "2019:07:04 12:00:00", m2

# --- render: fit never upscales; 4k downscales; crop applies before resize
out = config.RENDERS_DIR / "r1.jpg"
pipeline.render(orig, out, None, "fit", "4k", 90, False)
r = Image.open(out); assert (r.width, r.height) == (3600, 2160), r.size
edits = json.dumps({"crop": [0.25, 0.25, 0.5, 0.5]})   # center half
pipeline.render(orig, config.RENDERS_DIR / "r2.jpg", edits, "fit", "4k", 90, False)
r2 = Image.open(config.RENDERS_DIR / "r2.jpg")
assert (r2.width, r2.height) == (2500, 1500), r2.size  # 2500x1500 < 4k: no upscale
pipeline.render(orig, config.RENDERS_DIR / "r3.jpg", None, "fit", "1080p", 90, False)
assert Image.open(config.RENDERS_DIR / "r3.jpg").height == 1080

# blurfill: portrait becomes full-bleed 16:9 with photo intact in center
imgp = Image.new("RGB", (1000, 1600), (10, 120, 60))
bp = io.BytesIO(); imgp.save(bp, "JPEG")
mp = pipeline.ingest(bp.getvalue(), config.ORIGINALS_DIR / "p.jpg",
                     config.THUMBS_DIR / "p.jpg")
pipeline.render(config.ORIGINALS_DIR / "p.jpg", config.RENDERS_DIR / "r4.jpg",
                None, "blurfill", "4k", 90, True)
r4 = Image.open(config.RENDERS_DIR / "r4.jpg")
assert (r4.width, r4.height) == (3840, 2160), r4.size

# render_key changes with any input that changes pixels
k1 = pipeline.render_key("sha", None, "fit", "4k", 90, False)
assert k1 != pipeline.render_key("sha", edits, "fit", "4k", 90, False)
assert k1 != pipeline.render_key("sha", None, "blurfill", "4k", 90, False)
assert k1 != pipeline.render_key("sha", None, "fit", "1080p", 90, False)
assert k1 == pipeline.render_key("sha", None, "fit", "4k", 90, False)

# --- auth: create, verify, permissions
auth.create_user(db, "alice", "s3cure-pass-1", role="admin", can_delete_any=True)
auth.create_user(db, "bob", "s3cure-pass-2")
assert auth.check_login(db, "alice", "s3cure-pass-1")["role"] == "admin"
assert auth.check_login(db, "alice", "wrong") is None
bob = auth.check_login(db, "BOB", "s3cure-pass-2")
assert bob
assert auth.can_delete(bob, {"uploaded_by": bob["id"], "source": "upload"})
assert not auth.can_delete(bob, {"uploaded_by": 999, "source": "upload"})
admin = auth.check_login(db, "alice", "s3cure-pass-1")
assert auth.can_delete(admin, {"uploaded_by": 999, "source": "upload"})

# --- config: env pins beat db writes
config.set(db, "jpeg_quality", "80")
assert config.get(db, "jpeg_quality") == "80"
os.environ["JPEG_QUALITY"] = "95"
assert config.get(db, "jpeg_quality") == "95"
try:
    config.set(db, "jpeg_quality", "x"); raise AssertionError("env pin not enforced")
except ValueError:
    pass
del os.environ["JPEG_QUALITY"]

# --- worker: ingest_bytes dedupe + auto-assign to TVs
from app import worker
db.execute("INSERT INTO tvs(name, host, created) VALUES('Demo','192.0.2.1',0)")
db.commit()
pid = worker.ingest_bytes(db, buf.getvalue(), filename="x.heic", user_id=1)
assert pid is not None
assert worker.ingest_bytes(db, buf.getvalue(), filename="x2.heic") is None  # dup
row = db.execute("SELECT status FROM tv_photos WHERE photo_id=?", (pid,)).fetchone()
assert row and row["status"] == "queued"

print("all core checks passed")

# --- audit regressions (2026-09-11) -----------------------------------------
# matte type validation helper is in routes; test the pure validation shape here
from app import config as _cfg
_cfg.set(db, "jpeg_quality", "90")
try:
    _cfg.set(db, "jpeg_quality", "not-a-number"); raise AssertionError("int validation missing")
except ValueError:
    pass
try:
    _cfg.set(db, "brand_accent", "red; }body{}"); raise AssertionError("accent validation missing")
except ValueError:
    pass
# login rate limiter is keyed on a value, not header-spoofable (unit-level)
from app import auth as _auth
_auth._login_fails.clear()
for _ in range(_auth.LOGIN_MAX):
    _auth.note_login_failure("1.2.3.4")
assert _auth.rate_limited("1.2.3.4")
assert not _auth.rate_limited("5.6.7.8")   # a different client is unaffected
print("audit regressions passed")

# --- watcher is non-destructive: missing files never delete (2026-09-11) -----
import tempfile as _tf
_wroot = config.WATCH_DIR
for _i in range(3):
    _f = _wroot / f"w{_i}.jpg"
    Image.new("RGB", (300, 200), (_i * 40, 100, 120)).save(_f)
    worker.ingest_file(db, _f, f"w{_i}.jpg")
_n0 = db.execute("SELECT COUNT(*) c FROM photos WHERE source='folder'").fetchone()["c"]
assert _n0 == 3, _n0
for _f in _wroot.glob("w*.jpg"):
    _f.unlink()                      # empty the folder entirely
worker._scan_watch_folder(db)
_n1 = db.execute("SELECT COUNT(*) c FROM photos WHERE source='folder'").fetchone()["c"]
assert _n1 == 3, f"watcher deleted photos on empty folder: {_n0} -> {_n1}"
print("watcher non-destructive regression passed")

# --- watcher dedup: rename / copy never duplicates (checksum) (2026-09-11) ----
import shutil as _sh
_wr = config.WATCH_DIR
for _f in _wr.glob("*.jpg"):
    _f.unlink()
db.execute("DELETE FROM photos WHERE source='folder'"); db.commit()
_a = _wr / "trip.jpg"
Image.new("RGB", (500, 350), (160, 80, 30)).save(_a)
worker._scan_watch_folder(db)
assert db.execute("SELECT COUNT(*) c FROM photos WHERE source='folder'").fetchone()["c"] == 1
os.rename(_a, _wr / "trip-renamed.jpg")          # rename = same bytes, new name
worker._scan_watch_folder(db)
_c = db.execute("SELECT COUNT(*) c FROM photos WHERE source='folder'").fetchone()["c"]
assert _c == 1, f"rename created a duplicate: {_c}"
assert db.execute("SELECT folder_rel FROM photos WHERE source='folder'").fetchone()["folder_rel"] \
    == "trip-renamed.jpg"                          # tracked path followed the rename
_sh.copy(_wr / "trip-renamed.jpg", _wr / "trip-copy.jpg")   # byte-identical copy
worker._scan_watch_folder(db)
_c2 = db.execute("SELECT COUNT(*) c FROM photos WHERE source='folder'").fetchone()["c"]
assert _c2 == 1, f"copy created a duplicate: {_c2}"
print("watcher rename/copy dedup regression passed")


# --- Cloudflare Access: signature/audience/issuer gate, then JIT provisioning
import time as _time
import jwt as _jwt
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
from app import cloudflare

config.set(db, "cf_access_team", "acme")                 # bare team name is expanded
assert cloudflare.team_domain(db) == "acme.cloudflareaccess.com"
config.set(db, "cf_access_aud", "aud-123")
_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
_other = _rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJWKS:                      # stands in for PyJWKClient (no network)
    class _K:
        key = _key.public_key()
    def get_signing_key_from_jwt(self, token):
        return self._K()


cloudflare._jwks["acme.cloudflareaccess.com"] = _FakeJWKS()


def _tok(signer=_key, **claims):
    base = {"iss": "https://acme.cloudflareaccess.com", "aud": ["aud-123"],
            "iat": int(_time.time()), "exp": int(_time.time()) + 300}
    return _jwt.encode(base | claims, signer, algorithm="RS256")


assert cloudflare.verify(db, _tok(email="Ann@Example.com"))["email"] == "Ann@Example.com"
assert cloudflare.verify(db, _tok(signer=_other, email="x@y")) is None      # wrong key
assert cloudflare.verify(db, _tok(aud=["other"], email="x@y")) is None      # wrong app
assert cloudflare.verify(db, _tok(iss="https://evil.cloudflareaccess.com", email="x@y")) is None
assert cloudflare.verify(db, _tok(exp=int(_time.time()) - 10, email="x@y")) is None
assert cloudflare.verify(db, "not.a.jwt") is None

# provisioning: username from the local part, case-insensitive email match
auth.create_user(db, "ann", "localpassword", role="member")          # a local "ann" exists
u = auth.sso_user(db, {"email": "Ann@Example.com"})
assert u["username"] == "ann@example.com" and u["sso"] == 1 and u["role"] == "member", dict(u)
assert auth.sso_user(db, {"email": "ANN@example.com"})["id"] == u["id"]   # no duplicate
u2 = auth.sso_user(db, {"email": "dave@example.com"})
assert u2["username"] == "dave" and u2["sso"] == 1
assert auth.check_login(db, "dave", "") is None                       # no usable password
config.set(db, "cf_access_autoprovision", "false")
assert auth.sso_user(db, {"email": "carol@example.com"}) is None      # provisioning off
# service tokens: common_name must match an existing username
assert auth.sso_user(db, {"common_name": "ann"})["username"] == "ann"
assert auth.sso_user(db, {"common_name": "nobody"}) is None

# the request-level gate: header must verify, disabled users stay out
class _Req:
    def __init__(self, headers=None, cookies=None):
        self.headers, self.cookies = headers or {}, cookies or {}
        self.state = type("S", (), {})()
assert auth.user_from_request(_Req({"cf-access-jwt-assertion": _tok(email="dave@example.com")}), db)["username"] == "dave"
assert auth.user_from_request(_Req({"cf-access-jwt-assertion": _tok(signer=_other, email="dave@example.com")}), db) is None
db.execute("UPDATE users SET disabled=1 WHERE username='dave'"); db.commit()
assert auth.user_from_request(_Req({"cf-access-jwt-assertion": _tok(email="dave@example.com")}), db) is None
config.set(db, "cf_access_aud", "")                                   # Access off: header ignored
assert auth.user_from_request(_Req({"cf-access-jwt-assertion": _tok(email="ann@example.com")}), db) is None

# tunnel supervisor: log lines drive the status, token never hits argv
t = cloudflare.Tunnel()
t._observe("2026-10-08T20:00:00Z INF Registered tunnel connection connIndex=0")
t._observe("2026-10-08T20:00:01Z INF Registered tunnel connection connIndex=1")
assert t.state["connections"] == 2
t._observe("2026-10-08T20:00:02Z ERR Failed to dial a quic connection error=\"timeout\"")
assert t.state["error"].startswith("Failed to dial")
t._observe("2026-10-08T20:00:03Z INF Unregistered tunnel connection connIndex=1")
assert t.state["connections"] == 1
print("cloudflare: ok")


# --- backup/restore round trip; restore rejects anything but a backup's own files
from app import maint
(config.TOKENS_DIR / "tv1.txt").write_text("tok-abc")
out = maint.make_backup()
assert out.exists() and out.name.startswith("framevalet-")
import tarfile as _tf
names = sorted(_tf.open(out).getnames())
assert "framevalet.db" in names and "tokens/tv1.txt" in names and "manifest.json" in names, names
# a copy of the db inside the archive is a real, openable sqlite db
import sqlite3 as _sq
with _tf.open(out) as t, open(config.DATA_DIR / "x.db", "wb") as f:
    f.write(t.extractfile("framevalet.db").read())
assert _sq.connect(config.DATA_DIR / "x.db").execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 2
# staging + applying puts the files back where they belong
(config.TOKENS_DIR / "tv1.txt").write_text("changed")
assert maint.stage_restore(out.read_bytes())["files"] == len(names)
assert maint.PENDING_DIR.is_dir()
db.close()
maint.apply_pending_restore()
assert not maint.PENDING_DIR.exists()
assert (config.TOKENS_DIR / "tv1.txt").read_text() == "tok-abc"
db = dbm.connect()
assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 2
# hostile archives: traversal, stray files, non-archives
import io as _io
def _evil(name):
    b = _io.BytesIO()
    with _tf.open(fileobj=b, mode="w:gz") as t:
        ti = _tf.TarInfo(name); ti.size = 1; t.addfile(ti, _io.BytesIO(b"x"))
    return b.getvalue()
for bad in ("../etc/passwd", "tokens/../../x", "originals/a.jpg", "framevalet.db/../x"):
    try:
        maint.stage_restore(_evil(bad)); raise AssertionError(bad)
    except ValueError:
        pass
try:
    maint.stage_restore(b"not a tar"); raise AssertionError("garbage accepted")
except ValueError:
    pass
# status without a checkout or systemd never claims it can self-update
st = maint.update_status()
assert "checkout" in st and (not st["checkout"] or st.get("can_update") in (True, False))
print("maint: ok")


# --- encrypted backups, passphrase handling, schema guard, rollback marker
enc = maint.make_backup("hunter2-correct-horse")
assert enc.name.endswith(".tar.gz.enc") and maint.is_encrypted(enc.read_bytes())
assert maint.decrypt(enc.read_bytes(), "hunter2-correct-horse")[:2] == b"\x1f\x8b"   # gzip inside
for bad in ("", "wrong"):
    try:
        maint.stage_restore(enc.read_bytes(), bad); raise AssertionError("accepted " + repr(bad))
    except ValueError as e:
        assert "passphrase" in str(e), e
assert maint.stage_restore(enc.read_bytes(), "hunter2-correct-horse")["files"] >= 3
import shutil as _sh; _sh.rmtree(maint.PENDING_DIR)
assert enc.name in maint.list_backups() and maint.list_backups()[0] == enc.name
# a backup from a newer schema is refused before anything is staged
plain = maint.make_backup()
tmp = config.DATA_DIR / "newer.db"; _sh.copy(config.DB_PATH, tmp)
c = _sq.connect(tmp); c.execute("PRAGMA user_version = 99"); c.commit(); c.close()
b = _io.BytesIO()
with _tf.open(fileobj=b, mode="w:gz") as t:
    t.add(tmp, arcname="framevalet.db")
try:
    maint.stage_restore(b.getvalue()); raise AssertionError("newer schema accepted")
except ValueError as e:
    assert "update first" in str(e), e
assert not maint.PENDING_DIR.exists()
# ping: failure carries status=down; never raises
import http.server, threading as _th
hits = []
class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self): hits.append(self.path); self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), _H); _th.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/api/push/abc?ping="
maint.ping(url, True, "x.tar.gz"); maint.ping(url, False, "rclone copy: boom")
assert "status=up" in hits[0] and "status=down" in hits[1] and "boom" in hits[1], hits
maint.ping("http://127.0.0.1:1/", False)                                   # unreachable: no raise
# rollback marker: previous sha surfaces only when HEAD moved on
if maint.is_checkout():
    prev = maint._git("rev-parse", "HEAD~1")
    maint.PREVIOUS_FILE.write_text(prev)
    st = maint.update_status(refresh=True)
    assert st["previous"] == maint._describe(prev) and st["previous"], st
    maint.PREVIOUS_FILE.write_text("0000000deadbeef")          # stale sha: ignored, no error
    assert maint.update_status(refresh=True)["previous"] == ""
    maint.PREVIOUS_FILE.unlink()
print("maint extras: ok")


# --- logs: rotating file on disk, runtime level, zip bundle
import logging as _lg
from app import logbuf
logbuf.install()
logbuf.set_level("INFO")
_lg.getLogger("framevalet.test").debug("hidden-debug-line")
_lg.getLogger("framevalet.test").warning("visible-warning-line")
logbuf.set_level("DEBUG")
_lg.getLogger("framevalet.test").debug("now-visible-debug-line")
logbuf._file_handler.flush()
txt = logbuf.LOG_FILE.read_text()
assert "visible-warning-line" in txt and "now-visible-debug-line" in txt and "hidden-debug-line" not in txt
assert logbuf.current_level() == "DEBUG"
try:
    logbuf.set_level("LOUD"); raise AssertionError("bad level accepted")
except ValueError:
    pass
assert logbuf.files()[0]["name"] == "framevalet.log"
import zipfile as _zf
assert "framevalet.log" in _zf.ZipFile(_io.BytesIO(logbuf.zip_all())).namelist()
logbuf.set_level("INFO")
print("logs: ok")


# --- versions: describe() maps tags to vX.Y.Z / vX.Y.Z+N, falls back to sha
v = maint._describe("HEAD")
assert v.startswith("v") or len(v) >= 7, v
assert maint.package_version().startswith("v") or maint.package_version() == ""
config.set(db, "auto_update", "true"); assert config.get(db, "auto_update") == "true"
maint.auto_update()                                   # not under systemd here: must be a no-op
print("versions: ok")


# --- permissions: migration from old flags, can(), gates, editor guards
from fastapi import HTTPException as _HE
from app import routes as _routes
# old-style rows got perms folded in at init; ann was created with upload
assert auth.can(db.execute("SELECT * FROM users WHERE username='ann'").fetchone(), "upload")
alice = db.execute("SELECT * FROM users WHERE username='alice'").fetchone()        # admin
assert auth.perms_of(alice) == set(auth.PERMS)
auth.create_user(db, "tvguy", "s3cure-pass-9", perms=["tvs", "logs"])
tvguy = db.execute("SELECT * FROM users WHERE username='tvguy'").fetchone()
assert auth.can(tvguy, "tvs") and auth.can(tvguy, "logs") and not auth.can(tvguy, "users") and not auth.can(tvguy, "upload")
assert tvguy["can_upload"] == 0                                                     # legacy column kept in sync
try:
    auth.require("users")(user=tvguy); raise AssertionError("gate let tvguy manage users")
except _HE as e:
    assert e.status_code == 403
assert auth.require_any_admin(user=tvguy)["username"] == "tvguy"                   # can open Admin (logs)
try:
    auth.require_any_admin(user=db.execute("SELECT * FROM users WHERE username='ann'").fetchone()); raise AssertionError()
except _HE:
    pass
# editor: non-admin with 'users' cannot grant admin; last admin cannot be demoted
auth.set_perms(db, tvguy["id"], ["users"])
tvguy = db.execute("SELECT * FROM users WHERE id=?", (tvguy["id"],)).fetchone()
try:
    _routes.edit_user(tvguy["id"], action="perms", role="admin", perms=["upload"], db=db, user=tvguy); raise AssertionError("self-promotion")
except _HE as e:
    assert e.status_code == 403
db.execute("UPDATE users SET role='member' WHERE role='admin' AND id!=?", (alice["id"],)); db.commit()
try:
    _routes.edit_user(alice["id"], action="perms", role="", perms=["upload"], db=db, user=alice); raise AssertionError("demoted last admin")
except _HE as e:
    assert e.status_code == 400
_routes.edit_user(tvguy["id"], action="perms", role="admin", perms=[], db=db, user=alice)
assert db.execute("SELECT role FROM users WHERE id=?", (tvguy["id"],)).fetchone()["role"] == "admin"
_routes.edit_user(tvguy["id"], action="perms", role="", perms=["upload", "tvs"], db=db, user=alice)
tvguy = db.execute("SELECT * FROM users WHERE id=?", (tvguy["id"],)).fetchone()
assert tvguy["role"] == "member" and auth.perms_of(tvguy) == {"upload", "tvs"}
# tunnel self-cut guard
class _R:
    def __init__(self, h): self.headers = h
assert _routes._via_tunnel(_R({"cf-connecting-ip": "1.2.3.4"})) and not _routes._via_tunnel(_R({}))
print("perms: ok")


# --- discovery fallback: subnet sweep finds an open 8001 and skips closed ones
import socket as _sock
import ipaddress
from app import discovery
srv = _sock.socket(); srv.bind(("127.0.0.1", 8001)); srv.listen(1)
try:
    assert discovery._port_open("127.0.0.1") == "127.0.0.1"
finally:
    srv.close()
assert discovery._port_open("127.0.0.1") is None            # closed now
assert discovery.sweep(ipaddress.ip_network("127.0.0.0/30")) == set()
assert discovery.sweep(ipaddress.ip_network("10.0.0.0/8")) == set()   # too big: refused
print("discovery: ok")


# --- pairing without a token: marker counts as paired, connection sends no token
from app import tvservice
row = {"id": 77, "host": "192.0.2.10", "client_name": "framevalet"}
svc = tvservice.TVService(row)
assert not svc.has_token()
config.token_path(77).write_text(tvservice.NO_TOKEN)
assert svc.has_token() and not svc.token_issued()
svc._art()                                      # constructor only, no network
assert svc._tv.token_file is None and svc._tv.token is None
svc.reset()
config.token_path(77).write_text("real-token-123")
svc = tvservice.TVService(row); svc._art()
assert svc.token_issued() and svc._tv.token_file.endswith("tv77.txt")
svc.reset()
print("pairing: ok")


# --- import: phase 1 adopts everything at once; phase 2 thumbnails with a breaker
import asyncio as _aio
from app import worker as _w
db.execute("INSERT INTO tvs(name, host, created) VALUES('t','192.0.2.9',?)", (dbm.now(),)); db.commit()
tvid = db.execute("SELECT id FROM tvs WHERE host='192.0.2.9'").fetchone()["id"]
cur = db.execute("INSERT INTO photos(filename, source, created) VALUES('MY_F0001','external',?)", (dbm.now(),))
db.execute("INSERT INTO tv_photos(tv_id, photo_id, content_id, status) VALUES(?,?,'MY_F0001','on_tv')", (tvid, cur.lastrowid)); db.commit()
class _FakeArt:
    def __init__(self, ok): self.ok = ok
    def get_thumbnail(self, cid):
        raise RuntimeError("single API unsupported here")          # forces the batch path
    def get_thumbnail_list(self, cids):
        if not self.ok: raise RuntimeError("firmware says no")
        b = _io.BytesIO(); Image.new("RGB", (40, 20), "red").save(b, "JPEG"); return {cids[0] + ".jpg": bytearray(b.getvalue())}
class _FakeSvc:
    ok = True
    def __init__(self, row): self.timeout = 30
    def my_photos(self): return [{"content_id": "MY_F0001", "width": 10, "height": 5, "image_date": "2024:01:01 00:00:00"},
                                 {"content_id": "MY_F0001"}, {"content_id": "MY_F0002", "width": 8, "height": 4},
                                 {"content_id": "MY_F0003"}, {"content_id": "MY_F0004"}]
    def _call(self, fn, attempts=1, deadline=None): return fn(_FakeArt(self.ok))
    def reset(self): pass
_orig = _w.TVService; _w.TVService = _FakeSvc
try:
    _aio.run(_w._import_from_tv(tvid))
    st = dict(_w.import_state)
    assert st["error"] == "" and st["total"] == 4 and st["done"] == 4 and st["thumbs"] == 4, st
    rows = {r["filename"]: r for r in db.execute("SELECT p.* FROM photos p JOIN tv_photos tp ON tp.photo_id=p.id WHERE tp.tv_id=?", (tvid,))}
    assert rows["MY_F0001"]["source"] == "import" and rows["MY_F0001"]["thumb_path"] and rows["MY_F0001"]["width"] == 10
    assert len(rows) == 4
    # breaker: a TV that never serves thumbnails still gets its photos adopted, quickly
    db.execute("UPDATE photos SET thumb_path=NULL WHERE id IN (SELECT photo_id FROM tv_photos WHERE tv_id=?)", (tvid,)); db.commit()
    _FakeSvc.ok = False
    _aio.run(_w._import_from_tv(tvid))
    st = dict(_w.import_state)
    assert st["thumbs"] == 0 and "thumbnails unavailable" in st["error"] and "firmware says no" in st["error"], st
finally:
    _w.TVService = _orig
print("import: ok")


# --- deadline: a call that never returns is cut off and reported, socket closed
import threading as _thr
svc = tvservice.TVService({"id": 78, "host": "192.0.2.11", "client_name": "framevalet"})
class _StuckArt:
    closed = False
    def close(self): self.closed = True
svc._art_client = _StuckArt(); svc._tv = type("T", (), {"close": lambda self: None})()
stuck = _StuckArt()
def hang(a):
    while not a.closed: _time.sleep(0.05)
    raise RuntimeError("socket closed")
t0 = _time.time()
try:
    svc._call(hang, attempts=1, deadline=0.5); raise AssertionError("no timeout")
except tvservice.TVError as e:
    assert "no answer within" in str(e), e
assert _time.time() - t0 < 3
print("deadline: ok")


# --- rotate + crop: rotation first, crop in the rotated frame; attach gives a TV photo an original
img = Image.new("RGB", (400, 200), "white"); img.paste((255, 0, 0), (0, 0, 200, 200))   # left half red
bb = _io.BytesIO(); img.save(bb, "JPEG", quality=95)
src = config.ORIGINALS_DIR / "rot.jpg"; src.write_bytes(bb.getvalue())
out = config.RENDERS_DIR / "rot90.jpg"
pipeline.render(src, out, json.dumps({"rotate": 90}), "fit", "1080p", 90, False)
r = Image.open(out); assert (r.width, r.height) == (200, 400), r.size
assert r.getpixel((100, 50))[1] < 80 and r.getpixel((100, 350))[1] > 200           # red on top (clockwise), white below
pipeline.render(src, out, json.dumps({"rotate": 90, "crop": [0, 0.5, 1, 0.5]}), "fit", "1080p", 90, False)
r = Image.open(out); assert (r.width, r.height) == (200, 200) and r.getpixel((100, 100))[1] > 200   # bottom half: white
pipeline.rotated_original(src, config.THUMBS_DIR / "rot_x.jpg", 270)
assert Image.open(config.THUMBS_DIR / "rot_x.jpg").size == (200, 400)
# rotate route: accumulates, clears crop; crop route keeps rotate
pid = db.execute("INSERT INTO photos(filename, orig_path, sha256, created) VALUES('r','%s','shaR',?)" % src, (dbm.now(),)).lastrowid; db.commit()
_routes.save_crop(pid, {"crop": [0.1, 0.1, 0.5, 0.5]}, db=db, user=alice)
_routes.rotate_photo(pid, {"deg": 90}, db=db, user=alice)
e = json.loads(db.execute("SELECT edits FROM photos WHERE id=?", (pid,)).fetchone()["edits"])
assert e == {"rotate": 90}, e
_routes.save_crop(pid, {"crop": [0, 0, 0.5, 0.5]}, db=db, user=alice)
e = json.loads(db.execute("SELECT edits FROM photos WHERE id=?", (pid,)).fetchone()["edits"])
assert e["rotate"] == 90 and e["crop"] == [0, 0, 0.5, 0.5], e
_routes.rotate_photo(pid, {"deg": -90}, db=db, user=alice)
assert db.execute("SELECT edits FROM photos WHERE id=?", (pid,)).fetchone()["edits"] is None   # back to 0, no crop
# attach original to an imported photo
tvrow = db.execute("SELECT * FROM tvs WHERE host='192.0.2.9'").fetchone()
ph = db.execute("SELECT p.* FROM photos p JOIN tv_photos tp ON tp.photo_id=p.id WHERE tp.tv_id=? AND tp.content_id='MY_F0002'", (tvrow["id"],)).fetchone()
assert ph["orig_path"] is None
res = _w.attach_original(db, tvrow["id"], "MY_F0002", bb.getvalue())
assert res["already"] is False
ph = db.execute("SELECT * FROM photos WHERE id=?", (ph["id"],)).fetchone()
assert ph["orig_path"] and Path(ph["orig_path"]).is_file() and ph["width"] == 400 and ph["source"] == "import"
assert _w.attach_original(db, tvrow["id"], "MY_F0002", bb.getvalue())["already"] is True
rk = db.execute("SELECT render_key FROM tv_photos WHERE tv_id=? AND photo_id=?", (tvrow["id"], ph["id"])).fetchone()["render_key"]
assert rk, "attached photo should count as already pushed"
try:
    _w.attach_original(db, tvrow["id"], "MY_F0003", bb.getvalue()); raise AssertionError("duplicate accepted")
except ValueError as e2:
    assert "already photo" in str(e2)
try:
    _w.attach_original(db, tvrow["id"], "MY_NOPE", b"x"); raise AssertionError("unknown id accepted")
except ValueError:
    pass
print("rotate+attach: ok")


# --- import status text follows the phase
assert _w.import_text({"running": False}) == ""
assert _w.import_text({"running": True, "phase": "photos", "done": 3, "total": 9}) == "adopting photos 3/9"
assert _w.import_text({"running": True, "phase": "thumbnails", "thumbs": 554, "thumbs_total": 1211}) == "fetching thumbnails 554/1211"
print("status text: ok")


# --- attach route end-to-end over HTTP (catches route-level import/wiring errors)
from fastapi.testclient import TestClient
from app.main import app as _app
with TestClient(_app) as c:
    c.post("/login", data={"username": "alice", "password": "s3cure-pass-1"}, headers={"Origin": "http://testserver"})
    r = c.post(f"/tvs/{tvrow['id']}/attach/MY_F0004", files={"file": ("x.jpg", bb.getvalue(), "image/jpeg")}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    r = c.post(f"/tvs/{tvrow['id']}/attach/MY_NOPE", files={"file": ("x.jpg", bb.getvalue(), "image/jpeg")}, headers={"Origin": "http://testserver"})
    assert r.status_code == 400, r.text
print("attach route: ok")
