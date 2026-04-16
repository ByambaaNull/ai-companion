"""
screen_watcher.py — Periodic screen capture + vision LLM analysis.

Pipeline every SCREEN_WATCH_INTERVAL seconds:
    mss screen capture → JPEG bytes → Ollama vision LLM (moondream2/llava)
    → JSON scene description → emits SceneEvent via asyncio.Queue

The scene event is consumed by desktop_character.py to drive reactions.
Runs as a background asyncio Task — never blocks the agent loop.

Vision model requirements:
    ollama pull moondream  (1.8 GB, fast, good for scene description)
    OR
    ollama pull llava:7b   (4.2 GB, more accurate, needs more VRAM)
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

    # ─── Public ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Background task — call via asyncio.create_task()."""
        self._running = True
        log.info(
            "ScreenWatcher starting (interval=%ds, model=%s)",
            config.SCREEN_WATCH_INTERVAL,
            config.VISION_MODEL,
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
                # Downscale for faster LLM processing — 1024px wide max
                max_w = 1024
                if img.width > max_w:
                    ratio = max_w / img.width
                    img = img.resize(
                        (max_w, int(img.height * ratio)),
                        Image.LANCZOS,
                    )
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=75)
                return base64.b64encode(buf.getvalue()).decode("ascii")
        except ImportError as exc:
            log.error("Missing dependency for screen capture: %s — pip install mss pillow", exc)
            return None
        except Exception as exc:
            log.error("Screen capture failed: %s", exc)
            return None

    async def _query_vision_llm(self, jpeg_b64: str) -> SceneEvent | None:
        """Send screenshot to Ollama vision model, parse response."""
        payload = {
            "model": config.VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": _VISION_PROMPT,
                    "images": [jpeg_b64],
                }
            ],
            "stream": False,
            "options": {"temperature": 0.1, "num_ctx": 1024},
        }

        url = f"{config.OLLAMA_BASE_URL}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    log.warning(
                        "ScreenWatcher: vision LLM HTTP %d — %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return None
                data = resp.json()
                raw_text = data.get("message", {}).get("content", "").strip()
        except httpx.ConnectError:
            log.warning("ScreenWatcher: Ollama not reachable")
            return None
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
