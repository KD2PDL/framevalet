"""Two-stage pipeline, originals as source of truth.

INGEST (once, at upload): decode anything, bake EXIF orientation into pixels,
convert to sRGB, capture the taken date, store a normalized full-resolution
original (lossless-ish JPEG q97 or original bytes if already a JPEG needing no
fixes) plus a thumbnail. No downscaling here — crops happen later.

RENDER (at push time, cached): original + edits (crop) + style -> TV-ready
JPEG at 4K or 1080p. Cache key covers everything that affects pixels, so
re-pushes are free and a crop change is exactly one re-render.
"""
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path

import pillow_heif
from PIL import Image, ImageCms, ImageFilter, ImageOps

pillow_heif.register_heif_opener()
if hasattr(pillow_heif, "register_avif_opener"):  # AVIF split out of newer releases
    pillow_heif.register_avif_opener()

RES = {"4k": (3840, 2160), "1080p": (1920, 1080)}
THUMB = 480
ACCEPTED = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".avif", ".tif", ".tiff",
            ".webp", ".bmp", ".gif"}

Image.MAX_IMAGE_PIXELS = 120_000_000   # ~120 Mpx; bomb guard for low-RAM hosts

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
            datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
            return str(raw)
    except Exception:
        pass
    return None


def ingest(data: bytes, out_orig: Path, out_thumb: Path) -> dict:
    """Normalize and store the original. Returns {sha256, width, height, taken_date}."""
    sha = hashlib.sha256(data).hexdigest()
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise PipelineError(f"could not decode image: {e}") from e

    taken = _taken_date(img)
    icc = img.info.get("icc_profile")           # capture BEFORE transpose (it can drop ICC)
    img = ImageOps.exif_transpose(img)
    img = _to_srgb(img, icc)

    # Normalized original: full resolution, near-lossless. EXIF/GPS never written.
    img.save(out_orig, "JPEG", quality=97, optimize=True, progressive=False)

    t = img.copy()
    t.thumbnail((THUMB, THUMB), Image.LANCZOS)
    t.save(out_thumb, "JPEG", quality=75, optimize=True)

    return {"sha256": sha, "width": img.width, "height": img.height, "taken_date": taken}


def render_key(sha: str, edits: str | None, style: str, res: str,
               quality: int, unsharp: bool) -> str:
    basis = json.dumps([sha, edits or "", style, res, quality, unsharp])
    return hashlib.sha1(basis.encode()).hexdigest()


def apply_edits(img, edits: str | None):
    """Rotate (clockwise degrees), then crop (normalized coords on the rotated
    image). The editor shows the rotated original, so crop boxes are drawn in
    that frame."""
    if not edits:
        return img
    e = json.loads(edits)
    rot = int(e.get("rotate") or 0) % 360
    if rot:
        img = img.rotate(-rot, expand=True)     # PIL rotates counter-clockwise
    crop = e.get("crop")
    if crop:
        x, y, w, h = crop
        box = (round(x * img.width), round(y * img.height),
               round((x + w) * img.width), round((y + h) * img.height))
        if box[2] - box[0] >= 16 and box[3] - box[1] >= 16:
            img = img.crop(box)
    return img


def render(orig_path: Path, out_path: Path, edits: str | None, style: str,
           res: str, quality: int, unsharp: bool):
    """Original -> TV-ready JPEG. Rotate + crop first, then style."""
    img = apply_edits(Image.open(orig_path).convert("RGB"), edits)

    tw, th = RES.get(res, RES["4k"])
    if style == "blurfill" and img.width / img.height < 1.3:
        # portrait/square: blurred desaturated cover background, sharp photo centered
        bg = img.copy()
        scale = max(tw / bg.width, th / bg.height)
        bg = bg.resize((round(bg.width * scale), round(bg.height * scale)), Image.LANCZOS)
        bg = bg.crop(((bg.width - tw) // 2, (bg.height - th) // 2,
                      (bg.width - tw) // 2 + tw, (bg.height - th) // 2 + th))
        bg = bg.filter(ImageFilter.GaussianBlur(40))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (128, 128, 128)), 0.3)
        fg = img.copy()
        fg.thumbnail((tw, th), Image.LANCZOS)
        bg.paste(fg, ((tw - fg.width) // 2, (th - fg.height) // 2))
        img = bg
    else:
        if img.width > tw or img.height > th:   # fit, never upscale
            img.thumbnail((tw, th), Image.LANCZOS)

    if unsharp:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=30, threshold=3))

    img.save(out_path, "JPEG", quality=quality, optimize=True, progressive=False)


def render_preview(orig_path: Path, out_path: Path, edits: str | None, longest=1600):
    """Just the cropped photo pixels (no fit-letterbox, no matte) so the browser
    editor can place it on a CSS matte stage. Mirrors the crop math in render()."""
    img = apply_edits(Image.open(orig_path).convert("RGB"), edits)
    if max(img.width, img.height) > longest:
        img.thumbnail((longest, longest), Image.LANCZOS)
    img.save(out_path, "JPEG", quality=88, optimize=True)


def preview_key(sha: str, edits: str | None) -> str:
    return hashlib.sha1(json.dumps([sha, edits or "", "preview"]).encode()).hexdigest()


def make_thumb_from_bytes(data: bytes, out_thumb: Path):
    """Thumbnail for photos adopted from the TV (we only get its thumbnail stream)."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((THUMB, THUMB), Image.LANCZOS)
    img.save(out_thumb, "JPEG", quality=75, optimize=True)


def rotated_original(orig_path: Path, out_path: Path, rot: int):
    """Full-size rotated copy for the crop editor's stage (cached by caller)."""
    img = Image.open(orig_path).convert("RGB").rotate(-(rot % 360), expand=True)
    img.save(out_path, "JPEG", quality=92, optimize=True)
