"""
actions/music.py — OFFLINE-FIRST music: downloader + Spotify resolver + player.

How it works now (this is a real offline library, not "open YouTube in Chrome"):

    1. Library      : every track is recorded in data/music_library.json via the
                      music_library module (downloaded mp3s carry a local_path).
    2. Download     : `download()` runs yt-dlp + ffmpeg to extract audio → MP3 in
                      data/music_cache/, then registers the track in the library.
    3. Resolve      : a YouTube URL is used as-is; an open.spotify.com link is
                      resolved (no API key) to a "Title Artist" search string; any
                      other text becomes a `ytsearch1:` query.
    4. Playback     : local files play through mpv (Windows named-pipe IPC for
                      pause / resume / volume / seek), with an ffplay fallback.
    5. Graceful     : if a track isn't cached and can't be downloaded, we fall
                      back to opening it on YouTube in the browser. Missing yt-dlp,
                      ffmpeg, mpv, pywin32 or network never raise — they return a
                      helpful message / None.

Windows IPC specifics:
    mpv listens on \\.\pipe\mpvsocket (not a filesystem socket). We talk to it
    via the win32file API (pywin32). Commands are newline-terminated UTF-8 JSON.

Position reporting is computed in Python from a monotonic start timestamp (minus
accumulated pause time) rather than read back over the pipe — simpler and robust.
"""

from __future__ import annotations

import json
import logging
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import config
from config import (
    FFPLAY_EXECUTABLE,
    MPV_EXECUTABLE,
    MPV_IPC_PATH,
    MUSIC_CACHE_DIR,
    SPOTIFY_OEMBED_URL,
    YTDLP_AUDIO_FORMAT,
    YTDLP_MAX_FILESIZE,
    YTDLP_MP3_QUALITY,
    YTDLP_OUTPUT_TEMPLATE,
    YTDLP_RETRIES,
)

from actions import music_library

log = logging.getLogger(__name__)


# ─── URL helpers ──────────────────────────────────────────────────────────────

def is_spotify_url(s: str) -> bool:
    s = (s or "").strip().lower()
    return "open.spotify.com" in s


def is_youtube_url(s: str) -> bool:
    s = (s or "").strip().lower()
    return ("youtube.com" in s) or ("youtu.be" in s)


def _is_url(s: str) -> bool:
    return (s or "").strip().lower().startswith(("http://", "https://"))


# ─── Spotify resolver (best-effort, NO API key) ───────────────────────────────

def resolve_spotify(url: str) -> str:
    """
    Resolve an open.spotify.com track/album link to a YouTube search string
    "Title Artist" — without any API key.

    Strategy:
        1. Spotify's public oEmbed endpoint gives a clean track title.
        2. The track page's <meta property="og:title"/"og:description"> often
           carries the artist ("Song · Artist · Song · 2020"); we mine that to
           append the artist when oEmbed alone lacks it.

    Returns "" on any failure (the caller then falls back). Never raises.
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        import httpx  # type: ignore[import]
    except ImportError:
        log.warning("httpx not installed — cannot resolve Spotify links")
        return ""

    title = ""
    artist = ""
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as client:
            # 1) oEmbed → title
            try:
                oembed_url = f"{SPOTIFY_OEMBED_URL}?url={urllib.parse.quote(url, safe='')}"
                r = client.get(oembed_url)
                if r.status_code == 200:
                    data = r.json()
                    title = (data.get("title") or "").strip()
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                log.debug("Spotify oEmbed failed: %s", exc)

            # 2) page og:title / og:description → recover artist
            try:
                r = client.get(url)
                if r.status_code == 200:
                    html = r.text
                    og_title = _meta_content(html, "og:title")
                    og_desc = _meta_content(html, "og:description")
                    if og_title and not title:
                        title = og_title.strip()
                    artist = _artist_from_spotify_meta(og_desc, og_title, title)
            except httpx.HTTPError as exc:
                log.debug("Spotify page fetch failed: %s", exc)
    except Exception as exc:  # be paranoid — never propagate
        log.debug("Spotify resolve error: %s", exc)
        return ""

    parts = [p for p in (title, artist) if p]
    result = " ".join(parts).strip()
    if result:
        log.info("Spotify resolved → %r", result)
    return result


def _meta_content(html: str, prop: str) -> str:
    """Extract the content of a <meta property="prop" content="..."> tag."""
    import re
    # property may come before or after content; handle either order, any quoting.
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(prop)}["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            import html as _htmlmod
            return _htmlmod.unescape(m.group(1))
    return ""


def _artist_from_spotify_meta(og_desc: str, og_title: str, title: str) -> str:
    """
    Spotify og:description looks like "Song · Artist · Song · 2020" or
    "Artist · Song · 2020 · N songs". Pull the segment that isn't the title.
    """
    for source in (og_desc, og_title):
        if not source:
            continue
        segments = [seg.strip() for seg in source.split("·") if seg.strip()]
        for seg in segments:
            low = seg.lower()
            if title and low == title.lower():
                continue
            if low.isdigit():            # a year
                continue
            if "song" in low and any(c.isdigit() for c in low):  # "12 songs"
                continue
            if low.startswith("song ") or low == "song":
                continue
            return seg
    return ""


# ─── yt-dlp downloader ────────────────────────────────────────────────────────

def _resolve_input(query_or_url: str) -> tuple[str, str]:
    """
    Turn arbitrary user input into a yt-dlp target.

    Returns (ytdlp_target, source) where source ∈ {"youtube", "spotify"}.
    """
    q = (query_or_url or "").strip()
    if is_spotify_url(q):
        search = resolve_spotify(q)
        if search:
            return f"ytsearch1:{search}", "spotify"
        # resolver failed — last-ditch: search for the raw url text
        return f"ytsearch1:{q}", "spotify"
    if is_youtube_url(q) or _is_url(q):
        return q, "youtube"
    return f"ytsearch1:{q}", "youtube"


def download(query_or_url: str, on_event: Callable[[str], None] | None = None) -> dict | None:
    """
    Blocking. Resolve input → download best audio as MP3 into MUSIC_CACHE_DIR →
    register it in the library. Returns the library track dict, or None on failure.

    `on_event` (optional) receives short status strings; it's best-effort and
    any exception it raises is swallowed.
    """
    def _emit(msg: str) -> None:
        if on_event is None:
            return
        try:
            on_event(msg)
        except Exception:  # pragma: no cover - never let a callback break us
            pass

    query_or_url = (query_or_url or "").strip()
    if not query_or_url:
        return None

    try:
        import yt_dlp  # type: ignore[import]
    except ImportError:
        log.error("yt-dlp not installed (pip install yt-dlp) — cannot download")
        _emit("yt-dlp not installed")
        return None

    if not _ffmpeg_available():
        log.error("ffmpeg not found on PATH — cannot extract MP3 audio")
        _emit("ffmpeg not installed")
        return None

    target, source = _resolve_input(query_or_url)
    _emit(f"Searching: {query_or_url}")

    ydl_opts: dict[str, Any] = {
        "format": YTDLP_AUDIO_FORMAT,
        "outtmpl": YTDLP_OUTPUT_TEMPLATE,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": YTDLP_RETRIES,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(YTDLP_MP3_QUALITY),
        }],
    }
    # yt-dlp wants max_filesize in BYTES (int). config holds a human string
    # like "100m" — parse it; drop the cap if it can't be parsed (a raw string
    # makes yt-dlp raise: "'>' not supported between 'int' and 'str'").
    max_bytes = _max_filesize_bytes(yt_dlp)
    if max_bytes:
        ydl_opts["max_filesize"] = max_bytes

    downloaded_paths: list[str] = []

    class _PP(yt_dlp.postprocessor.PostProcessor):
        def run(self, info):  # type: ignore[override]
            fp = info.get("filepath") or info.get("_filename") or ""
            if fp:
                downloaded_paths.append(fp)
            return [], info

    log.info("yt-dlp downloading: %s", target)
    _emit("Downloading…")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.add_post_processor(_PP(), when="post_process")
            info = ydl.extract_info(target, download=True)
    except yt_dlp.utils.DownloadError as exc:
        log.error("yt-dlp download failed: %s", exc)
        _emit("Download failed")
        return None
    except Exception as exc:  # network, ffmpeg, anything — degrade gracefully
        log.error("yt-dlp unexpected error: %s", exc)
        _emit("Download failed")
        return None

    if not info:
        return None
    # ytsearch returns a playlist-like dict with "entries"
    if isinstance(info, dict) and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else info

    title = (info.get("title") or "").strip() if isinstance(info, dict) else ""
    artist = ""
    if isinstance(info, dict):
        artist = (info.get("artist") or info.get("uploader")
                  or info.get("channel") or "").strip()
    try:
        duration = int(info.get("duration") or 0) if isinstance(info, dict) else 0
    except (TypeError, ValueError):
        duration = 0
    source_url = ""
    if isinstance(info, dict):
        source_url = (info.get("webpage_url") or info.get("original_url") or "").strip()
    if not source_url and (is_youtube_url(query_or_url) or _is_url(query_or_url)):
        source_url = query_or_url

    # Locate the produced mp3.
    final_path = _pick_downloaded_mp3(downloaded_paths, info)
    if not final_path:
        log.error("Download completed but no mp3 file was found")
        _emit("No file produced")
        return None

    if not title:
        title = final_path.stem

    track = music_library.add_track(
        title=title,
        artist=artist,
        source=source,
        source_url=source_url,
        local_path=str(final_path),
        duration=duration,
    )
    log.info("Downloaded + library-added: %s (%s)", track.get("title"), final_path.name)
    _emit(f"Saved: {track.get('title')}")
    return track


def _pick_downloaded_mp3(downloaded_paths: list[str], info: Any) -> Path | None:
    """Resolve the final mp3 path from PP output / info dict / cache scan."""
    # 1) trust the postprocessor's reported path, swapping ext to .mp3
    for raw in reversed(downloaded_paths):
        if not raw:
            continue
        p = Path(raw)
        if p.suffix.lower() == ".mp3" and p.exists():
            return p
        mp3 = p.with_suffix(".mp3")
        if mp3.exists():
            return mp3

    # 2) info dict
    if isinstance(info, dict):
        raw = info.get("filepath") or info.get("_filename") or ""
        if raw:
            mp3 = Path(raw).with_suffix(".mp3")
            if mp3.exists():
                return mp3

    # 3) most-recently-modified mp3 in the cache
    try:
        files = sorted(MUSIC_CACHE_DIR.glob("*.mp3"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if files and (time.time() - files[0].stat().st_mtime) < 600:
            return files[0]
    except OSError:
        pass
    return None


def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def _max_filesize_bytes(yt_dlp_mod: Any) -> int | None:
    """Convert config.YTDLP_MAX_FILESIZE ("100m") to an int byte count for
    yt-dlp. Returns None (no cap) if it can't be parsed."""
    raw = YTDLP_MAX_FILESIZE
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    try:
        val = yt_dlp_mod.utils.parse_filesize(str(raw))
        return int(val) if val else None
    except Exception:
        return None


# ─── IPC helpers (Windows named pipe) ────────────────────────────────────────

def _send_mpv_command(command: dict[str, Any]) -> bool:
    """
    Send a JSON command to the running mpv instance via Windows named pipe.
    Returns True on success, False if the pipe is unavailable.
    """
    try:
        import win32file  # type: ignore[import]
        import pywintypes  # type: ignore[import]
    except ImportError:
        log.debug("pywin32 not installed — mpv IPC unavailable")
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


# ─── MusicPlayer (offline-first) ──────────────────────────────────────────────

class MusicPlayer:
    """
    Offline-first audio player.

    Plays local mp3s from the library through mpv (named-pipe IPC for runtime
    control), with an ffplay fallback. Queue support powers next()/previous().
    Like / favourite / stats are backed by the music_library.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._using_ffplay: bool = False

        self._current_id: str = ""
        self._current_title: str = ""
        self._current_artist: str = ""
        self._current_duration: int = 0

        self._queue: list[str] = []
        self._index: int = 0

        self._volume: int = 100
        self._paused: bool = False

        # playback modes
        self._shuffle: bool = False
        self._repeat: str = "off"          # off | one | all

        # auto-advance: a per-track token + RLock guard transitions so the
        # end-watcher thread and GUI/agent callers can't race over the process.
        self._lock = threading.RLock()
        self._play_token: int = 0

        # sleep timer
        self._sleep_timer: threading.Timer | None = None
        self._sleep_deadline: float = 0.0

        # position bookkeeping (computed in Python — no pipe read-back)
        self._start_monotonic: float = 0.0
        self._pause_started: float = 0.0
        self._paused_accum: float = 0.0

    # ─── Process launch (mpv → ffplay) ──────────────────────────────────────

    def _launch_mpv(self, path: Path | str) -> None:
        """Launch (or re-launch) mpv with IPC on the configured pipe; fall back
        to ffplay if mpv is unavailable."""
        self.stop()

        cmd_mpv = [
            MPV_EXECUTABLE,
            f"--input-ipc-server={MPV_IPC_PATH}",
            "--no-video",
            "--audio-display=no",
            f"--volume={self._volume}",
        ]
        # Route to the user-chosen speaker/headset (Settings → Audio) if we can
        # map it to an mpv device id; otherwise mpv uses the system default.
        try:
            import config
            import audio_devices
            if config.AUDIO_OUTPUT_DEVICE:
                dev = audio_devices.mpv_audio_device(config.AUDIO_OUTPUT_DEVICE)
                if dev:
                    cmd_mpv.append(f"--audio-device={dev}")
        except Exception:
            pass
        cmd_mpv.append(str(path))
        log.info("Launching mpv: %s", path)
        try:
            self._process = subprocess.Popen(
                cmd_mpv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._using_ffplay = False
            time.sleep(0.5)  # let mpv open the pipe
            return
        except FileNotFoundError:
            log.warning("mpv not found — falling back to ffplay")

        ffplay = FFPLAY_EXECUTABLE if (FFPLAY_EXECUTABLE and Path(FFPLAY_EXECUTABLE).exists()) \
            else shutil.which("ffplay")
        if ffplay:
            cmd_ff = [str(ffplay), "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]
            log.info("Launching ffplay: %s", path)
            try:
                self._process = subprocess.Popen(
                    cmd_ff, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._using_ffplay = True
                return
            except FileNotFoundError:
                log.error("ffplay not found either")

        log.error("No audio player available. Install mpv: winget install mpv")
        self._process = None

    def _begin_playback(self, track: dict) -> None:
        """Common bookkeeping when starting a local track."""
        self._current_id = track.get("id", "")
        self._current_title = track.get("title", "") or "Unknown"
        self._current_artist = track.get("artist", "") or ""
        try:
            self._current_duration = int(track.get("duration") or 0)
        except (TypeError, ValueError):
            self._current_duration = 0
        self._paused = False
        self._start_monotonic = time.monotonic()
        self._pause_started = 0.0
        self._paused_accum = 0.0
        if self._current_id:
            music_library.record_play(self._current_id)
        # Mark this track as the active one and watch for its natural end so we
        # can auto-advance the queue (mpv/ffplay only ever play a single file).
        self._play_token += 1
        if self._process is not None:
            self._start_end_watcher(self._process, self._play_token)

    # ─── Auto-advance (queue continuation) ───────────────────────────────────

    def _start_end_watcher(self, proc: subprocess.Popen, token: int) -> None:
        """Wait (in a daemon thread) for *proc* to exit, then advance the queue
        — but only if this track is still the active one and wasn't paused or
        intentionally stopped/replaced (both bump _play_token)."""
        def _wait() -> None:
            try:
                proc.wait()
            except Exception:
                return
            if token != self._play_token or self._paused:
                return  # superseded (stop / next / new track) or just paused
            try:
                self._auto_advance()
            except Exception as exc:
                log.debug("auto-advance failed: %s", exc)
        threading.Thread(target=_wait, name="music-endwatch", daemon=True).start()

    def _pick_random_index(self) -> int:
        if len(self._queue) <= 1:
            return self._index
        return random.choice([i for i in range(len(self._queue)) if i != self._index])

    def _auto_advance(self) -> None:
        """Decide what to play when the current track ends naturally."""
        with self._lock:
            if self._repeat == "one":
                self._play_index()
                return
            if not self._queue:
                return
            if self._shuffle and len(self._queue) > 1:
                self._index = self._pick_random_index()
                self._play_index()
                return
            if self._index + 1 < len(self._queue):
                self._index += 1
                self._play_index()
            elif self._repeat == "all":
                self._index = 0
                self._play_index()
            # else: reached the end with repeat off — leave it stopped.

    def _play_local(self, track: dict) -> str:
        lp = track.get("local_path") or ""
        if not lp or not Path(lp).exists():
            return ""
        self._launch_mpv(lp)
        if self._process is None:
            return "I couldn't start the audio player. Install mpv (winget install mpv)."
        self._begin_playback(track)
        artist = f" by {self._current_artist}" if self._current_artist else ""
        return f"Now playing: {self._current_title}{artist}."

    # ─── Play ────────────────────────────────────────────────────────────────

    def play(self, query: str = "", track_id: str = "") -> str:
        """
        Play a track. Priority:
          1. track_id with a local file → play locally.
          2. query → an already-downloaded matching track → play locally.
          3. query → download() → play the new local file.
          4. download failed → open it on YouTube in the browser (graceful).
        """
        # control keywords kept for backward compatibility with voice commands
        lower = (query or "").strip().lower()
        if lower in ("pause",):
            return self.pause()
        if lower in ("resume", "unpause"):
            return self.resume()
        if lower in ("stop", "quit"):
            return self.stop()
        if lower in ("next", "skip"):
            return self.next()
        if lower in ("previous", "prev", "back"):
            return self.previous()
        if lower.startswith("volume "):
            try:
                return self.set_volume(int(lower.split()[1]))
            except (IndexError, ValueError):
                return "Please specify a volume level between 0 and 100."

        # 1) explicit track id
        if track_id:
            track = music_library.get_track(track_id)
            if track and (track.get("local_path") and Path(track["local_path"]).exists()):
                self._queue = [track_id]
                self._index = 0
                return self._play_local(track)
            if track and not query:
                query = track.get("title", "")

        query = (query or "").strip()
        if not query:
            return "What would you like me to play?"

        # 2) already downloaded? match title/artist/url substring
        match = self._find_downloaded(query)
        if match:
            self._queue = [match["id"]]
            self._index = 0
            log.info("Cache hit for %r → %s", query, match.get("title"))
            return self._play_local(match)

        # 3) download then play
        track = download(query)
        if track and track.get("local_path") and Path(track["local_path"]).exists():
            self._queue = [track["id"]]
            self._index = 0
            return self._play_local(track)

        # 4) graceful fallback — open it on YouTube
        log.info("Download unavailable for %r — falling back to browser", query)
        try:
            from actions.browser import open_youtube
            open_youtube(query)
            self._current_title = query
            self._current_id = ""
            return (f"I couldn't download '{query}' for offline play, "
                    f"so I opened it on YouTube in your browser instead.")
        except Exception as exc:
            log.error("Browser fallback failed: %s", exc)
            return (f"I couldn't download or open '{query}'. "
                    f"Check that yt-dlp and ffmpeg are installed.")

    # Filler words that shouldn't decide a match ("play the rocket bay song").
    _MATCH_STOPWORDS = {
        "the", "a", "an", "of", "to", "for", "by", "feat", "ft", "with",
        "official", "audio", "video", "lyric", "lyrics", "hd", "hq", "mv",
        "music", "song", "songs", "track", "play", "please", "cover", "remix",
        "version", "full", "live",
    }

    @classmethod
    def _match_tokens(cls, text: str) -> set[str]:
        return {
            w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if w and w not in cls._MATCH_STOPWORDS
        }

    def _find_downloaded(self, query: str) -> dict | None:
        """Find a downloaded library track matching ``query``.

        Two passes so vague requests still resolve to something already on disk
        (instead of triggering a slow, often-wrong re-download):
          1. precise — the whole query is a substring of title/artist/url;
          2. fuzzy   — best token overlap, if it covers most of the query words.
        """
        q = query.strip().lower()
        if not q:
            return None
        tracks = music_library.list_tracks(filter="downloaded")

        # 1) precise substring match (fast, unambiguous)
        for t in tracks:
            hay = " ".join((t.get("title", ""), t.get("artist", ""),
                            t.get("source_url", ""))).lower()
            if q in hay:
                return t

        # 2) fuzzy token-overlap match
        q_tokens = self._match_tokens(q)
        if not q_tokens:
            return None
        best, best_score = None, 0.0
        for t in tracks:
            t_tokens = self._match_tokens(t.get("title", "") + " " + t.get("artist", ""))
            if not t_tokens:
                continue
            overlap = len(q_tokens & t_tokens)
            if not overlap:
                continue
            score = overlap / len(q_tokens)   # fraction of query words present
            if score > best_score:
                best, best_score = t, score
        # need most of the meaningful query words to line up before we trust it
        if best is not None and best_score >= 0.6:
            log.info("Fuzzy cache hit for %r → %s (score=%.2f)",
                     query, best.get("title"), best_score)
            return best
        return None

    # ─── Queue navigation ────────────────────────────────────────────────────

    def play_queue(self, track_ids: list[str], index: int = 0) -> str:
        """Store a queue + index and play that track by id."""
        with self._lock:
            self._queue = [t for t in (track_ids or []) if t]
            if not self._queue:
                return "The queue is empty."
            self._index = max(0, min(index, len(self._queue) - 1))
            return self._play_index()

    def _play_index(self) -> str:
        if not self._queue:
            return "The queue is empty."
        track_id = self._queue[self._index]
        track = music_library.get_track(track_id)
        if not track:
            return "That track is no longer in the library."
        if track.get("local_path") and Path(track["local_path"]).exists():
            return self._play_local(track)
        # not downloaded — try to fetch by title
        return self.play(query=track.get("title", ""), track_id=track_id)

    def next(self) -> str:
        with self._lock:
            if not self._queue:
                return "There's nothing queued."
            if self._shuffle and len(self._queue) > 1:
                self._index = self._pick_random_index()
            else:
                self._index = (self._index + 1) % len(self._queue)  # wrap-around
            return self._play_index()

    def previous(self) -> str:
        with self._lock:
            if not self._queue:
                return "There's nothing queued."
            self._index = max(0, self._index - 1)  # clamp at start
            return self._play_index()

    # ─── Playback modes ───────────────────────────────────────────────────────

    def set_shuffle(self, on: bool | str) -> str:
        if isinstance(on, str):
            on = on.strip().lower() in ("on", "true", "1", "yes", "shuffle")
        self._shuffle = bool(on)
        return "Shuffle on." if self._shuffle else "Shuffle off."

    def toggle_shuffle(self) -> str:
        return self.set_shuffle(not self._shuffle)

    def set_repeat(self, mode: str) -> str:
        mode = (mode or "").strip().lower()
        aliases = {
            "": "all", "on": "all", "all": "all", "queue": "all", "loop": "all",
            "one": "one", "song": "one", "track": "one", "single": "one",
            "off": "off", "none": "off", "no": "off",
        }
        self._repeat = aliases.get(mode, "all")
        return {
            "off": "Repeat off.",
            "one": "Repeating the current song.",
            "all": "Repeating the whole queue.",
        }[self._repeat]

    # ─── Collections (play a whole filter as a queue) ─────────────────────────

    def play_collection(self, which: str = "all") -> str:
        """Queue up every downloaded track in a collection and play it.

        which ∈ {all, liked, favourites, downloaded}. Respects the shuffle flag.
        """
        which = (which or "all").strip().lower()
        flt = {"favorites": "favourites"}.get(which, which)
        if flt not in ("all", "liked", "favourites", "downloaded"):
            flt = "all"
        playable = [
            t for t in music_library.list_tracks(filter=flt)
            if t.get("local_path") and Path(t["local_path"]).exists()
        ]
        if not playable:
            label = "downloaded" if flt in ("all", "downloaded") else flt
            return f"No {label} tracks to play yet. Download some first."
        ids = [t["id"] for t in playable]
        if self._shuffle:
            random.shuffle(ids)
        return self.play_queue(ids, 0)

    # ─── Sleep timer ──────────────────────────────────────────────────────────

    def set_sleep_timer(self, minutes: int | float | str) -> str:
        try:
            mins = float(str(minutes).strip())
        except (TypeError, ValueError):
            return "How many minutes? e.g. sleep timer 20."
        self.cancel_sleep_timer()
        if mins <= 0:
            return "Sleep timer off."
        self._sleep_deadline = time.monotonic() + mins * 60
        self._sleep_timer = threading.Timer(mins * 60, self._sleep_fire)
        self._sleep_timer.daemon = True
        self._sleep_timer.start()
        return f"I'll stop the music in {int(mins)} minute{'s' if int(mins) != 1 else ''}."

    def _sleep_fire(self) -> None:
        self._sleep_timer = None
        self._sleep_deadline = 0.0
        self.stop()

    def cancel_sleep_timer(self) -> str:
        if self._sleep_timer is not None:
            self._sleep_timer.cancel()
            self._sleep_timer = None
            self._sleep_deadline = 0.0
            return "Sleep timer cancelled."
        return "No sleep timer is set."

    def _sleep_remaining(self) -> int:
        if self._sleep_deadline > 0:
            return max(0, int(self._sleep_deadline - time.monotonic()))
        return 0

    # ─── Transport controls ───────────────────────────────────────────────────

    def pause(self) -> str:
        if not self.is_playing():
            return "Nothing is playing."
        if self._using_ffplay:
            return "Pause isn't supported without mpv — say 'stop' to stop playback."
        if _send_mpv_command({"command": ["set_property", "pause", True]}):
            if not self._paused:
                self._paused = True
                self._pause_started = time.monotonic()
            return "Paused."
        return "Nothing is playing."

    def resume(self) -> str:
        if not self.is_playing():
            return "Nothing is playing."
        if self._using_ffplay:
            return "Resume isn't supported without mpv. Say the song name again to restart."
        if _send_mpv_command({"command": ["set_property", "pause", False]}):
            if self._paused:
                self._paused = False
                self._paused_accum += time.monotonic() - self._pause_started
                self._pause_started = 0.0
            return "Resumed."
        return "Nothing is playing."

    def toggle_pause(self) -> str:
        return self.resume() if self._paused else self.pause()

    def stop(self) -> str:
        with self._lock:
            self._play_token += 1  # invalidate any end-watcher: this stop is intentional
            was_playing = self._process is not None and self._process.poll() is None
            if was_playing:
                if not self._using_ffplay:
                    _send_mpv_command({"command": ["quit"]})
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None
            self._using_ffplay = False
            self._paused = False
            self._start_monotonic = 0.0
            self._pause_started = 0.0
            self._paused_accum = 0.0
            return "Stopped." if was_playing else "Nothing was playing."

    def set_volume(self, level: int) -> str:
        try:
            level = int(level)
        except (TypeError, ValueError):
            return "Please specify a volume level between 0 and 100."
        level = max(0, min(100, level))
        self._volume = level
        if self._using_ffplay:
            return "Volume control isn't supported without mpv."
        sent = _send_mpv_command({"command": ["set_property", "volume", level]})
        return f"Volume set to {level}." if sent else f"Volume will be {level} next track."

    def seek(self, seconds: int) -> str:
        """Best-effort absolute seek (mpv only)."""
        if not self.is_playing():
            return "Nothing is playing."
        if self._using_ffplay:
            return "Seeking isn't supported without mpv."
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return "Please give a position in seconds."
        seconds = max(0, seconds)
        if _send_mpv_command({"command": ["seek", seconds, "absolute"]}):
            # realign python position clock to the seek target
            self._start_monotonic = time.monotonic() - seconds
            self._paused_accum = 0.0
            if self._paused:
                self._pause_started = time.monotonic()
            return f"Seeked to {seconds}s."
        return "Couldn't seek."

    # ─── Status ────────────────────────────────────────────────────────────────

    def is_playing(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def current_song(self) -> str | None:
        return self._current_title if self.is_playing() else None

    def _position(self) -> int:
        """Elapsed playback seconds, computed in Python; clamped to [0, duration]."""
        if not self.is_playing() or self._start_monotonic <= 0:
            return 0
        elapsed = time.monotonic() - self._start_monotonic - self._paused_accum
        if self._paused and self._pause_started:
            elapsed -= (time.monotonic() - self._pause_started)
        pos = int(max(0.0, elapsed))
        if self._current_duration > 0:
            pos = min(pos, self._current_duration)
        return pos

    def now_playing(self) -> dict:
        return {
            "playing": self.is_playing(),
            "paused": self._paused,
            "track_id": self._current_id,
            "title": self._current_title,
            "artist": self._current_artist,
            "duration": self._current_duration,
            "position": self._position(),
            "volume": self._volume,
            "shuffle": self._shuffle,
            "repeat": self._repeat,
            "queue_length": len(self._queue),
            "sleep_remaining": self._sleep_remaining(),
        }

    # ─── Backward-compatible like / favourite / stats ──────────────────────────

    def like(self) -> str:
        """Mark the current track as liked (library-backed)."""
        if not self._current_id:
            return "Nothing is playing right now."
        music_library.set_like(self._current_id, True)
        return f"Got it — marked '{self._current_title}' as liked."

    def dislike(self) -> str:
        """Un-like the current track and stop playback."""
        if not self._current_id:
            return "Nothing is playing right now."
        title = self._current_title
        music_library.set_like(self._current_id, False)
        self.stop()
        return f"Noted — skipped '{title}' and removed the like. I'll avoid it."

    def save_fav(self) -> str:
        """Save the current track as a favourite (also liked)."""
        if not self._current_id:
            return "Nothing is playing right now."
        music_library.set_favourite(self._current_id, True)
        music_library.set_like(self._current_id, True)
        return f"'{self._current_title}' saved as a favourite."

    def show_top_played(self, n: int = 5) -> str:
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 5
        ranked = sorted(
            (t for t in music_library.all_tracks() if t.get("play_count", 0) > 0),
            key=lambda t: t.get("play_count", 0), reverse=True,
        )[: max(1, n)]
        if not ranked:
            return "I haven't tracked any plays yet. Start playing some music!"
        lines = [f"{i+1}. {t.get('title','?')} ({t.get('play_count',0)}x)"
                 for i, t in enumerate(ranked)]
        return "Your most played songs:\n" + "\n".join(lines)

    def show_favourites(self) -> str:
        favs = music_library.list_tracks(filter="favourites")
        if not favs:
            return "No favourites saved yet. Like a song while it plays!"
        return "Your favourites:\n" + "\n".join(f"• {t.get('title','?')}" for t in favs)


# ─── Offline local-folder import ──────────────────────────────────────────────

_AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma"}


def _read_audio_meta(path: Path) -> tuple[str, str, int]:
    """(title, artist, duration_seconds). Uses mutagen if available; otherwise
    falls back to the filename stem. Never raises."""
    title, artist, duration = path.stem, "", 0
    try:
        from mutagen import File as _MFile  # type: ignore[import]
        mf = _MFile(str(path), easy=True)
        if mf is not None:
            tags = getattr(mf, "tags", None) or {}
            title = (tags.get("title", [title]) or [title])[0] or title
            artist = (tags.get("artist", [""]) or [""])[0] or ""
            info = getattr(mf, "info", None)
            if info is not None and getattr(info, "length", 0):
                duration = int(info.length)
    except Exception:
        pass  # mutagen missing or unreadable tags — filename fallback is fine
    return title, artist, duration


def import_folder(folder: str) -> dict:
    """
    Scan *folder* (recursively) for audio files and add them to the library.

    Fully offline — no yt-dlp, no network. Existing tracks (same local_path
    basis) are upserted, so re-running is safe. Returns {"added", "scanned"}.
    """
    base = Path((folder or "").strip().strip('"').strip("'"))
    if not base.exists() or not base.is_dir():
        return {"added": 0, "scanned": 0, "error": f"Folder not found: {base}"}
    files = [p for p in base.rglob("*")
             if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]
    added = 0
    for p in files:
        try:
            title, artist, duration = _read_audio_meta(p)
            music_library.add_track(
                title=title or p.stem, artist=artist, source="local",
                source_url=str(p),  # local path doubles as the stable id basis
                local_path=str(p), duration=duration,
            )
            added += 1
        except Exception as exc:
            log.debug("import_folder: skipped %s (%s)", p, exc)
    log.info("import_folder: added %d/%d audio files from %s", added, len(files), base)
    return {"added": added, "scanned": len(files)}


# ─── Module-level compat shim + singleton ─────────────────────────────────────

def save_favourite(song: str, url: str = "") -> str:
    """
    Thin compatibility shim over the library: mark *song* as a favourite (and
    liked). If *url* is given it's stored as the track's source_url so a future
    play can find/download it directly.
    """
    song = (song or "").strip()
    if not song:
        return "No song to save — nothing is playing."
    track = music_library.add_track(title=song, source_url=url,
                                    source="youtube" if url else "local")
    music_library.set_favourite(track["id"], True)
    music_library.set_like(track["id"], True)
    return f"'{song}' saved as a favourite."


_PLAYER: MusicPlayer | None = None


def get_player() -> MusicPlayer:
    """Return the lazily-created shared MusicPlayer (safe across threads —
    playback is subprocess + named-pipe based)."""
    global _PLAYER
    if _PLAYER is None:
        _PLAYER = MusicPlayer()
    return _PLAYER


# ─── Standalone smoke test (no network) ───────────────────────────────────────

if __name__ == "__main__":
    config.setup_logging()
    log.info("music.py standalone test (offline)")

    log.info("is_spotify_url: %s", is_spotify_url("https://open.spotify.com/track/x"))
    log.info("is_youtube_url: %s", is_youtube_url("https://youtu.be/abc"))

    player = get_player()
    log.info("now_playing (idle): %s", player.now_playing())
    log.info("next (no queue): %s", player.next())
    log.info("show_top_played: %s", player.show_top_played(3))
    log.info("music.py test done")
