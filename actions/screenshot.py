"""
actions/screenshot.py — Capture the screen and optionally save or encode it.

Uses pyautogui for capture. Optionally encodes to base64 for LLM vision input.
Saved images go to data/temp/ and are NOT auto-deleted (caller decides).
"""

from __future__ import annotations

import base64
import logging
import uuid
from pathlib import Path

import config
from config import TEMP_DIR

log = logging.getLogger(__name__)


def take_screenshot(region: tuple[int, int, int, int] | None = None) -> Path:
    """
    Capture the screen and save to a temp PNG file.

    Args:
        region: (left, top, width, height) for partial capture.
                None = full screen.

    Returns:
        Path to the saved PNG file.
    """
    try:
        import pyautogui
    except ImportError as exc:
        raise RuntimeError("pyautogui not installed: pip install pyautogui") from exc

    uid = uuid.uuid4().hex[:8]
    out_path = TEMP_DIR / f"screenshot_{uid}.png"

    log.debug("Taking screenshot%s", f" region={region}" if region else " (full)")
    try:
        screenshot = pyautogui.screenshot(region=region)
        screenshot.save(str(out_path))
        log.info("Screenshot saved: %s (%dx%d)", out_path.name,
                 screenshot.width, screenshot.height)
        return out_path
    except Exception as exc:
        log.error("Screenshot failed: %s", exc)
        raise


def screenshot_to_base64(
    region: tuple[int, int, int, int] | None = None,
    delete_after: bool = True,
) -> str:
    """
    Capture screen and return as a base64-encoded PNG string.

    Suitable for passing to an LLM vision endpoint (e.g. Ollama llava model).

    Args:
        region: Optional capture region.
        delete_after: If True, deletes the temp file after encoding.

    Returns:
        Base64-encoded PNG string (no data: prefix).
    """
    path = take_screenshot(region=region)
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        log.debug("Encoded screenshot to base64 (%d bytes)", len(encoded))
        return encoded
    finally:
        if delete_after:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    config.setup_logging()
    log.info("Screenshot action test")

    # Full screen
    p = take_screenshot()
    log.info("Full screenshot saved: %s", p)

    # Partial (top-left quarter)
    import pyautogui
    screen_w, screen_h = pyautogui.size()
    p2 = take_screenshot(region=(0, 0, screen_w // 2, screen_h // 2))
    log.info("Partial screenshot saved: %s", p2)

    # Base64 (auto-deleted)
    b64 = screenshot_to_base64(delete_after=True)
    log.info("Base64 length: %d chars", len(b64))
    log.info("Screenshot tests passed ✓")
