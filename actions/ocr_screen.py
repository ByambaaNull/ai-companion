"""
actions/ocr_screen.py — Extract all visible text from the screen via vision LLM.

Captures the screen with PIL.ImageGrab (param "region" → centre half of the
screen, anything else → full screen), sends it to the same vision-capable
provider screen_watcher.py uses (GitHub Models, OpenAI content-array format),
and copies the extracted text to the clipboard.

Usage (via LLM actions):
    ocr_screen |              ← full screen
    ocr_screen | region       ← centre half only (sharper on dense text)
"""

from __future__ import annotations

import asyncio
import base64
import logging
from io import BytesIO

import httpx

import config

log = logging.getLogger(__name__)

_OCR_PROMPT = ("Extract ALL text visible in this image, preserving layout. "
               "Output text only.")

_DISPLAY_LIMIT = 1500   # chars shown in chat; clipboard gets the full text
_MAX_WIDTH = 1600       # keep resolution high enough for small UI text


def _capture(region: bool) -> str | None:
    """Grab screen → base64 JPEG. Blocking — run in executor."""
    try:
        from PIL import ImageGrab, Image
    except ImportError:
        return None

    try:
        img = ImageGrab.grab()
        if region:
            w, h = img.size
            img = img.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4))
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.width > _MAX_WIDTH:
            ratio = _MAX_WIDTH / img.width
            img = img.resize((_MAX_WIDTH, int(img.height * ratio)),
                             Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        log.error("ocr_screen capture failed: %s", exc)
        return None


async def _query_vision(jpeg_b64: str) -> str | None:
    """Same provider + content-array pattern as screen_watcher.py."""
    payload = {
        "model": config.GITHUB_GPT_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _OCR_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{jpeg_b64}",
                    "detail": "high",
                }},
            ],
        }],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 2048,
    }
    headers = {
        "Authorization": f"Bearer {config.GITHUB_API_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(config.GITHUB_API_URL, json=payload,
                                 headers=headers)
        if resp.status_code == 429:
            return "__RATE_LIMITED__"
        resp.raise_for_status()
        choices = resp.json().get("choices", [])
        if not choices:
            return None
        return choices[0].get("message", {}).get("content", "").strip()


async def ocr_screen(param: str = "") -> str:
    """OCR the screen. param: "region" for centre half, else full screen."""
    try:
        if not config.GITHUB_API_KEY:
            return ("Screen OCR needs a GitHub Models API key (vision). Add "
                    "GITHUB_TOKEN in .env or the API keys tab in settings.")

        region = "region" in param.strip().lower()
        loop = asyncio.get_running_loop()
        jpeg_b64 = await loop.run_in_executor(None, _capture, region)
        if jpeg_b64 is None:
            return ("Couldn't capture the screen — pip install pillow to "
                    "enable screen capture.")

        try:
            text = await _query_vision(jpeg_b64)
        except Exception as exc:
            log.warning("ocr_screen vision call failed: %s", exc)
            return f"The vision service didn't respond: {exc}"

        if text == "__RATE_LIMITED__":
            return "Vision API is rate-limited right now — try again in a minute."
        if not text:
            return "I couldn't read any text from the screen."

        clip_note = ""
        try:
            import pyperclip
            pyperclip.copy(text)  # full text, not truncated
            clip_note = "\n\n(Full text copied to clipboard.)"
        except Exception as exc:
            log.warning("ocr_screen clipboard copy failed: %s", exc)

        shown = text
        if len(shown) > _DISPLAY_LIMIT:
            shown = shown[:_DISPLAY_LIMIT] + \
                f"\n… [{len(text) - _DISPLAY_LIMIT} more chars on the clipboard]"
        return shown + clip_note
    except Exception as exc:
        log.error("ocr_screen failed: %s", exc, exc_info=True)
        return f"Screen OCR failed: {exc}"
