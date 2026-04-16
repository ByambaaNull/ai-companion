"""
actions/browser.py — Open Chrome and navigate to URLs.

Uses subprocess (no shell=True) + pyautogui for automation.
Falls back to the default browser if Chrome is not found.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import config

log = logging.getLogger(__name__)

# Typical Chrome install paths on Windows
_CHROME_CANDIDATES: list[Path] = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
]

_DEFAULT_URL = "https://www.google.com"


def _find_chrome() -> Path | None:
    """Return path to chrome.exe if found, else None."""
    for candidate in _CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    # Also check PATH
    found = shutil.which("chrome") or shutil.which("google-chrome")
    return Path(found) if found else None


def open_browser(url: str = _DEFAULT_URL) -> str:
    """
    Open Chrome (or default browser) at the given URL.

    Args:
        url: Full URL including scheme, e.g. "https://youtube.com"

    Returns:
        Human-readable result string for TTS.
    """
    # Sanitise — only allow http/https/file schemes
    stripped = url.strip()
    if not stripped.startswith(("http://", "https://", "file://")):
        if "." in stripped and " " not in stripped:
            stripped = "https://" + stripped
        else:
            # Treat it as a Google search query
            import urllib.parse
            stripped = "https://www.google.com/search?q=" + urllib.parse.quote(stripped)

    chrome = _find_chrome()
    if chrome:
        log.info("Opening Chrome: %s", stripped)
        try:
            subprocess.Popen(
                [str(chrome), stripped],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return f"Opening {stripped} in Chrome."
        except OSError as exc:
            log.error("Failed to open Chrome: %s", exc)

    # Fallback to os.startfile (Windows default browser)
    log.info("Chrome not found — using default browser: %s", stripped)
    import os
    try:
        os.startfile(stripped)  # type: ignore[attr-defined]  # Windows only
        return f"Opening {stripped} in your default browser."
    except OSError as exc:
        log.error("Failed to open browser: %s", exc)
        return f"I could not open the browser: {exc}"


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    config.setup_logging()
    log.info("Browser action test")

    result = open_browser("https://www.google.com")
    log.info("Result: %s", result)

    result2 = open_browser("youtube.com")
    log.info("Result: %s", result2)
