"""
actions/music.py — Cache-first music playback via yt-dlp + mpv.

Architecture:
    1. Lookup: check favourites.json for known alias → get URL
    2. Cache check: if URL was previously downloaded, use local file
    3. Download (online only): yt-dlp fetches audio to data/music_cache/
    4. Playback: mpv launched with Windows named-pipe IPC server
    5. Control: pause / resume / stop / volume via named pipe

Windows IPC specifics:
    mpv uses \\\\.\\pipe\\mpvsocket (not a filesystem socket).
    We communicate via the win32file API (pywin32).
    JSON commands are sent as newline-terminated UTF-8 strings.

Offline behaviour:
    - If a track is already cached, plays without any network access.
    - If not cached and network unavailable, reports failure gracefully.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

import config
from config import (
    FAVOURITES_FILE,
    MPV_EXECUTABLE,
    MPV_IPC_PATH,
    MPV_IPC_TIMEOUT_S,
    MUSIC_CACHE_DIR,
    YTDLP_AUDIO_FORMAT,
    YTDLP_MAX_FILESIZE,
    YTDLP_OUTPUT_TEMPLATE,
    YTDLP_RETRIES,
)

log = logging.getLogger(__name__)


# ─── IPC helpers (Windows named pipe) ────────────────────────────────────────

def _send_mpv_command(command: dict[str, Any]) -> bool:
    """
    Send a JSON command to the running mpv instance via Windows named pipe.

    Returns True on success, False if pipe not available.
    """
    try:
        import win32file  # type: ignore[import]
        import pywintypes  # type: ignore[import]
    except ImportError:
        log.error("pywin32 not installed (pip install pywin32) — mpv IPC unavailable")
        return False

    payload = (json.dumps(command) + "\n").encode("utf-8")
    try:
        handle = win32file.CreateFile(
            MPV_IPC_PATH,
            win32file.GENERIC_WRITE,
            0, None,
            win32file.OPEN_EXISTING,
            0, None,
        )
        win32file.WriteFile(handle, payload)
        win32file.CloseHandle(handle)
        return True
    except pywintypes.error as exc:
        log.debug("mpv IPC send failed: %s", exc)
        return False


# ─── Favourites management ────────────────────────────────────────────────────

def _load_favourites() -> dict[str, dict]:
    if not FAVOURITES_FILE.exists():
        return {}
    try:
        return json.loads(FAVOURITES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load favourites.json: %s", exc)
        return {}


def _save_favourites(data: dict[str, dict]) -> None:
    try:
        FAVOURITES_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        log.error("Failed to save favourites.json: %s", exc)


def _resolve_query(query: str) -> tuple[str | None, Path | None]:
    """
    Look up a query against favourites.

    Returns (url_or_None, cached_path_or_None).
    """
    favs = _load_favourites()
    key = query.strip().lower()

    if key in favs:
        entry = favs[key]
        cached = Path(entry["local_path"]) if entry.get("local_path") else None
        if cached and cached.exists():
            log.info("Cache hit: %s → %s", key, cached)
            return None, cached
        return entry.get("url"), None

    # Not a known alias — treat as a YouTube search term or direct URL
    if query.startswith(("http://", "https://")):
        return query, None
    return f"ytsearch1:{query}", None


# ─── yt-dlp downloader ────────────────────────────────────────────────────────

def _download_audio(url_or_query: str) -> Path | None:
    """
    Download audio from a URL or yt-dlp search query.

    Returns Path to the downloaded file on success, None on failure.
    """
    try:
        import yt_dlp  # type: ignore[import]
    except ImportError:
        log.error("yt-dlp not installed (pip install yt-dlp)")
        return None

    ydl_opts: dict[str, Any] = {
        "format": YTDLP_AUDIO_FORMAT,
        "outtmpl": YTDLP_OUTPUT_TEMPLATE,
        "quiet": True,
        "no_warnings": True,
        "retries": YTDLP_RETRIES,
        "max_filesize": YTDLP_MAX_FILESIZE,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    log.info("yt-dlp downloading: %s", url_or_query)
    downloaded_paths: list[str] = []

    class _PP(yt_dlp.postprocessor.PostProcessor):
        def run(self, info):  # type: ignore[override]
            downloaded_paths.append(info.get("filepath", ""))
            return [], info

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.add_post_processor(_PP(), when="post_process")
            info = ydl.extract_info(url_or_query, download=True)
            if not info:
                return None

            # Find the downloaded file
            if downloaded_paths:
                return Path(downloaded_paths[-1])

            # Fallback: search music_cache for most recently modified file
            files = sorted(
                MUSIC_CACHE_DIR.glob("*.mp3"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return files[0] if files else None

    except yt_dlp.utils.DownloadError as exc:
        log.error("yt-dlp download failed: %s", exc)
        return None


# ─── Main MusicPlayer class ───────────────────────────────────────────────────

class MusicPlayer:
    """
    Controls audio playback via mpv.

    Maintains a reference to the active mpv process and communicates
    via Windows named pipe IPC for runtime control (pause, volume, stop).
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None

    # ─── Playback ─────────────────────────────────────────────────────────────

    def _launch_mpv(self, path: Path | str) -> None:
        """Launch (or re-launch) mpv with IPC server on the configured pipe."""
        self.stop()  # kill any existing instance first

        cmd = [
            MPV_EXECUTABLE,
            f"--input-ipc-server={MPV_IPC_PATH}",
            "--no-video",
            "--audio-display=no",
            str(path),
        ]
        log.info("Launching mpv: %s", path)
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Give mpv a moment to open the pipe
            time.sleep(0.5)
        except FileNotFoundError:
            log.error(
                "mpv not found. Install via: choco install mpv  OR  scoop install mpv"
            )
            self._process = None

    def play(self, query: str) -> str:
        """
        Play music matching query.

        Lookup order:
          1. favourites.json → local cached path (offline-safe)
          2. favourites.json → URL → yt-dlp download → cache
          3. Treat query as yt-dlp search term → download → cache

        Returns human-readable result for TTS.
        """
        query = query.strip()

        # Control commands
        lower = query.lower()
        if lower == "pause":
            return self.pause()
        if lower in ("stop", "quit"):
            return self.stop()
        if lower.startswith("volume "):
            try:
                level = int(lower.split()[1])
                return self.set_volume(level)
            except (IndexError, ValueError):
                return "Please specify a volume level between 0 and 100."
        if lower == "resume":
            return self.resume()

        url, cached_path = _resolve_query(query)

        if cached_path:
            self._launch_mpv(cached_path)
            return f"Playing {cached_path.stem} from cache."

        if url is None:
            return f"I don't have '{query}' in favourites and couldn't find a URL."

        # Download
        downloaded = _download_audio(url)
        if downloaded is None:
            return (
                f"I couldn't download '{query}'. "
                "Check your internet connection or add it to favourites with a local path."
            )

        # Cache the result in favourites
        favs = _load_favourites()
        key = query.lower()
        if key not in favs:
            favs[key] = {"url": url if not url.startswith("ytsearch") else None,
                         "local_path": str(downloaded),
                         "description": query}
        else:
            favs[key]["local_path"] = str(downloaded)
        _save_favourites(favs)

        self._launch_mpv(downloaded)
        return f"Playing {downloaded.stem}."

    # ─── Controls ─────────────────────────────────────────────────────────────

    def pause(self) -> str:
        sent = _send_mpv_command({"command": ["cycle", "pause"]})
        return "Paused." if sent else "Nothing is playing."

    def resume(self) -> str:
        sent = _send_mpv_command({"command": ["set_property", "pause", False]})
        return "Resumed." if sent else "Nothing is playing."

    def stop(self) -> str:
        if self._process and self._process.poll() is None:
            _send_mpv_command({"command": ["quit"]})
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            return "Stopped."
        self._process = None
        return "Nothing was playing."

    def set_volume(self, level: int) -> str:
        level = max(0, min(100, level))
        sent = _send_mpv_command({"command": ["set_property", "volume", level]})
        return f"Volume set to {level}." if sent else "Nothing is playing."

    def is_playing(self) -> bool:
        return self._process is not None and self._process.poll() is None


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    config.setup_logging()
    log.info("Music action standalone test")

    player = MusicPlayer()

    # Test 1: play from favourites (lofi default)
    log.info("--- Test 1: play lofi ---")
    result = player.play("lofi")
    log.info("Result: %s", result)

    if player.is_playing():
        time.sleep(5)

        log.info("--- Test 2: volume 40 ---")
        log.info(player.set_volume(40))
        time.sleep(2)

        log.info("--- Test 3: pause ---")
        log.info(player.pause())
        time.sleep(1)

        log.info("--- Test 4: resume ---")
        log.info(player.resume())
        time.sleep(2)

        log.info("--- Test 5: stop ---")
        log.info(player.stop())
    else:
        log.warning("mpv did not start — check mpv is on PATH")

    log.info("Music tests done ✓")
