"""
pdf_tools.py — local PDF toolkit (pypdf + PyMuPDF). Replaces Acrobat / Smallpdf
for the everyday operations. All offline; outputs go to config.TOOLS_DIR.

Every function returns {"ok", "path"/"error", ...}. "is_dir": True marks results
that are a folder of files (split / pdf→images).
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


def _out_dir(name: str) -> Path:
    base = config.TOOLS_DIR / name
    d = base
    i = 1
    while d.exists():
        d = Path(f"{base}_{i}")
        i += 1
    d.mkdir(parents=True, exist_ok=True)
    return d


def merge(paths: list[str]) -> dict:
    if not paths or len(paths) < 2:
        return {"ok": False, "error": "Choose at least two PDFs to merge."}
    try:
        from pypdf import PdfWriter
        w = PdfWriter()
        for p in paths:
            w.append(p)
        dest = _out("merged", "pdf")
        with open(dest, "wb") as f:
            w.write(f)
        return {"ok": True, "path": str(dest)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def split_all(path: str) -> dict:
    """One PDF per page, into a folder."""
    try:
        from pypdf import PdfReader, PdfWriter
        src = Path(path)
        r = PdfReader(path)
        out = _out_dir(src.stem + "_pages")
        for i, page in enumerate(r.pages, 1):
            w = PdfWriter()
            w.add_page(page)
            with open(out / f"{src.stem}_p{i}.pdf", "wb") as f:
                w.write(f)
        return {"ok": True, "path": str(out), "count": len(r.pages), "is_dir": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def images_to_pdf(paths: list[str]) -> dict:
    if not paths:
        return {"ok": False, "error": "Choose one or more images."}
    try:
        from PIL import Image
        imgs = [Image.open(p).convert("RGB") for p in paths]
        dest = _out("images", "pdf")
        imgs[0].save(dest, save_all=True, append_images=imgs[1:])
        return {"ok": True, "path": str(dest)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def pdf_to_images(path: str, dpi: int = 150) -> dict:
    try:
        import fitz  # PyMuPDF
        src = Path(path)
        doc = fitz.open(path)
        out = _out_dir(src.stem + "_images")
        n = 0
        for i, page in enumerate(doc, 1):
            page.get_pixmap(dpi=dpi).save(str(out / f"{src.stem}_p{i}.png"))
            n += 1
        doc.close()
        return {"ok": True, "path": str(out), "count": n, "is_dir": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def compress(path: str) -> dict:
    try:
        import fitz
        src = Path(path)
        doc = fitz.open(path)
        dest = _out(src.stem + "_compressed", "pdf")
        doc.save(str(dest), garbage=4, deflate=True, clean=True)
        doc.close()
        saved = max(0, src.stat().st_size - dest.stat().st_size)
        return {"ok": True, "path": str(dest), "saved_bytes": saved}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def rotate(path: str, degrees: int = 90) -> dict:
    try:
        from pypdf import PdfReader, PdfWriter
        src = Path(path)
        r = PdfReader(path)
        w = PdfWriter()
        for page in r.pages:
            page.rotate(int(degrees))
            w.add_page(page)
        dest = _out(src.stem + "_rotated", "pdf")
        with open(dest, "wb") as f:
            w.write(f)
        return {"ok": True, "path": str(dest)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def unlock(path: str, password: str) -> dict:
    """Remove a password you know, writing an unencrypted copy."""
    try:
        from pypdf import PdfReader, PdfWriter
        src = Path(path)
        r = PdfReader(path)
        if r.is_encrypted:
            if not r.decrypt(password or ""):
                return {"ok": False, "error": "Wrong password."}
        w = PdfWriter()
        for page in r.pages:
            w.add_page(page)
        dest = _out(src.stem + "_unlocked", "pdf")
        with open(dest, "wb") as f:
            w.write(f)
        return {"ok": True, "path": str(dest)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def extract_text(path: str) -> dict:
    try:
        import fitz
        src = Path(path)
        doc = fitz.open(path)
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        dest = _out(src.stem + "_text", "txt")
        dest.write_text(text, encoding="utf-8")
        return {"ok": True, "path": str(dest), "chars": len(text)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
