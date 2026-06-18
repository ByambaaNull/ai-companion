"""
actions/video.py — YouTube (and yt-dlp-supported) VIDEO downloader.

Unlike actions/music.py (which extracts audio → MP3), this keeps the full video
and — importantly — lets the user CHOOSE the resolution instead of always
grabbing the highest one available.

Flow:
    1. probe()      → inspect a URL with yt-dlp (no download) and return the list
                      of resolutions actually available for it, with rough file
                      sizes, so the UI can offer a real choice.
    2. download()   → fetch the chosen height (or a specific format) and mux the
                      best matching audio into an MP4 with ffmpeg.
    3. list_downloaded() → enumerate everything already saved in VIDEO_CACHE_DIR.

Everything degrades gracefully: a missing yt-dlp / ffmpeg or a network failure
never raises — it returns a dict carrying an "error" string the caller can show.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from config import (
    VIDEO_CACHE_DIR,
    VIDEO_DEFAULT_HEIGHT,
    VIDEO_EXTS,
    VIDEO_MERGE_FORMAT,
    VIDEO_OUTPUT_TEMPLATE,
    YTDLP_RETRIES,
)

log = logging.getLogger(__name__)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def _is_url(s: str) -> bool:
    return (s or "").strip().lower().startswith(("http://", "https://"))


def _fmt_size(num: float | int | None) -> str:
    """Human-readable byte size, e.g. 73.4 MB. '' when unknown."""
    if not num:
        return ""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _resolution_label(height: int, fps: int | None) -> str:
    label = f"{height}p"
    if fps and fps >= 50:           # 50/60 fps streams get the suffix YouTube uses
        label += str(int(round(fps / 10) * 10))
    if height >= 2160:
        label += " (4K)"
    elif height >= 1440:
        label += " (2K)"
    return label


# ─── probe: what resolutions are available? ──────────────────────────────────

def probe(url: str) -> dict:
    """
    Inspect *url* and return available resolutions WITHOUT downloading.

    Returns:
        {
          "ok": True,
          "title": "...", "uploader": "...", "duration": 213,
          "thumbnail": "https://...",
          "webpage_url": "https://...",
          "resolutions": [
             {"height": 1080, "label": "1080p", "fps": 30, "ext": "mp4",
              "filesize": 73400000, "filesize_label": "73.4 MB",
              "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]"},
             ...
          ],
          "audio_only": {"label": "Audio only (MP3)", "format": "bestaudio/best",
                         "audio_only": True},
        }
    or {"ok": False, "error": "..."} on failure.
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "No link given. Paste a YouTube video link."}
    if not _is_url(url):
        # treat plain text as a YouTube search for the first match
        url = f"ytsearch1:{url}"

    try:
        import yt_dlp  # type: ignore[import]
    except ImportError:
        return {"ok": False, "error": "yt-dlp is not installed (pip install yt-dlp)."}

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        log.warning("video probe failed: %s", exc)
        return {"ok": False, "error": "Couldn't read that link — check it's a valid video URL."}
    except Exception as exc:  # network etc.
        log.warning("video probe error: %s", exc)
        return {"ok": False, "error": f"Couldn't read that link ({exc})."}

    if isinstance(info, dict) and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else info
    if not isinstance(info, dict):
        return {"ok": False, "error": "No video information found."}

    # Collapse the raw format list into one entry per video height, keeping the
    # largest known file size at each height (≈ best quality for that resolution).
    by_height: dict[int, dict] = {}
    for f in info.get("formats", []) or []:
        if f.get("vcodec") in (None, "none"):
            continue  # audio-only stream
        h = f.get("height")
        if not h:
            continue
        fs = f.get("filesize") or f.get("filesize_approx") or 0
        cur = by_height.get(h)
        if cur is None or fs > cur.get("filesize", 0):
            by_height[h] = {
                "height": int(h),
                "fps": int(f["fps"]) if f.get("fps") else None,
                "ext": f.get("ext") or "mp4",
                "filesize": int(fs) if fs else 0,
            }

    resolutions: list[dict] = []
    for h in sorted(by_height.keys(), reverse=True):
        e = by_height[h]
        resolutions.append({
            "height": e["height"],
            "label": _resolution_label(e["height"], e["fps"]),
            "fps": e["fps"],
            "ext": e["ext"],
            "filesize": e["filesize"],
            "filesize_label": _fmt_size(e["filesize"]),
            # height ceiling selector → robust across sites; audio merged in.
            "format": (f"bestvideo[height<={e['height']}]+bestaudio/"
                       f"best[height<={e['height']}]"),
        })

    return {
        "ok": True,
        "title": (info.get("title") or "").strip() or "Untitled",
        "uploader": (info.get("uploader") or info.get("channel") or "").strip(),
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "webpage_url": info.get("webpage_url") or info.get("original_url") or url,
        "resolutions": resolutions,
        "audio_only": {"label": "Audio only (MP3)", "format": "bestaudio/best",
                       "audio_only": True},
    }


# ─── download ─────────────────────────────────────────────────────────────────

def download(
    url: str,
    height: int | None = None,
    format_selector: str = "",
    audio_only: bool = False,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """
    Download a video from *url* at the requested resolution.

    height / format_selector / audio_only choose what to fetch:
      • audio_only=True            → extract bestaudio → MP3
      • format_selector given      → used verbatim (e.g. from probe())
      • height given (e.g. 720)    → bestvideo[height<=720]+bestaudio
      • nothing                    → VIDEO_DEFAULT_HEIGHT (0 = best available)

    on_progress(dict) receives {"pct", "downloaded", "total", "speed", "eta"}
    updates; it's best-effort (exceptions swallowed). Returns
    {"ok": True, "path", "filename", "title"} or {"ok": False, "error"}.
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "No link given."}
    target = url if _is_url(url) else f"ytsearch1:{url}"

    try:
        import yt_dlp  # type: ignore[import]
    except ImportError:
        return {"ok": False, "error": "yt-dlp is not installed (pip install yt-dlp)."}
    if not _ffmpeg_available():
        return {"ok": False,
                "error": "ffmpeg is not installed — it's needed to merge video + audio."}

    def _emit(d: dict) -> None:
        if on_progress is None:
            return
        try:
            on_progress(d)
        except Exception:  # pragma: no cover - a callback must never break a download
            pass

    def _hook(d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0.0
            _emit({"status": "downloading", "pct": round(pct, 1),
                   "downloaded": done, "total": total,
                   "speed": d.get("speed") or 0, "eta": d.get("eta") or 0})
        elif status == "finished":
            # download done; ffmpeg post-processing (merge/transcode) is next
            _emit({"status": "processing", "pct": 99.0})

    # Choose the format string.
    if audio_only:
        fmt = format_selector or "bestaudio/best"
    elif format_selector:
        fmt = format_selector
    else:
        h = height if height is not None else VIDEO_DEFAULT_HEIGHT
        fmt = (f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
               if h and int(h) > 0 else "bestvideo+bestaudio/best")

    ydl_opts: dict[str, Any] = {
        "format": fmt,
        "outtmpl": VIDEO_OUTPUT_TEMPLATE,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,   # we report progress via _hook, not yt-dlp's console bar
        "noplaylist": True,
        "retries": YTDLP_RETRIES,
        "progress_hooks": [_hook],
    }
    if audio_only:
        # mirror music.py: extract to MP3 instead of keeping the video container
        ydl_opts["outtmpl"] = str(VIDEO_CACHE_DIR / "%(title)s.%(ext)s")
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = VIDEO_MERGE_FORMAT

    log.info("video download: %s (format=%s)", target, fmt)
    _emit({"status": "starting", "pct": 0.0})
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target, download=True)
    except yt_dlp.utils.DownloadError as exc:
        log.error("video download failed: %s", exc)
        return {"ok": False, "error": "Download failed — that resolution may be unavailable."}
    except Exception as exc:
        log.error("video download error: %s", exc)
        return {"ok": False, "error": f"Download failed ({exc})."}

    if isinstance(info, dict) and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else info
    title = (info.get("title") or "video").strip() if isinstance(info, dict) else "video"

    final = _locate_output(info, audio_only)
    if not final:
        return {"ok": False, "error": "Download finished but the file couldn't be located."}

    _emit({"status": "done", "pct": 100.0})
    log.info("video saved: %s", final)
    return {"ok": True, "path": str(final), "filename": final.name, "title": title}


def _locate_output(info: Any, audio_only: bool) -> Path | None:
    """Find the file yt-dlp actually produced (post-merge / post-transcode)."""
    # 1) trust yt-dlp's reported path, fixing the extension for merged/extracted files.
    candidates: list[Path] = []
    if isinstance(info, dict):
        for key in ("filepath", "_filename"):
            raw = info.get(key)
            if raw:
                candidates.append(Path(raw))
        rds = info.get("requested_downloads")
        if isinstance(rds, list):
            for rd in rds:
                fp = (rd or {}).get("filepath")
                if fp:
                    candidates.append(Path(fp))
    for c in candidates:
        if c.exists():
            return c
        if audio_only:
            mp3 = c.with_suffix(".mp3")
            if mp3.exists():
                return mp3
        else:
            merged = c.with_suffix("." + VIDEO_MERGE_FORMAT)
            if merged.exists():
                return merged

    # 2) fall back to the newest matching file in the cache (within the last 10 min).
    exts = (".mp3",) if audio_only else VIDEO_EXTS
    try:
        files = [p for p in VIDEO_CACHE_DIR.iterdir()
                 if p.is_file() and p.suffix.lower() in exts]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if files and (time.time() - files[0].stat().st_mtime) < 600:
            return files[0]
    except OSError:
        pass
    return None


# ─── list what's already downloaded ───────────────────────────────────────────

def list_downloaded() -> list[dict]:
    """Every saved video (and any extracted audio) in VIDEO_CACHE_DIR, newest first."""
    exts = set(VIDEO_EXTS) | {".mp3"}
    out: list[dict] = []
    try:
        for p in VIDEO_CACHE_DIR.iterdir():
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": p.name,
                "path": str(p),
                "size": st.st_size,
                "size_label": _fmt_size(st.st_size),
                "mtime": st.st_mtime,
                "ext": p.suffix.lower().lstrip("."),
            })
    except OSError as exc:
        log.warning("list_downloaded failed: %s", exc)
    out.sort(key=lambda d: d.get("mtime", 0), reverse=True)
    return out


def delete_download(path: str) -> bool:
    """Delete one downloaded file, but only inside VIDEO_CACHE_DIR (path-safety)."""
    try:
        p = Path(path).resolve()
        if VIDEO_CACHE_DIR.resolve() not in p.parents:
            log.warning("refusing to delete outside video dir: %s", p)
            return False
        if p.exists():
            p.unlink()
        return True
    except OSError as exc:
        log.warning("delete_download failed: %s", exc)
        return False


# ─── standalone smoke test (no network) ───────────────────────────────────────

if __name__ == "__main__":
    import config
    config.setup_logging()
    log.info("video.py standalone test")
    log.info("ffmpeg available: %s", _ffmpeg_available())
    log.info("resolution label 2160/60: %s", _resolution_label(2160, 60))
    log.info("size label: %s", _fmt_size(73_400_000))
    log.info("downloaded so far: %d file(s)", len(list_downloaded()))
    log.info("video.py test done")
