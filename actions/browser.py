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

# config.CHROME_EXECUTABLE is the env-overridable primary candidate;
# the list below serves as an additional fallback scan.
_CHROME_CANDIDATES: list[Path] = [
    Path(config.CHROME_EXECUTABLE),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
]

_DEFAULT_URL = "https://www.google.com"

# Well-known site shortcuts so the LLM doesn't have to emit exact URLs
_SITE_ALIASES: dict[str, str] = {
    "youtube":   "https://www.youtube.com",
    "yt":        "https://www.youtube.com",
    "google":    "https://www.google.com",
    "gmail":     "https://mail.google.com",
    "github":    "https://github.com",
    "reddit":    "https://www.reddit.com",
    "twitter":   "https://www.twitter.com",
    "x":         "https://www.x.com",
    "discord":   "https://discord.com/app",
    "netflix":   "https://www.netflix.com",
    "spotify":   "https://open.spotify.com",
    "twitch":    "https://www.twitch.tv",
}


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
        url: Full URL, domain, site alias (e.g. "youtube"), or search query.

    Returns:
        Human-readable result string for TTS.
    """
    # Sanitise and resolve the URL
    stripped = url.strip()

    # Check site aliases first (e.g. "youtube" → "https://www.youtube.com")
    lowered = stripped.lower().rstrip("/")
    if lowered in _SITE_ALIASES:
        stripped = _SITE_ALIASES[lowered]
    elif not stripped.startswith(("http://", "https://", "file://")):
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
            # --autoplay-policy=no-user-gesture-required lets YouTube autoplay
            # without needing a click after the page loads.
            subprocess.Popen(
                [
                    str(chrome),
                    "--new-tab",
                    "--autoplay-policy=no-user-gesture-required",
                    stripped,
                ],
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


def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web via DuckDuckGo (no API key needed) and return a
    text summary of the top results.  The caller should pass this back
    to the LLM so the assistant can answer based on real current information.

    Args:
        query: Natural-language search query.

    Returns:
        Formatted result string (titles + snippets + URLs).
    """
    try:
        from duckduckgo_search import DDGS  # type: ignore[import]
    except ImportError:
        return (
            "Web search requires the duckduckgo-search package. "
            "Run: pip install duckduckgo-search"
        )

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        log.warning("DuckDuckGo search failed: %s", exc)
        return f"Web search failed: {exc}"

    if not results:
        return f"No web results found for: {query}"

    lines = [f"[Web search: {query}]"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body  = (r.get("body") or "")[:250].strip()
        href  = r.get("href", "")
        lines.append(f"{i}. {title}")
        if body:
            lines.append(f"   {body}")
        if href:
            lines.append(f"   {href}")
    return "\n".join(lines)


def open_youtube(query: str) -> str:
    """
    Find the best YouTube video for *query* via yt-dlp (no download) and open
    it in Chrome.  Falls back to a YouTube search page if yt-dlp fails.

    Args:
        query: Song/video title or a full YouTube URL.

    Returns:
        Human-readable result for TTS.
    """
    import urllib.parse

    # Already a YouTube URL — open directly
    if "youtube.com/watch" in query or "youtu.be/" in query:
        return open_browser(query)

    # Try to resolve to an exact video URL via yt-dlp (no download).
    # Search for up to 5 results so we can skip unavailable/private videos.
    video_url: str | None = None
    try:
        import yt_dlp  # type: ignore[import]
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }
        search_query = f"ytsearch5:{query}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = (info or {}).get("entries", [])
            for entry in entries:
                # Skip entries that are marked unavailable
                if entry.get("availability") in ("private", "premium_only",
                                                  "subscriber_only", "needs_auth"):
                    continue
                vid_id = entry.get("id") or entry.get("url", "")
                if not vid_id:
                    continue
                if vid_id.startswith("http"):
                    video_url = vid_id
                else:
                    video_url = f"https://www.youtube.com/watch?v={vid_id}"
                break
    except Exception as exc:
        log.warning("yt-dlp YouTube lookup failed: %s", exc)

    # Fallback to YouTube search page
    if not video_url:
        video_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)

    return open_browser(video_url)


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    config.setup_logging()
    log.info("Browser action test")

    result = open_browser("https://www.google.com")
    log.info("Result: %s", result)

    result2 = open_browser("youtube.com")
    log.info("Result: %s", result2)
