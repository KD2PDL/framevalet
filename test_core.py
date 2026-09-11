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
auth.create_user(db, "kevin", "hunter22-long", role="admin", can_delete_any=True)
auth.create_user(db, "mike", "password-123")
assert auth.check_login(db, "kevin", "hunter22-long")["role"] == "admin"
assert auth.check_login(db, "kevin", "wrong") is None
mike = auth.check_login(db, "MIKE", "password-123")
assert mike
assert auth.can_delete(mike, {"uploaded_by": mike["id"], "source": "upload"})
assert not auth.can_delete(mike, {"uploaded_by": 999, "source": "upload"})
admin = auth.check_login(db, "kevin", "hunter22-long")
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
