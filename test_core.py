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
