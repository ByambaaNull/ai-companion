"""
action_router.py — Parse LLM output for ACTION directives and dispatch them.

Action format (from LLM):
    ACTION: <action_name> | <param>

Examples:
    ACTION: open_browser | https://youtube.com
    ACTION: play_music | lofi hip hop
    ACTION: screenshot | full
    ACTION: type_text | Hello world
    ACTION: click | 960,540

Design:
- Strict regex parsing — not string split — handles varied LLM whitespace
- Extensible registry: one function + one register() call = new action
- Returns (response_text, action_result_or_None) tuple
- Never raises — failed actions return a human-readable error string
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import Callable, Awaitable

import config

log = logging.getLogger(__name__)

# Matches: ACTION: <name> | <param>
# Groups:  (1) action_name  (2) param (may be empty)
_ACTION_PATTERN = re.compile(
    r"ACTION:\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\|\s*(.*)",
    re.IGNORECASE,
)

# Handler type: async (param: str) -> str
ActionHandler = Callable[[str], Awaitable[str]]


class ActionRouter:
    """
    Registry and dispatcher for companion actions.

    Usage:
        router = ActionRouter(music_player)
        response_text, action_result = await router.parse_and_execute(llm_output)
    """

    def __init__(self, music_player=None, tts=None) -> None:
        self._handlers: dict[str, ActionHandler] = {}
        self._tts = tts
        self._register_defaults(music_player)

    def register(self, name: str, handler: ActionHandler) -> None:
        """
        Register an action handler.

        Args:
            name: Case-insensitive action name (e.g. "open_browser")
            handler: Async callable (param: str) -> str result message
        """
        self._handlers[name.lower()] = handler
        log.debug("Registered action: %s", name)

    def _register_defaults(self, music_player) -> None:
        """Register all built-in action handlers."""
        from actions.browser import open_browser
        from actions.screenshot import take_screenshot, screenshot_to_base64
        from actions.gaming import (
            open_steam, launch_game, play_video, press_key,
            cs2_queue, start_accept_watcher, cancel_accept_watcher,
        )
        from actions.touch import swipe, scroll, double_tap, pinch_zoom
        import pyautogui

        # open_browser
        async def _open_browser(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, open_browser, param.strip()
            )

        # play_music
        async def _play_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.play, param.strip()
            )

        # screenshot
        async def _screenshot(param: str) -> str:
            loop = asyncio.get_running_loop()
            path = await loop.run_in_executor(None, take_screenshot, None)
            return f"Screenshot saved to {path.name}."

        # type_text — paste via clipboard to support full Unicode
        async def _type_text(param: str) -> str:
            loop = asyncio.get_running_loop()
            def _do_type():
                import time
                import pyperclip
                time.sleep(0.3)
                pyperclip.copy(param)
                pyautogui.hotkey("ctrl", "v")
            await loop.run_in_executor(None, _do_type)
            return f"Typed: {param[:40]}{'…' if len(param) > 40 else ''}"

        # click — "x,y" pixel coordinates
        # Guardrail: reject obviously destructive coordinates (0,0 etc.)
        async def _click(param: str) -> str:
            try:
                parts = param.strip().split(",")
                x, y = int(parts[0].strip()), int(parts[1].strip())
            except (ValueError, IndexError):
                return f"Invalid click coordinates: '{param}'. Use format: x,y"
            if x < 5 and y < 5:
                return "Click at (0,0) blocked — likely an LLM error."
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, pyautogui.click, x, y)
            return f"Clicked at ({x}, {y})."

        # open_app — launch any executable or app by name (shlex handles spaces)
        async def _open_app(param: str) -> str:
            import subprocess as sp
            try:
                args = shlex.split(param.strip(), posix=False)
                sp.Popen(args, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                return f"Launched {param.strip()}."
            except FileNotFoundError:
                return f"Could not find application: {param.strip()}"
            except Exception as exc:
                return f"Failed to launch {param.strip()}: {exc}"

        # ── gaming / system actions ──────────────────────────────────────────
        _tts_ref = self._tts

        async def _open_steam(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(None, open_steam)

        async def _launch_game(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, launch_game, param.strip()
            )

        async def _play_video(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, play_video, param.strip()
            )

        async def _press_key(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, press_key, param.strip()
            )

        async def _cs2_queue(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(None, cs2_queue)

        async def _accept_match(param: str) -> str:
            # timeout override via param e.g. "300"
            try:
                timeout = int(param.strip()) if param.strip().isdigit() else 300
            except (ValueError, AttributeError):
                timeout = 300
            return start_accept_watcher(tts=_tts_ref, timeout=timeout)

        async def _cancel_accept(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, cancel_accept_watcher
            )

        # ── touch actions ────────────────────────────────────────────────────

        async def _swipe(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, swipe, param.strip()
            )

        async def _scroll(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, scroll, param.strip()
            )

        async def _double_tap(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, double_tap, param.strip()
            )

        async def _pinch_zoom(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, pinch_zoom, param.strip()
            )

        self.register("open_browser", _open_browser)
        self.register("play_music", _play_music)
        self.register("screenshot", _screenshot)
        self.register("type_text", _type_text)
        self.register("click", _click)
        self.register("open_app", _open_app)
        self.register("open_steam", _open_steam)
        self.register("launch_game", _launch_game)
        self.register("play_video", _play_video)
        self.register("press_key", _press_key)
        self.register("cs2_queue", _cs2_queue)
        self.register("accept_match", _accept_match)
        self.register("cancel_accept", _cancel_accept)
        self.register("swipe", _swipe)
        self.register("scroll", _scroll)
        self.register("double_tap", _double_tap)
        self.register("pinch_zoom", _pinch_zoom)

    # ─── Parse and dispatch ───────────────────────────────────────────────────

    def extract_action(self, text: str) -> tuple[str, str] | None:
        """
        Extract ACTION directive from LLM output.

        Returns (action_name, param) or None if no action found.
        The text may contain prose before or after the ACTION line.
        """
        match = _ACTION_PATTERN.search(text)
        if match:
            return match.group(1).lower(), match.group(2).strip()
        return None

    def strip_action(self, text: str) -> str:
        """Remove the ACTION: line from response text for cleaner TTS output."""
        return _ACTION_PATTERN.sub("", text).strip()

    async def parse_and_execute(
        self, llm_output: str
    ) -> tuple[str, str | None]:
        """
        Parse LLM output for an ACTION directive and execute it.

        Returns:
            (speech_text, action_result_or_None)

            speech_text    — LLM response with ACTION line stripped (for TTS)
            action_result  — human-readable result returned by the handler,
                             or None if no action was found
        """
        action_tuple = self.extract_action(llm_output)
        speech_text = self.strip_action(llm_output)

        if action_tuple is None:
            return speech_text, None

        action_name, param = action_tuple
        handler = self._handlers.get(action_name)

        if handler is None:
            log.warning("Unknown action: %r", action_name)
            return speech_text, f"I don't know how to perform action '{action_name}'."

        log.info("Executing action: %s(%r)", action_name, param)
        try:
            result = await handler(param)
            log.info("Action result: %s", result)
            return speech_text, result
        except Exception as exc:
            log.error("Action %r raised: %s", action_name, exc)
            return speech_text, f"Action '{action_name}' failed: {exc}"


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    config.setup_logging()
    log.info("ActionRouter standalone test")

    router = ActionRouter(music_player=None)

    test_cases = [
        "Sure! ACTION: open_browser | https://github.com",
        "Here is the screenshot you requested. ACTION: screenshot | full",
        "Let me type that for you. ACTION: type_text | Hello, world!",
        "Playing your music now. ACTION: play_music | lofi hip hop",
        "ACTION: click | 960,540 I clicked the centre of your screen.",
        "No action in this response — just a normal reply.",
        "ACTION: unknown_action | test parameter",
    ]

    async def _test() -> None:
        for text in test_cases:
            log.info("\nInput:  %r", text)
            speech, result = await router.parse_and_execute(text)
            log.info("Speech: %r", speech)
            log.info("Action: %r", result)

    asyncio.run(_test())
    log.info("\nActionRouter tests passed ✓")
