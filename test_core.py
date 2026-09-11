"""Smallest checks that fail if the core logic breaks. Run: .venv/bin/python test_core.py"""
import io
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

# --- pipeline: HEIC in, oriented sRGB JPEG out, date captured
img = Image.new("RGB", (5000, 3000), (200, 120, 40))
buf = io.BytesIO()
heif = pillow_heif.from_pillow(img)
heif.save(buf, format="HEIF")
out_p, out_t = config.PROCESSED_DIR / "t.jpg", config.THUMBS_DIR / "t.jpg"
meta = pipeline.process(buf.getvalue(), out_p, out_t)
assert out_p.exists() and out_t.exists()
assert meta["width"] == 3600 and meta["height"] == 2160, meta  # fit into 2160 tall
assert Image.open(out_p).format == "JPEG"

# EXIF orientation is baked in: rotate-90 tag on a landscape becomes portrait pixels
img2 = Image.new("RGB", (400, 200), "white")
b2 = io.BytesIO()
exif = Image.Exif(); exif[274] = 6  # rotate 90 CW
exif[306] = "2019:07:04 12:00:00"
img2.save(b2, "JPEG", exif=exif)
m2 = pipeline.process(b2.getvalue(), config.PROCESSED_DIR / "o.jpg", config.THUMBS_DIR / "o.jpg")
assert (m2["width"], m2["height"]) == (200, 400), m2
assert m2["taken_date"] == "2019:07:04 12:00:00", m2

# small images are never upscaled
assert m2["width"] < 3840

# --- auth: create, verify, session round trip, permissions
auth.create_user(db, "kevin", "hunter22-long", role="admin", can_delete_any=True)
auth.create_user(db, "mike", "password-123")
assert auth.check_login(db, "kevin", "hunter22-long")["role"] == "admin"
assert auth.check_login(db, "kevin", "wrong") is None
mike = auth.check_login(db, "MIKE", "password-123")   # case-insensitive username
assert mike
photo_own = {"uploaded_by": mike["id"], "source": "upload"}
photo_other = {"uploaded_by": 999, "source": "upload"}
assert auth.can_delete(mike, photo_own)
assert not auth.can_delete(mike, photo_other)
admin = auth.check_login(db, "kevin", "hunter22-long")
assert auth.can_delete(admin, photo_other)

# --- config: env pins beat db writes
config.set(db, "tv_host", "10.0.0.5")
assert config.get(db, "tv_host") == "10.0.0.5"
os.environ["TV_HOST"] = "10.9.9.9"
assert config.get(db, "tv_host") == "10.9.9.9"
try:
    config.set(db, "tv_host", "x"); raise AssertionError("env pin not enforced")
except ValueError:
    pass
del os.environ["TV_HOST"]

# --- dedupe key stability
assert meta["sha256"] == pipeline.process(buf.getvalue(), config.PROCESSED_DIR / "d.jpg",
                                          config.THUMBS_DIR / "d.jpg")["sha256"]

print("all core checks passed")
