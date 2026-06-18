"""
media_tools.py — local audio/video utilities via ffmpeg (the same binary the
music/video downloader already uses). Replaces online compressors / converters
/ GIF makers — all offline.

Every function returns {"ok": bool, "path"/"error": ...} and writes to
config.TOOLS_DIR. Slow work is meant to run on a background thread.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import config

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _out(src: Path, suffix: str, ext: str) -> Path:
    config.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.TOOLS_DIR / f"{src.stem}{suffix}.{ext.lstrip('.')}"
    i = 1
    while dest.exists():
        dest = config.TOOLS_DIR / f"{src.stem}{suffix}_{i}.{ext.lstrip('.')}"
        i += 1
    return dest


def _run(cmd: list[str]) -> tuple[bool, str]:
    log.info("ffmpeg: %s", " ".join(str(c) for c in cmd))
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       creationflags=_NO_WINDOW)
    if p.returncode != 0:
        return False, (p.stderr or "ffmpeg failed")[-400:]
    return True, ""


def _done(ok: bool, dest: Path, err: str) -> dict:
    if ok and dest.exists():
        return {"ok": True, "path": str(dest)}
    return {"ok": False, "error": err or "Conversion failed."}


def compress_video(src_path: str, crf: int = 28) -> dict:
    """Re-encode to smaller H.264/AAC MP4 (higher crf = smaller, 23-30 typical)."""
    src = Path(src_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}
    dest = _out(src, "_compressed", "mp4")
    ok, err = _run([config.FFMPEG_EXECUTABLE, "-y", "-i", src,
                    "-vcodec", "libx264", "-crf", crf, "-preset", "medium",
                    "-acodec", "aac", "-b:a", "128k", dest])
    return _done(ok, dest, err)


def convert(src_path: str, target_ext: str) -> dict:
    """Convert audio/video to another container/codec (mp4, mkv, webm, mp3, wav, m4a)."""
    src = Path(src_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}
    ext = target_ext.lstrip(".").lower()
    dest = _out(src, "", ext)
    cmd = [config.FFMPEG_EXECUTABLE, "-y", "-i", src]
    if ext in ("mp3", "wav", "m4a", "flac", "ogg", "aac"):
        cmd += ["-vn"]  # audio-only target → drop video
    ok, err = _run(cmd + [dest])
    return _done(ok, dest, err)


def trim(src_path: str, start: str = "0", end: str | None = None) -> dict:
    """Cut a clip between start/end (seconds or HH:MM:SS), copying streams (fast)."""
    src = Path(src_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}
    dest = _out(src, "_clip", src.suffix.lstrip(".") or "mp4")
    cmd = [config.FFMPEG_EXECUTABLE, "-y", "-ss", str(start), "-i", src]
    if end:
        cmd += ["-to", str(end)]
    ok, err = _run(cmd + ["-c", "copy", dest])
    if not ok:  # stream-copy can fail on odd cut points → re-encode fallback
        ok, err = _run([config.FFMPEG_EXECUTABLE, "-y", "-ss", str(start), "-i", src]
                       + (["-to", str(end)] if end else []) + [dest])
    return _done(ok, dest, err)


def to_gif(src_path: str, start: str = "0", duration: float = 5.0,
           fps: int = 12, width: int = 480) -> dict:
    """Make a GIF from a slice of a video."""
    src = Path(src_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}
    dest = _out(src, "", "gif")
    vf = f"fps={fps},scale={width}:-1:flags=lanczos"
    ok, err = _run([config.FFMPEG_EXECUTABLE, "-y", "-ss", str(start), "-t", str(duration),
                    "-i", src, "-vf", vf, "-loop", "0", dest])
    return _done(ok, dest, err)


def extract_audio(src_path: str, fmt: str = "mp3") -> dict:
    """Pull the audio track out of a video as mp3/wav/m4a."""
    return convert(src_path, fmt)
