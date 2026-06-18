"""
upscale.py — 4x AI image upscaler using Swin2SR (ONNX) on onnxruntime (CPU).
A free, local stand-in for Topaz / paid upscalers.

The ~53 MB model downloads once on first use (config.UPSCALE_ONNX_URL), then runs
offline. Large images are processed in overlapping tiles to bound memory; CPU
inference is thorough but slow, so progress is reported via on_event.
"""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

import config

log = logging.getLogger(__name__)

_session = None
_TILE = 160        # core tile size (input px)
_OVERLAP = 16      # context border discarded after upscaling (avoids seams)
_WHOLE_MAX = 256   # images this small are done in one pass


def is_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_model(on_event=None) -> Path:
    mp = config.UPSCALE_MODEL_PATH
    if mp.exists() and mp.stat().st_size > 1_000_000:
        return mp
    mp.parent.mkdir(parents=True, exist_ok=True)
    if on_event:
        on_event("Downloading upscaler model (~53 MB, one time)…")
    req = urllib.request.Request(config.UPSCALE_ONNX_URL,
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(mp, "wb") as f:
        f.write(r.read())
    return mp


def _get_session(on_event=None):
    global _session
    if _session is None:
        import onnxruntime as ort
        mp = _ensure_model(on_event)
        _session = ort.InferenceSession(str(mp), providers=["CPUExecutionProvider"])
    return _session


def _run_tile(sess, arr):
    """arr: HxWx3 float32 0..1 → upscaled HxWx3 (x4) float32 0..1."""
    import numpy as np
    h, w, _ = arr.shape
    ph, pw = (8 - h % 8) % 8, (8 - w % 8) % 8          # Swin2SR needs mult-of-8
    if ph or pw:
        arr = np.pad(arr, ((0, ph), (0, pw), (0, 0)), mode="edge")
    inp = arr.transpose(2, 0, 1)[None].astype(np.float32)
    out = sess.run(["reconstruction"], {"pixel_values": inp})[0][0]   # 3, 4Hp, 4Wp
    out = out.transpose(1, 2, 0)[: h * config.UPSCALE_SCALE, : w * config.UPSCALE_SCALE, :]
    return np.clip(out, 0.0, 1.0)


def upscale(image_path: str, on_event=None) -> dict:
    """Upscale an image 4x. Returns {ok, path, size}."""
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        return {"ok": False, "error": f"Missing dependency: {exc}"}

    src = Path(image_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}

    def emit(m):
        if on_event:
            try:
                on_event(m)
            except Exception:
                pass

    try:
        img = Image.open(src).convert("RGB")
        arr = np.asarray(img, dtype=np.float32) / 255.0
        H, W, _ = arr.shape
        emit("Loading model…")
        sess = _get_session(on_event)
        s = config.UPSCALE_SCALE

        if max(H, W) <= _WHOLE_MAX:
            emit("Upscaling…")
            out = _run_tile(sess, arr)
        else:
            out = np.zeros((H * s, W * s, 3), dtype=np.float32)
            tiles_y = (H + _TILE - 1) // _TILE
            tiles_x = (W + _TILE - 1) // _TILE
            total = tiles_y * tiles_x
            k = 0
            for y in range(0, H, _TILE):
                for x in range(0, W, _TILE):
                    k += 1
                    emit(f"Upscaling tile {k}/{total}…")
                    y0, x0 = max(0, y - _OVERLAP), max(0, x - _OVERLAP)
                    y1, x1 = min(H, y + _TILE + _OVERLAP), min(W, x + _TILE + _OVERLAP)
                    t = _run_tile(sess, arr[y0:y1, x0:x1])
                    yy1, xx1 = min(H, y + _TILE), min(W, x + _TILE)
                    ch, cw = (yy1 - y) * s, (xx1 - x) * s
                    cy0, cx0 = (y - y0) * s, (x - x0) * s
                    out[y * s:y * s + ch, x * s:x * s + cw] = t[cy0:cy0 + ch, cx0:cx0 + cw]

        res = (np.clip(out, 0.0, 1.0) * 255.0).astype("uint8")
        config.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.TOOLS_DIR / f"{src.stem}_{s}x.png"
        i = 1
        while dest.exists():
            dest = config.TOOLS_DIR / f"{src.stem}_{s}x_{i}.png"
            i += 1
        Image.fromarray(res).save(dest)
        emit("Done")
        return {"ok": True, "path": str(dest), "size": [res.shape[1], res.shape[0]]}
    except Exception as exc:
        log.error("upscale failed: %s", exc)
        return {"ok": False, "error": str(exc)}
