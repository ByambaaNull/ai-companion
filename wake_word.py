"""
wake_word.py — Keyboard hotkey activation for the Gojo AI companion.

No microphone always-on, no voice detection, no extra models.

Hotkeys (configurable in config.py):
    HOTKEY_ACTIVATE  (default Ctrl+Shift+G) — wake Gojo, he listens once
    HOTKEY_QUIT      (default Ctrl+Shift+Q) — shut the agent down cleanly

Interface:
    detector = HotkeyActivator()
    await detector.wait_for_wake_word()   # blocks until activate hotkey pressed

WakeWordDetector is an alias for HotkeyActivator for drop-in compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

import keyboard

import config

log = logging.getLogger(__name__)


class HotkeyActivator:
    """
    Activates Gojo on a global keyboard hotkey press.

    HOTKEY_ACTIVATE  (default Ctrl+Shift+G) — triggers one conversation turn
    HOTKEY_QUIT      (default Ctrl+Shift+Q) — shuts the agent down cleanly

    No microphone is open between turns. Zero CPU, zero VRAM idle cost.
    """

    def __init__(self) -> None:
        self._activate_event = threading.Event()
        self._quit_event = threading.Event()

        keyboard.add_hotkey(
            config.HOTKEY_ACTIVATE,
            self._on_activate,
            suppress=False,
        )
        keyboard.add_hotkey(
            config.HOTKEY_QUIT,
            self._on_quit,
            suppress=False,
        )
        log.info(
            "Hotkey activator ready ✓  activate=%r  quit=%r",
            config.HOTKEY_ACTIVATE,
            config.HOTKEY_QUIT,
        )

    def _on_activate(self) -> None:
        log.debug("Activate hotkey pressed")
        self._activate_event.set()

    def _on_quit(self) -> None:
        log.info("Quit hotkey pressed — shutting down")
        self._quit_event.set()
        self._activate_event.set()  # unblock any waiting coroutine

    @property
    def quit_requested(self) -> bool:
        return self._quit_event.is_set()

    # ─── Public async interface ───────────────────────────────────────────────

    async def wait_for_wake_word(self) -> None:
        """Block until the activate hotkey is pressed."""
        self._activate_event.clear()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._activate_event.wait)

    async def continuous_stream(self):
        """Yields each time the activate hotkey fires."""
        while True:
            await self.wait_for_wake_word()
            yield


# Alias so main.py import stays unchanged
WakeWordDetector = HotkeyActivator


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    config.setup_logging()
    log.info("Hotkey test — press %r to activate, %r to quit",
             config.HOTKEY_ACTIVATE, config.HOTKEY_QUIT)

    activator = HotkeyActivator()

    async def _test() -> None:
        count = 0
        while count < 3 and not activator.quit_requested:
            log.info("Waiting for hotkey… (%d/3)", count + 1)
            await activator.wait_for_wake_word()
            if activator.quit_requested:
                break
            count += 1
            log.info("  → Activated! (%d/3)", count)

    try:
        asyncio.run(_test())
        log.info("Test complete ✓")
    except KeyboardInterrupt:
        log.info("Interrupted")
        sys.exit(0)
