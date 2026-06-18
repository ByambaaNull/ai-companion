"""
actions/background_eraser.py — LOCAL, free background removal.

This replaces paid "remove background" web tools. It runs entirely on your
machine with rembg (an ONNX U^2-Net model). The model (~176 MB) is downloaded
once on first use, after which it works fully offline. Output is a transparent
PNG.

rembg is an OPTIONAL dependency — if it isn't installed we never crash; we
return a clear, actionable message instead (matching how the rest of the app
treats yt-dlp / ffmpeg / mpv).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from config import ERASER_OUTPUT_DIR, REMBG_MODEL

log = logging.getLogger(__name__)

# Supported input image types.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

# A rembg session is reused across calls so the (heavy) model loads only once.
_SESSION = None
_SESSION_MODEL = ""

_INSTALL_HINT = (
    "The background eraser needs the free, local 'rembg' package. Install it once "
    "with:  pip install rembg onnxruntime  — then try again. It works offline after "
    "the first run."
)


def is_available() -> bool:
    """True if rembg can be imported (the eraser is usable)."""
    try:
        import rembg  # noqa: F401  # type: ignore[import]
        return True
    except Exception:
        return False


def _get_session():
    """Lazily build (and cache) the rembg session for REMBG_MODEL."""
    global _SESSION, _SESSION_MODEL
    if _SESSION is not None and _SESSION_MODEL == REMBG_MODEL:
        return _SESSION
    from rembg import new_session  # type: ignore[import]
    _SESSION = new_session(REMBG_MODEL)
    _SESSION_MODEL = REMBG_MODEL
    return _SESSION


def _output_path(input_path: Path, output_path: str | None) -> Path:
    if output_path:
        out = Path(output_path)
    else:
        out = ERASER_OUTPUT_DIR / f"{input_path.stem}-nobg.png"
    # never silently overwrite a previous cutout — append a counter
    if out.exists():
        i = 2
        while True:
            candidate = out.with_name(f"{out.stem} ({i}){out.suffix}")
            if not candidate.exists():
                out = candidate
                break
            i += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def remove_background(
    input_path: str,
    output_path: str | None = None,
    on_event: Callable[[str], None] | None = None,
) -> dict:
    """
    Remove the background from *input_path* and save a transparent PNG.

    Returns {"ok": True, "input", "output"} on success, otherwise
    {"ok": False, "error": "..."} — never raises.
    """
    def _emit(msg: str) -> None:
        if on_event is None:
            return
        try:
            on_event(msg)
        except Exception:  # pragma: no cover
            pass

    src = (input_path or "").strip().strip('"').strip("'")
    if not src:
        return {"ok": False, "error": "No image given. Choose an image first."}
    p = Path(src)
    if not p.exists() or not p.is_file():
        return {"ok": False, "error": f"Image not found: {p}"}
    if p.suffix.lower() not in _IMAGE_EXTS:
        return {"ok": False,
                "error": f"Unsupported image type '{p.suffix}'. Use PNG, JPG or WEBP."}

    try:
        from rembg import remove  # type: ignore[import]
    except ImportError:
        return {"ok": False, "error": _INSTALL_HINT}
    except Exception as exc:  # e.g. onnxruntime / numpy ABI mismatch
        return {"ok": False, "error": f"Background eraser failed to load: {exc}. {_INSTALL_HINT}"}

    out = _output_path(p, output_path)
    _emit("Loading model…")
    try:
        session = _get_session()
        _emit("Removing background…")
        data = p.read_bytes()
        result = remove(data, session=session)
        out.write_bytes(result)
    except Exception as exc:
        log.error("background removal failed: %s", exc)
        return {"ok": False, "error": f"Background removal failed: {exc}"}

    log.info("background removed: %s → %s", p.name, out)
    _emit("Done")
    return {"ok": True, "input": str(p), "output": str(out)}


# ─── standalone smoke test ────────────────────────────────────────────────────

if __name__ == "__main__":
    import config
    config.setup_logging()
    log.info("background_eraser.py standalone test")
    log.info("rembg available: %s", is_available())
    log.info("model: %s", REMBG_MODEL)
    log.info("output dir: %s", ERASER_OUTPUT_DIR)
    log.info("background_eraser.py test done")
