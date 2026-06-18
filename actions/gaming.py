"""
actions/gaming.py — App launching, gaming automation, and video playback.

Actions:
  open_steam()                  Launch the Steam client
  launch_game(name)             Launch a game by name (maps common names to Steam IDs)
  play_video(query)             Open a YouTube search in the browser
  press_key(combo)              Press a key or keyboard shortcut (e.g. "enter", "ctrl+c")
  cs2_queue()                   Launch CS2 via Steam
  start_accept_watcher(tts)     Background task: auto-clicks the CS2 match Accept button

Match accept detection:
  Polls every 2 s, scans the centre band of the screen for a large
  green region (the CS2 / Valorant accept button cluster).
  Times out after 5 minutes by default.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

import config

if TYPE_CHECKING:
    from tts_rvc import TTSEngine

log = logging.getLogger(__name__)

# ─── Steam App IDs ─────────────────────────────────────────────────────────────

STEAM_APP_IDS: dict[str, str] = {
    "cs2":                        "730",
    "counter-strike 2":           "730",
    "counter strike 2":           "730",
    "csgo":                       "730",
    "cs go":                      "730",
    "dota 2":                     "570",
    "dota2":                      "570",
    "dota":                       "570",
    "tf2":                        "440",
    "team fortress 2":            "440",
    "rust":                       "252490",
    "valheim":                    "892970",
    "elden ring":                 "1245620",
    "cyberpunk 2077":             "1091500",
    "cyberpunk":                  "1091500",
    "gta 5":                      "271590",
    "gta v":                      "271590",
    "grand theft auto 5":         "271590",
    "grand theft auto v":         "271590",
    "apex legends":               "1172470",
    "apex":                       "1172470",
    "pubg":                       "578080",
    "warframe":                   "230410",
    "destiny 2":                  "1085660",
    "rainbow six siege":          "359550",
    "r6":                         "359550",
    "rocket league":              "252950",
    # Strategy / TW
    "total war warhammer 3":      "1142710",
    "total war warhammer":        "364360",
    "total war warhammer 2":      "594570",
    "warhammer 3":                "1142710",
    "warhammer 2":                "594570",
    "total war troy":             "1099410",
    "troy":                       "1099410",
    "total war three kingdoms":   "779340",
    "three kingdoms":             "779340",
    "total war pharaoh":          "1937780",
    "pharaoh":                    "1937780",
    "total war rome remastered":  "885970",
    "total war attila":           "325610",
    "attila":                     "325610",
    "total war medieval 2":       "4700",
    # Roguelikes
    "slay the spire":             "646570",
    "slay the spire 2":           "2483220",
    "sts2":                       "2483220",
    "sts":                        "646570",
    "hades":                      "1145360",
    "hades 2":                    "1145370",
    "dead cells":                 "588650",
    "vampire survivors":          "1794680",
    # Other popular
    "stardew valley":             "413150",
    "stardew":                    "413150",
    "satisfactory":               "526870",
    "factorio":                   "427520",
    "rimworld":                   "294100",
    "baldur's gate 3":            "1086940",
    "bg3":                        "1086940",
    "divinity original sin 2":    "435150",
    "dos2":                       "435150",
    "monster hunter world":       "582010",
    "mhw":                        "582010",
    "dark souls 3":               "374320",
    "ds3":                        "374320",
    "sekiro":                     "814380",
    "hollow knight":              "367520",
    "terraria":                   "105600",
    "minecraft":                  "1672970",
    "among us":                   "945360",
    "fall guys":                  "1097150",
    "the forest":                 "242760",
    "sons of the forest":         "1326470",
    "palworld":                   "1623730",
    "enshrouded":                 "1203620",
}

_STEAM_PATHS: list[Path] = [
    Path(config.STEAM_EXECUTABLE),
    Path(r"C:\Program Files (x86)\Steam\steam.exe"),
    Path(r"C:\Program Files\Steam\steam.exe"),
]


def _find_steam_exe() -> Path | None:
    for p in _STEAM_PATHS:
        if p.exists():
            return p
    found = shutil.which("steam")
    return Path(found) if found else None


# ─── open_steam ────────────────────────────────────────────────────────────────

def open_steam() -> str:
    """Launch the Steam client."""
    steam = _find_steam_exe()
    if steam:
        subprocess.Popen(
            [str(steam)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "Opening Steam."
    try:
        os.startfile("steam://open/main")
        return "Opening Steam."
    except Exception as exc:
        return f"Could not open Steam: {exc}"


# ─── launch_game ───────────────────────────────────────────────────────────────

def launch_game(name: str) -> str:
    """Launch a game by name via Steam, with fuzzy fallback."""
    import difflib
    key = name.lower().strip()
    if key not in STEAM_APP_IDS:
        # Fuzzy match against known game names
        close = difflib.get_close_matches(key, STEAM_APP_IDS.keys(), n=1, cutoff=0.6)
        if close:
            key = close[0]
            log.info("Fuzzy matched '%s' → '%s'", name, key)
        else:
            # Try running as a bare executable name
            try:
                subprocess.Popen(
                    [name.strip()],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return f"Trying to launch {name}."
            except FileNotFoundError:
                return (
                    f"I don't know '{name}'. "
                    "Add it to STEAM_APP_IDS in actions/gaming.py or use 'open_app'."
                )

    app_id = STEAM_APP_IDS[key]
    try:
        os.startfile(f"steam://rungameid/{app_id}")
        return f"Launching {name} via Steam."
    except Exception as exc:
        return f"Failed to launch {name}: {exc}"


# ─── play_video ────────────────────────────────────────────────────────────────

def play_video(query: str) -> str:
    """Find the best YouTube video for query and open it in Chrome."""
    from actions.browser import open_youtube
    open_youtube(query.strip())
    return ""


# ─── press_key ─────────────────────────────────────────────────────────────────

def press_key(combo: str) -> str:
    """
    Press a keyboard key or combo.
    Examples: "enter", "escape", "ctrl+c", "alt+tab", "ctrl+shift+esc"
    """
    import pyautogui
    import time as _t
    _t.sleep(0.15)
    keys = [k.strip().lower() for k in combo.split("+")]
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)
    return f"Pressed {combo}."


# ─── cs2_queue ─────────────────────────────────────────────────────────────────

def cs2_queue() -> str:
    """Launch CS2 via Steam."""
    try:
        os.startfile("steam://rungameid/730")
        return (
            "Launching CS2. "
            "Once you're in the game and queuing, say 'watch for match accept' "
            "and I'll click Accept automatically when a game is found."
        )
    except Exception as exc:
        return f"Could not launch CS2: {exc}"


# ─── Background match-accept watcher ──────────────────────────────────────────

_watcher_task: asyncio.Task | None = None


async def _watch_and_accept(tts: "TTSEngine | None", timeout: int) -> None:
    """
    Polls every 2 s for a large green cluster in the centre of the screen
    (CS2 / game match accept button).  Clicks it and speaks the result.
    """
    try:
        import numpy as np
        import pyautogui
    except ImportError:
        log.error("numpy or pyautogui missing — accept watcher cannot run")
        return

    log.info("Accept watcher running (timeout=%ds)…", timeout)
    deadline = time.monotonic() + timeout
    accepted = False

    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        try:
            screen = pyautogui.screenshot()
            arr = np.asarray(screen)
            h, w = arr.shape[:2]

            # Scan the centre 50 % width × centre-upper 50 % height
            rx0, rx1 = int(w * 0.25), int(w * 0.75)
            ry0, ry1 = int(h * 0.30), int(h * 0.80)
            region = arr[ry0:ry1, rx0:rx1]

            r = region[:, :, 0].astype(np.int16)
            g = region[:, :, 1].astype(np.int16)
            b = region[:, :, 2].astype(np.int16)

            # Green region: G clearly dominant, not too dark
            green_mask = (g > 110) & (r < 140) & (b < 140) & ((g - r) > 35) & ((g - b) > 35)
            green_count = int(green_mask.sum())

            if green_count > 800:
                ys, xs = np.where(green_mask)
                click_x = int(xs.mean()) + rx0
                click_y = int(ys.mean()) + ry0
                log.info(
                    "Accept button detected (green_count=%d) at (%d, %d) — clicking",
                    green_count, click_x, click_y,
                )
                pyautogui.click(click_x, click_y)
                accepted = True
                break

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Accept watcher poll error: %s", exc)

    msg = "Match accepted!" if accepted else "Accept watcher timed out — no accept screen found."
    log.info(msg)
    if tts:
        try:
            await tts.speak(msg)
        except Exception:
            pass


def start_accept_watcher(tts: "TTSEngine | None" = None, timeout: int = 300) -> str:
    """
    Fire up a background asyncio task to auto-click the match accept button.
    Cancels any previously running watcher first.
    """
    global _watcher_task

    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
        log.info("Cancelled previous accept watcher")

    try:
        loop = asyncio.get_running_loop()
        _watcher_task = loop.create_task(_watch_and_accept(tts, timeout))
        minutes = timeout // 60
        return (
            f"I'm watching for a match accept screen. "
            f"I'll click Accept automatically for up to {minutes} minutes."
        )
    except RuntimeError:
        return "Could not start accept watcher — no running event loop."


def cancel_accept_watcher() -> str:
    """Stop a running accept watcher."""
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
        return "Accept watcher cancelled."
    return "No accept watcher is running."


# ─── steam_search ─────────────────────────────────────────────────────────────

def steam_search(query: str) -> str:
    """
    Search the Steam store API for games matching *query*.
    Returns a text summary of up to 5 results with names, app IDs, and prices.
    No API key required — uses the public Steam store search endpoint.
    """
    import json
    import urllib.request

    endpoint = (
        "https://store.steampowered.com/api/storesearch/"
        f"?term={urllib.parse.quote(query)}&l=english&cc=US"
    )
    try:
        req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        log.warning("Steam store search failed: %s", exc)
        return f"Steam search failed: {exc}"

    items = data.get("items", [])[:5]
    if not items:
        return f"No Steam games found for '{query}'."

    lines = [f"[Steam search: {query}]"]
    for item in items:
        name   = item.get("name", "Unknown")
        app_id = item.get("id", "")
        price  = item.get("price", {})
        if price:
            price_str = price.get("final_formatted") or "Free"
        else:
            price_str = "Free / N/A"
        lines.append(f"- {name}  (App {app_id})  {price_str}")
        lines.append(f"  https://store.steampowered.com/app/{app_id}/")
    return "\n".join(lines)
