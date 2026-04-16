"""
personality.py — Converts screen scene events into in-character reactions.

Instead of canned responses, routes scene context through the LLM with
the character's personality baked into the system prompt. This means
reactions are dynamic, never repeat the same line, and stay fully in-character.

The character's personality traits are defined in config.CHARACTER_PERSONALITY.

Flow:
    SceneEvent (tags + description)
        → build_reaction_prompt()
        → LLM (Ollama, same model as main)
        → short in-character reaction string
        → DesktopCharacter.react(text) or .point_at(x, y, text)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

import config

if TYPE_CHECKING:
    from screen_watcher import SceneEvent
    from desktop_character import DesktopCharacter

log = logging.getLogger(__name__)

# Don't react to the same tag cluster more than once per this interval (seconds)
_REACT_COOLDOWN = 30.0


class PersonalityReactor:
    """
    Listens to scene events from ScreenWatcher and drives character reactions.

    Usage:
        reactor = PersonalityReactor(character)
        asyncio.create_task(reactor.run(screen_watcher))
    """

    def __init__(self, character: "DesktopCharacter") -> None:
        self._char = character
        self._last_react_time: float = 0.0
        self._last_tags: frozenset[str] = frozenset()
        self._running = False

    async def run(self, watcher) -> None:
        """Background task that polls ScreenWatcher and reacts."""
        self._running = True
        log.info("PersonalityReactor started")
        while self._running:
            event = await watcher.get_event()
            if event is not None:
                await self._handle_event(event)
            await asyncio.sleep(1.0)

    def stop(self) -> None:
        self._running = False

    async def _handle_event(self, event: "SceneEvent") -> None:
        now = time.time()
        new_tags = frozenset(event.tags)

        # Cooldown: skip if same tags seen recently
        if (
            new_tags == self._last_tags
            or now - self._last_react_time < _REACT_COOLDOWN
        ):
            return

        # Check if any tags match personality interests
        relevant = new_tags & frozenset(config.CHARACTER_INTEREST_TAGS)
        if not relevant and not _has_strong_visual(new_tags):
            log.debug("PersonalityReactor: no relevant tags in %s", new_tags)
            return

        self._last_tags = new_tags
        self._last_react_time = now

        reaction = await self._generate_reaction(event)
        if not reaction:
            return

        log.info("PersonalityReactor: reacting → %s", reaction)

        # If pointing-type reaction (character sees something specific), point toward it
        # For now point toward screen centre area — can be extended with object detection coords
        if any(t in new_tags for t in ("youtube", "anime", "game", "video", "stream")):
            self._char.point_at(
                config.SCREEN_WATCH_REGION_X,
                config.SCREEN_WATCH_REGION_Y,
                reaction,
            )
        else:
            self._char.react(reaction)

    async def _generate_reaction(self, event: "SceneEvent") -> str | None:
        """Ask the LLM to produce a short in-character reaction to the scene."""
        prompt = _build_reaction_prompt(event)
        payload = {
            "model": config.OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": config.CHARACTER_PERSONALITY_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.85, "num_ctx": 512, "num_predict": 60},
        }

        url = f"{config.OLLAMA_BASE_URL}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                text = data.get("message", {}).get("content", "").strip()
                # Keep it short — one sentence only
                if "." in text:
                    text = text.split(".")[0].strip() + "."
                return text or None
        except Exception as exc:
            log.warning("PersonalityReactor: LLM error: %s", exc)
            return None


def _build_reaction_prompt(event: "SceneEvent") -> str:
    tags_str = ", ".join(event.tags) if event.tags else "nothing specific"
    return (
        f"You just noticed what's on the user's screen. Here's what you see:\n"
        f"\"{event.description}\"\n"
        f"Relevant things visible: {tags_str}\n\n"
        f"React in ONE short sentence exactly as your character would. "
        f"Be spontaneous and natural. Don't ask questions. "
        f"Don't start with 'I'. Max 15 words."
    )


def _has_strong_visual(tags: frozenset[str]) -> bool:
    """Return True for visually striking things worth reacting to even if not an interest."""
    strong = {"explosion", "fight", "blood", "death", "anime", "meme", "funny", "horror"}
    return bool(tags & strong)
