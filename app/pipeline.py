"""Ingestion pipeline: decode anything a phone or laptop can throw at us,
normalize, and emit a TV-ready baseline JPEG plus a thumbnail.

Every upload passes through here, no exceptions:
  1. decode (HEIC/HEIF/AVIF via pillow-heif; JPEG/PNG/TIFF/WebP/BMP/GIF via Pillow)
  2. EXIF orientation baked into pixels (the Frame ignores orientation metadata)
  3. ICC -> sRGB (iPhone HEIC is Display-P3; skipping this washes out on the TV)
  4. fit within 3840x2160, never upscale (Lanczos)
  5. baseline JPEG, optimized Huffman (the Frame dislikes progressive JPEG)
  6. capture DateTimeOriginal before stripping all other metadata
"""
import hashlib
import io
from datetime import datetime
from pathlib import Path

import pillow_heif
from PIL import Image, ImageCms, ImageOps

pillow_heif.register_heif_opener()
if hasattr(pillow_heif, "register_avif_opener"):  # AVIF split out of newer releases
    pillow_heif.register_avif_opener()

TV_W, TV_H = 3840, 2160
THUMB = 480
ACCEPTED = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".avif", ".tif", ".tiff",
            ".webp", ".bmp", ".gif"}

_SRGB = ImageCms.createProfile("sRGB")


class PipelineError(Exception):
    pass


def _to_srgb(img: Image.Image, icc: bytes | None) -> Image.Image:
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            img = ImageCms.profileToProfile(img, src, _SRGB, outputMode="RGB")
        except Exception:
            img = img.convert("RGB")
    return img.convert("RGB") if img.mode != "RGB" else img


def _taken_date(img: Image.Image) -> str | None:
    try:
        exif = img.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal | DateTime
        if raw:
            datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")  # validate TV format
            return str(raw)
    except Exception:
        pass
    return None


def process(data: bytes, out_proc: Path, out_thumb: Path, jpeg_quality: int = 85) -> dict:
    """Returns {sha256, width, height, bytes, taken_date}. Raises PipelineError."""
    sha = hashlib.sha256(data).hexdigest()
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise PipelineError(f"could not decode image: {e}") from e

    taken = _taken_date(img)
    icc = img.info.get("icc_profile")           # capture BEFORE transpose (it can drop ICC)
    img = ImageOps.exif_transpose(img)          # bake orientation into pixels
    img = _to_srgb(img, icc)
    if img.width > TV_W or img.height > TV_H:   # fit, never upscale
        img.thumbnail((TV_W, TV_H), Image.LANCZOS)

    img.save(out_proc, "JPEG", quality=jpeg_quality, optimize=True, progressive=False)

    t = img.copy()
    t.thumbnail((THUMB, THUMB), Image.LANCZOS)
    t.save(out_thumb, "JPEG", quality=75, optimize=True)

    return {"sha256": sha, "width": img.width, "height": img.height,
            "bytes": out_proc.stat().st_size, "taken_date": taken}


def make_thumb_from_bytes(data: bytes, out_thumb: Path):
    """Thumbnail for photos adopted from the TV (we only get its thumbnail stream)."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((THUMB, THUMB), Image.LANCZOS)
    img.save(out_thumb, "JPEG", quality=75, optimize=True)
