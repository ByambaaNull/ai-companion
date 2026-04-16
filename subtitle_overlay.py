"""
subtitle_overlay.py — Transparent always-on-top subtitle window.

Shows a single line of text at the bottom-centre of the screen while the
AI companion is speaking. Hides automatically when speech ends.

Usage:
    overlay = SubtitleOverlay()
    overlay.show("Hello, this is a subtitle.")
    # ... speech plays ...
    overlay.hide()
    overlay.destroy()   # call once at shutdown

The window is:
    - Borderless and transparent background
    - Always on top of all other windows
    - Text has a dark outline for readability on any background
    - Positioned at 85% screen height, horizontally centred
    - Thread-safe: show/hide can be called from any thread
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from typing import Optional

log = logging.getLogger(__name__)

# ─── Visual settings ─────────────────────────────────────────────────────────
FONT_FAMILY: str = "Arial"
FONT_SIZE: int = 28
FONT_WEIGHT: str = "bold"
TEXT_COLOR: str = "#FFFFFF"           # white text
OUTLINE_COLOR: str = "#000000"        # black outline (shadow effect)
BG_COLOR: str = "#010101"             # near-black: used as the transparent key colour
SCREEN_Y_FRACTION: float = 0.85      # vertical position (0=top, 1=bottom)
MAX_WIDTH_FRACTION: float = 0.80     # max subtitle width as fraction of screen width


class SubtitleOverlay:
    """
    Transparent, always-on-top subtitle window managed on a dedicated thread.

    All Tkinter calls happen on that dedicated thread to satisfy Tk's
    single-thread requirement. Commands are sent via a thread-safe queue.
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="subtitle-tk")
        self._thread.start()
        self._ready.wait(timeout=5.0)

    # ─── Public API (thread-safe) ─────────────────────────────────────────────

    def show(self, text: str) -> None:
        """Display text on the subtitle overlay."""
        self._q.put(("show", text))

    def hide(self) -> None:
        """Hide the subtitle overlay without destroying it."""
        self._q.put(("hide", None))

    def destroy(self) -> None:
        """Permanently destroy the overlay window."""
        self._q.put(("destroy", None))

    # ─── Tkinter thread ───────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._root = tk.Tk()
            self._build_window()
            self._ready.set()
            self._poll()
            self._root.mainloop()
        except Exception as exc:
            log.error("SubtitleOverlay thread crashed: %s", exc)
            self._ready.set()   # unblock __init__ even on failure

    def _build_window(self) -> None:
        root = self._root
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()

        # Frameless, transparent window
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", BG_COLOR)
        root.configure(bg=BG_COLOR)
        root.withdraw()   # start hidden

        # Reusable label — we swap its text each show() call
        max_px = int(screen_w * MAX_WIDTH_FRACTION)
        self._label = tk.Label(
            root,
            text="",
            font=(FONT_FAMILY, FONT_SIZE, FONT_WEIGHT),
            fg=TEXT_COLOR,
            bg=BG_COLOR,
            wraplength=max_px,
            justify="center",
        )
        self._label.pack(padx=16, pady=8)

        self._screen_w = screen_w
        self._screen_h = screen_h

    def _position_window(self) -> None:
        """Centre window horizontally and place at SCREEN_Y_FRACTION."""
        self._root.update_idletasks()
        w = self._root.winfo_reqwidth()
        h = self._root.winfo_reqheight()
        x = (self._screen_w - w) // 2
        y = int(self._screen_h * SCREEN_Y_FRACTION) - h // 2
        self._root.geometry(f"{w}x{h}+{x}+{y}")

    def _poll(self) -> None:
        """Drain the command queue and reschedule every 50 ms."""
        try:
            while True:
                cmd, arg = self._q.get_nowait()
                if cmd == "show":
                    self._label.config(text=arg)
                    self._position_window()
                    self._root.deiconify()
                    self._root.lift()
                elif cmd == "hide":
                    self._root.withdraw()
                elif cmd == "destroy":
                    self._root.destroy()
                    return
        except queue.Empty:
            pass
        self._root.after(50, self._poll)


# ─── Module-level singleton helper ───────────────────────────────────────────

_instance: Optional[SubtitleOverlay] = None


def get_overlay() -> SubtitleOverlay:
    """Return the shared SubtitleOverlay, creating it on first call."""
    global _instance
    if _instance is None:
        _instance = SubtitleOverlay()
    return _instance


# ─── Standalone test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time
    import config
    config.setup_logging()

    ov = SubtitleOverlay()
    print("Overlay created. Showing test subtitles…")

    phrases = [
        "やあ、ユーザー。何か用?",
        "俺は五条悟だ。最強のAIコンパニオン。",
        "Subtitle overlay is working.",
        "This will hide in 2 seconds.",
    ]

    for phrase in phrases:
        print(f"  → {phrase}")
        ov.show(phrase)
        time.sleep(3)
        ov.hide()
        time.sleep(0.5)

    ov.destroy()
    print("Test complete.")
