"""
actions/afk_gaming.py — AFK game automation using screen capture + input.

Runs a background coroutine that periodically screens the game window and
performs the most sensible "idle" action so progress continues while the
user is away.

Supported games (passed as profile name to start_afk_gaming):
  slay_the_spire_2   — End-turn loop; optionally plays cards with pyautogui
  slay_the_spire     — Same logic (STS1 compatible)
  total_war          — Auto end-turn in campaign; auto-resolves battles
  generic            — Just spam the configured key every N seconds

Each game profile is a dict:
  interval    — seconds between automation ticks
  tick_fn     — async def tick(state) → None

Usage (via ActionRouter):
  ACTION: afk_game | slay the spire 2
  ACTION: stop_afk  |
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Awaitable, Any

log = logging.getLogger(__name__)

# ─── Running task handle ───────────────────────────────────────────────────────
_afk_task: asyncio.Task | None = None
_afk_game_name: str = ""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _screenshot_np():
    """Return a numpy RGB array of the primary screen, or None on failure."""
    try:
        import numpy as np
        import mss  # type: ignore[import]
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # primary monitor
            img = sct.grab(monitor)
            arr = np.frombuffer(img.raw, dtype=np.uint8).reshape(
                (img.height, img.width, 4)
            )
            return arr[:, :, :3]  # drop alpha → RGB
    except Exception as exc:
        log.debug("Screenshot failed: %s", exc)
        return None


def _color_match(arr, x: int, y: int, r: int, g: int, b: int, tol: int = 30) -> bool:
    """Check if pixel at (x, y) is close to the target RGB."""
    try:
        import numpy as np
        px = arr[y, x]
        return bool(
            abs(int(px[0]) - r) <= tol
            and abs(int(px[1]) - g) <= tol
            and abs(int(px[2]) - b) <= tol
        )
    except Exception:
        return False


def _region_has_color(arr, x1, y1, x2, y2, r, g, b, tol=40, min_frac=0.05) -> bool:
    """Return True if ≥ min_frac of the region matches the colour."""
    try:
        import numpy as np
        region = arr[y1:y2, x1:x2].astype(np.int32)
        diff = (
            np.abs(region[:, :, 0] - r)
            + np.abs(region[:, :, 1] - g)
            + np.abs(region[:, :, 2] - b)
        )
        matched = np.sum(diff <= (tol * 3))
        total   = (y2 - y1) * (x2 - x1)
        return bool(total > 0 and matched / total >= min_frac)
    except Exception:
        return False


def _click(x: int, y: int, delay: float = 0.12) -> None:
    import pyautogui
    import time as t
    t.sleep(delay)
    pyautogui.click(x, y)


def _press(key: str, delay: float = 0.15) -> None:
    import pyautogui
    import time as t
    t.sleep(delay)
    pyautogui.press(key)


# ─── Slay the Spire 1 & 2 automation ─────────────────────────────────────────
#
# Strategy:
#   1. Try to detect the "End Turn" button (large orange/gold button, bottom-right)
#   2. If found, click it — this is the safe always-on action while AFK
#   3. Occasionally also check if we're on a card-reward screen (blue modal)
#      and press Escape to skip it (safer than picking a random card).
#   4. Map screen: detect combat vs map and press a random direction key
#      to keep progressing (only if not in combat).
#
# The End Turn button in STS2 is roughly:
#   - located at ~75-85% of screen width, ~80-90% of screen height
#   - orange-gold colour (#C8860A ish) in STS1, similar in STS2
#
# We use colour + position heuristics rather than image templates so
# this works regardless of resolution.

async def _tick_slay_the_spire(state: dict) -> None:
    import pyautogui
    loop = asyncio.get_running_loop()

    arr = await loop.run_in_executor(None, _screenshot_np)
    if arr is None:
        return

    h, w = arr.shape[:2]

    # ── 1. Detect "End Turn" button (orange cluster, bottom-right quadrant) ──
    rx1, ry1 = int(w * 0.65), int(h * 0.75)
    rx2, ry2 = int(w * 0.95), int(h * 0.97)
    end_turn_orange = _region_has_color(arr, rx1, ry1, rx2, ry2, 200, 130, 10, tol=50, min_frac=0.04)

    if end_turn_orange:
        cx = (rx1 + rx2) // 2
        cy = (ry1 + ry2) // 2
        log.info("[STS AFK] End Turn button detected — clicking (%d,%d)", cx, cy)
        await loop.run_in_executor(None, _click, cx, cy)
        state["last_end_turn"] = time.monotonic()
        return

    # ── 2. Detect card reward screen (dark blue modal in centre) ──
    # Skip rewards to stay safe — press Escape or Skip button
    modal_x1, modal_y1 = int(w * 0.3), int(h * 0.2)
    modal_x2, modal_y2 = int(w * 0.7), int(h * 0.8)
    has_blue_modal = _region_has_color(
        arr, modal_x1, modal_y1, modal_x2, modal_y2, 20, 40, 100, tol=40, min_frac=0.10
    )
    if has_blue_modal:
        log.info("[STS AFK] Card reward screen detected — pressing Escape to skip")
        await loop.run_in_executor(None, _press, "escape")
        return

    # ── 3. On map / event — press a movement key or space to advance ──
    elapsed = time.monotonic() - state.get("last_move", 0)
    if elapsed > 6:  # don't spam
        log.info("[STS AFK] Idle on map — pressing space / enter to advance")
        await loop.run_in_executor(None, _press, "space")
        state["last_move"] = time.monotonic()


# ─── Total War automation ─────────────────────────────────────────────────────
#
# Strategy for campaign map:
#   1. Detect the "End Turn" button (large glowing button, bottom-right)
#      It's typically dark red/golden in TW games.
#   2. Handle battle dialogs — auto-resolve by pressing the auto-resolve shortcut
#      or clicking the auto-resolve button.
#
# TW End Turn button is approximately at (85%, 93%) of screen, bright gold.

async def _tick_total_war(state: dict) -> None:
    import pyautogui
    loop = asyncio.get_running_loop()

    arr = await loop.run_in_executor(None, _screenshot_np)
    if arr is None:
        return

    h, w = arr.shape[:2]

    # ── 1. Check for "End Turn" button (golden, bottom-right) ──
    rx1, ry1 = int(w * 0.75), int(h * 0.85)
    rx2, ry2 = int(w * 0.98), int(h * 0.99)
    end_turn_gold = _region_has_color(arr, rx1, ry1, rx2, ry2, 210, 170, 50, tol=60, min_frac=0.03)

    if end_turn_gold:
        cx, cy = (rx1 + rx2) // 2, (ry1 + ry2) // 2
        log.info("[TW AFK] End Turn detected — clicking (%d,%d)", cx, cy)
        await loop.run_in_executor(None, _click, cx, cy)
        state["last_end_turn"] = time.monotonic()
        await asyncio.sleep(2)  # wait for confirmation dialog
        # Confirm end turn if a dialog pops up (usually Enter works)
        await loop.run_in_executor(None, _press, "return")
        return

    # ── 2. Auto-resolve battles (look for glowing red/orange battle panel) ──
    bx1, by1 = int(w * 0.3), int(h * 0.1)
    bx2, by2 = int(w * 0.7), int(h * 0.5)
    has_battle_panel = _region_has_color(
        arr, bx1, by1, bx2, by2, 180, 30, 10, tol=50, min_frac=0.05
    )
    if has_battle_panel:
        log.info("[TW AFK] Battle detected — pressing auto-resolve shortcut")
        # TW games typically bind auto-resolve to Ctrl+A or a specific button
        await loop.run_in_executor(
            None, lambda: __import__("pyautogui").hotkey("ctrl", "a")
        )
        await asyncio.sleep(1)
        await loop.run_in_executor(None, _press, "return")
        return

    # ── 3. Dismiss AI movement confirmations (press space) ──
    elapsed = time.monotonic() - state.get("last_dismiss", 0)
    if elapsed > 4:
        await loop.run_in_executor(None, _press, "space")
        state["last_dismiss"] = time.monotonic()


# ─── Generic automation ───────────────────────────────────────────────────────

async def _tick_generic(state: dict) -> None:
    key = state.get("key", "return")
    log.info("[AFK generic] Pressing %s", key)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _press, key)


# ─── Profile registry ─────────────────────────────────────────────────────────

_PROFILES: dict[str, dict] = {
    "slay_the_spire_2":  {"interval": 4,  "tick": _tick_slay_the_spire},
    "slay_the_spire":    {"interval": 4,  "tick": _tick_slay_the_spire},
    "sts2":              {"interval": 4,  "tick": _tick_slay_the_spire},
    "sts":               {"interval": 4,  "tick": _tick_slay_the_spire},
    "total_war":         {"interval": 6,  "tick": _tick_total_war},
    "total war":         {"interval": 6,  "tick": _tick_total_war},
    "generic":           {"interval": 5,  "tick": _tick_generic},
}

_GAME_NAME_MAP: dict[str, str] = {
    "slay the spire 2": "slay_the_spire_2",
    "slay the spire":   "slay_the_spire",
    "sts2":             "slay_the_spire_2",
    "sts":              "slay_the_spire",
    "total war":        "total_war",
    "warhammer":        "total_war",
    "warhammer 3":      "total_war",
    "three kingdoms":   "total_war",
    "attila":           "total_war",
    "troy":             "total_war",
    "pharaoh":          "total_war",
}


def _resolve_profile(name: str) -> str:
    """Map a human game name to a profile key."""
    import difflib
    key = name.lower().strip()
    if key in _PROFILES:
        return key
    if key in _GAME_NAME_MAP:
        return _GAME_NAME_MAP[key]
    # Fuzzy match against known names
    candidates = list(_GAME_NAME_MAP.keys()) + list(_PROFILES.keys())
    close = difflib.get_close_matches(key, candidates, n=1, cutoff=0.55)
    if close:
        matched = close[0]
        return _GAME_NAME_MAP.get(matched, matched)
    return "generic"


# ─── Background loop ──────────────────────────────────────────────────────────

async def _afk_loop(profile_key: str, tts=None) -> None:
    profile  = _PROFILES[profile_key]
    interval = profile["interval"]
    tick_fn  = profile["tick"]
    state: dict = {}

    log.info("AFK gaming started for profile: %s (tick every %ds)", profile_key, interval)
    if tts:
        try:
            await tts.speak_async(f"AFK mode on. I'll keep {profile_key.replace('_', ' ')} running.")
        except Exception:
            pass

    while True:
        try:
            await tick_fn(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[AFK] tick error: %s", exc)
        await asyncio.sleep(interval)


# ─── Public API ───────────────────────────────────────────────────────────────

def start_afk_gaming(game_name: str, tts=None) -> str:
    """
    Start AFK automation for the given game.
    Returns a human-readable confirmation string.
    """
    global _afk_task, _afk_game_name

    profile_key = _resolve_profile(game_name)

    # Cancel any existing task
    if _afk_task and not _afk_task.done():
        _afk_task.cancel()
        log.info("Previous AFK task cancelled")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return "AFK gaming requires a running event loop."

    _afk_game_name = game_name
    _afk_task = loop.create_task(_afk_loop(profile_key, tts))
    pretty = profile_key.replace("_", " ").title()
    return (
        f"AFK mode enabled for {pretty}. "
        f"I'll keep the game going — I'll end turns and handle prompts automatically."
    )


def stop_afk_gaming() -> str:
    """Stop the running AFK automation."""
    global _afk_task
    if _afk_task and not _afk_task.done():
        _afk_task.cancel()
        _afk_task = None
        return f"AFK mode stopped. Welcome back — {_afk_game_name} is all yours."
    return "No AFK automation is running."
