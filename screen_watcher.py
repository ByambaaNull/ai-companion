"""
screen_watcher.py — On-demand + periodic screen capture with vision LLM analysis.

The `look_now()` coroutine powers the `look_at_screen` action: it captures the
screen and returns a detailed description via the GitHub Models vision API.

The `ScreenWatcher` class additionally supports a periodic ambient-watch loop
(every SCREEN_WATCH_INTERVAL seconds) that emits SceneEvents. It is provided for
opt-in use; the agent currently only wires up `look_now()`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import AsyncGenerator

import httpx

import config

log = logging.getLogger(__name__)


@dataclass
class SceneEvent:
    """Describes what is currently visible on screen."""
    description: str          # short natural language summary
    tags: list[str]           # normalised lowercase tags e.g. ["ramen", "youtube"]
    raw: str                  # raw LLM output
    timestamp: float = field(default_factory=time.time)


_VISION_PROMPT = (
    "Look at this screenshot. In ONE short sentence describe what the user is doing or watching. "
    "Then on a new line output TAGS: followed by a comma-separated list of relevant lowercase keywords "
    "(e.g. youtube, ramen, coding, anime, sukuna, game, music, food, chat). "
    "Be concise. Example:\n"
    "User is watching an anime fight scene on YouTube.\n"
    "TAGS: youtube, anime, fight, action"
)

_LOOK_PROMPT = (
    "Look at this screenshot carefully. Describe in 2-3 sentences exactly what you see: "
    "what application is open, what content is visible, what the user appears to be doing. "
    "Be specific — mention visible text, names, UI elements, and anything notable. "
    "Do NOT output TAGS. Just a natural, detailed description."
)


class ScreenWatcher:
    """
    Captures the screen periodically and emits SceneEvents.

    Usage:
        watcher = ScreenWatcher()
        asyncio.create_task(watcher.run())
        # elsewhere:
        event = await watcher.get_event()
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[SceneEvent] = asyncio.Queue(maxsize=3)
        self._last_tags: list[str] = []
        self._running = False
        self._backoff_until: float = 0.0  # epoch seconds; skip vision calls until then

    # ─── Public ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Background task — call via asyncio.create_task()."""
        self._running = True
        log.info(
            "ScreenWatcher starting (interval=%ds, model=%s)",
            config.SCREEN_WATCH_INTERVAL,
            config.GITHUB_GPT_MODEL,
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.warning("ScreenWatcher tick error: %s", exc)
            await asyncio.sleep(config.SCREEN_WATCH_INTERVAL)

    def stop(self) -> None:
        self._running = False

    async def get_event(self, timeout: float = 0.0) -> SceneEvent | None:
        """
        Non-blocking pop of the latest scene event.
        Returns None if nothing is queued.
        """
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ─── Internal ────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        """Capture screen → vision LLM → push SceneEvent."""
        # Respect 429 backoff
        if time.time() < self._backoff_until:
            remaining = self._backoff_until - time.time()
            log.debug("ScreenWatcher: rate-limit backoff — %.0fs remaining", remaining)
            return

        jpeg_b64 = await asyncio.get_running_loop().run_in_executor(
            None, self._capture_screen
        )
        if jpeg_b64 is None:
            return

        scene = await self._query_vision_llm(jpeg_b64)
        if scene is None:
            return

        # Only emit if tags changed meaningfully (avoid spamming identical events)
        new_tags = set(scene.tags)
        old_tags = set(self._last_tags)
        if new_tags == old_tags:
            log.debug("ScreenWatcher: scene unchanged, skipping event")
            return

        self._last_tags = scene.tags
        # Discard oldest if full — we only care about the latest
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(scene)
        log.info("ScreenWatcher: new scene — %s | tags: %s", scene.description[:60], scene.tags)

    def _capture_screen(self) -> str | None:
        """Capture primary monitor, return base64-encoded JPEG string."""
        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                monitor = sct.monitors[1]  # primary monitor
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                # Downscale for faster LLM processing — 512px wide for periodic watch
                max_w = 512
                if img.width > max_w:
                    ratio = max_w / img.width
                    img = img.resize(
                        (max_w, int(img.height * ratio)),
                        Image.LANCZOS,
                    )
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=50)
                return base64.b64encode(buf.getvalue()).decode("ascii")
        except ImportError as exc:
            log.error("Missing dependency for screen capture: %s — pip install mss pillow", exc)
            return None
        except Exception as exc:
            log.error("Screen capture failed: %s", exc)
            return None

    async def _query_vision_llm(self, jpeg_b64: str) -> SceneEvent | None:
        """Send screenshot to GitHub Models vision API, parse response."""
        payload = {
            "model": config.GITHUB_GPT_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{jpeg_b64}",
                        "detail": "low",
                    }},
                ],
            }],
            "stream": False,
            "temperature": 0.1,
            "max_tokens": 64,
        }
        headers = {
            "Authorization": f"Bearer {config.GITHUB_API_KEY}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(config.GITHUB_API_URL, json=payload, headers=headers)
                if resp.status_code == 429:
                    # Parse Retry-After if present, else back off for 60 s
                    retry_after = float(resp.headers.get("Retry-After", 60))
                    self._backoff_until = time.time() + retry_after
                    log.warning(
                        "ScreenWatcher: HTTP 429 — backing off for %.0fs", retry_after
                    )
                    return None
                if resp.status_code != 200:
                    log.warning("ScreenWatcher: HTTP %d — %s", resp.status_code, resp.text[:200])
                    return None
                choices = resp.json().get("choices", [])
                raw_text = choices[0].get("message", {}).get("content", "").strip() if choices else ""
        except Exception as exc:
            log.warning("ScreenWatcher: vision LLM error: %r", exc)
            return None

        return self._parse_vision_response(raw_text)

    @staticmethod
    def _parse_vision_response(raw: str) -> SceneEvent:
        """Parse 'description\\nTAGS: a,b,c' format from LLM output."""
        tags: list[str] = []
        description = raw.strip()

        lines = raw.strip().splitlines()
        desc_lines = []
        for line in lines:
            if line.upper().startswith("TAGS:"):
                tag_str = line.split(":", 1)[1]
                tags = [t.strip().lower() for t in tag_str.split(",") if t.strip()]
            else:
                desc_lines.append(line)

        description = " ".join(desc_lines).strip() or raw[:120]
        return SceneEvent(description=description, tags=tags, raw=raw)


# ─── On-demand screen look (used by look_at_screen action) ───────────────────

async def look_now() -> str:
    """
    Instantly capture the screen and return a detailed description via vision LLM.

    Designed for the `look_at_screen` action — gives the companion genuine
    real-time sight on request ("what's on my screen?", "read that for me", etc.).
    """
    # Vision works with ANY configured key (Groq / Gemini / GitHub) — bail early
    # with a helpful message if none is set, rather than sending an empty Bearer.
    if not config.VISION_PROVIDERS:
        return ("Screen vision needs an API key — add Groq, Gemini, or a GitHub "
                "token in Settings → API keys, then try again.")

    loop = asyncio.get_running_loop()

    # Capture in thread (mss is not async)
    def _capture() -> str | None:
        try:
            import mss
            from PIL import Image
            with mss.mss() as sct:
                raw = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                if img.width > 1280:
                    ratio = 1280 / img.width
                    img = img.resize((1280, int(img.height * ratio)), Image.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=80)
                return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:
            log.error("look_now capture failed: %s", exc)
            return None

    jpeg_b64 = await loop.run_in_executor(None, _capture)
    if jpeg_b64 is None:
        return "I couldn't capture the screen."

    content = [
        {"type": "text", "text": _LOOK_PROMPT},
        {"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{jpeg_b64}",
            "detail": "high",
        }},
    ]
    # Try each configured provider's vision model; fall over on failure.
    for prov in config.VISION_PROVIDERS:
        payload = {
            "model": prov["model"],
            "messages": [{"role": "user", "content": content}],
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 256,
        }
        headers = {
            "Authorization": f"Bearer {prov['key']}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(prov["url"], json=payload, headers=headers)
                resp.raise_for_status()
                choices = resp.json().get("choices", [])
                text = (choices[0].get("message", {}).get("content", "").strip()
                        if choices else "")
                if text:
                    return text
        except Exception as exc:
            log.warning("look_now via %s failed: %s — trying next", prov["name"], exc)
            continue
    return "I couldn't analyse the screen right now."
