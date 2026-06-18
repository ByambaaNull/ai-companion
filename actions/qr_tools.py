"""
qr_tools.py — generate and scan QR codes / barcodes locally (qrcode + pyzbar).
Replaces paid QR generators / scanners. Outputs to config.TOOLS_DIR.
"""

from __future__ import annotations

import logging
from pathlib import Path

import config

log = logging.getLogger(__name__)


def _out(name: str, ext: str) -> Path:
    config.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.TOOLS_DIR / f"{name}.{ext}"
    i = 1
    while dest.exists():
        dest = config.TOOLS_DIR / f"{name}_{i}.{ext}"
        i += 1
    return dest


def generate(text: str) -> dict:
    """Make a QR code PNG from text / a URL."""
    if not (text or "").strip():
        return {"ok": False, "error": "Enter text or a URL."}
    try:
        import qrcode
        img = qrcode.make(text)
        dest = _out("qr", "png")
        img.save(dest)
        return {"ok": True, "path": str(dest)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def scan(image_path: str) -> dict:
    """Decode any QR codes / barcodes found in an image."""
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
        results = decode(Image.open(image_path))
        if not results:
            return {"ok": True, "results": [], "text": "No code found in that image."}
        items = [{"type": r.type, "data": r.data.decode("utf-8", "replace")}
                 for r in results]
        joined = "\n".join(f"[{it['type']}] {it['data']}" for it in items)
        return {"ok": True, "results": items, "text": joined}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
