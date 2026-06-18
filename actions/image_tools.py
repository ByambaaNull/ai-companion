"""
image_tools.py — local image utilities via Pillow. Replaces TinyPNG / online
converters / resizers — all offline. HEIC input works if pillow-heif is present.

Each function returns {"ok", "path"/"error"} and writes to config.TOOLS_DIR.
"""

from __future__ import annotations

import logging
from pathlib import Path

import config

log = logging.getLogger(__name__)

# Optional HEIC/HEIF support (iPhone photos). No-op if the package isn't installed.
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:
    pass


def _out(src: Path, suffix: str, ext: str) -> Path:
    config.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.TOOLS_DIR / f"{src.stem}{suffix}.{ext.lstrip('.')}"
    i = 1
    while dest.exists():
        dest = config.TOOLS_DIR / f"{src.stem}{suffix}_{i}.{ext.lstrip('.')}"
        i += 1
    return dest


def _open(src: Path):
    from PIL import Image
    return Image.open(src)


def _flatten(img, ext: str):
    """JPEG has no alpha — paste onto white when saving to a non-alpha format."""
    if ext in ("jpg", "jpeg") and img.mode in ("RGBA", "LA", "P"):
        from PIL import Image
        bg = Image.new("RGB", img.size, (255, 255, 255))
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img


def compress(src_path: str, quality: int = 70) -> dict:
    """Shrink file size by re-saving at a lower quality (JPEG/WebP)."""
    src = Path(src_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}
    try:
        img = _open(src)
        ext = "webp" if src.suffix.lower() == ".webp" else "jpg"
        dest = _out(src, "_min", ext)
        out = _flatten(img, ext)
        out.save(dest, quality=int(quality), optimize=True)
        saved = max(0, src.stat().st_size - dest.stat().st_size)
        return {"ok": True, "path": str(dest), "saved_bytes": saved}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def convert(src_path: str, target_ext: str) -> dict:
    """Convert between png / jpg / webp / bmp / tiff (and HEIC input)."""
    src = Path(src_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}
    ext = target_ext.lstrip(".").lower()
    try:
        img = _open(src)
        out = _flatten(img, ext)
        dest = _out(src, "", ext)
        out.save(dest)
        return {"ok": True, "path": str(dest)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def resize(src_path: str, max_px: int = 1280) -> dict:
    """Scale so the longest side is max_px (keeps aspect ratio; never upscales)."""
    src = Path(src_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}
    try:
        from PIL import Image
        img = _open(src)
        img.thumbnail((int(max_px), int(max_px)), Image.LANCZOS)
        ext = (src.suffix.lstrip(".").lower() or "png")
        dest = _out(src, f"_{max_px}px", ext)
        _flatten(img, ext).save(dest)
        return {"ok": True, "path": str(dest), "size": list(img.size)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def watermark(src_path: str, text: str, opacity: int = 128) -> dict:
    """Tile faint diagonal text across the image (corner-anchored caption)."""
    src = Path(src_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}
    if not (text or "").strip():
        return {"ok": False, "error": "Enter watermark text."}
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = _open(src).convert("RGBA")
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        size = max(16, img.width // 24)
        try:
            font = ImageFont.truetype("arial.ttf", size)
        except Exception:
            font = ImageFont.load_default()
        margin = max(8, size // 2)
        try:
            tw = draw.textlength(text, font=font)
        except Exception:
            tw = size * len(text) * 0.5
        draw.text((img.width - tw - margin, img.height - size - margin), text,
                  fill=(255, 255, 255, int(opacity)), font=font)
        merged = Image.alpha_composite(img, layer)
        ext = "png"
        dest = _out(src, "_wm", ext)
        merged.save(dest)
        return {"ok": True, "path": str(dest)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
