"""
subtitle_gen.py — generate an .srt subtitle file from any audio/video, locally,
using the Whisper model the app already loads (no paid transcription service).

Pairs with the in-app subtitle adder: the produced .srt sits next to the video,
so the player auto-detects it on next play.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import config

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _fmt_ts(seconds: float) -> str:
    """Seconds -> SRT timestamp HH:MM:SS,mmm."""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _extract_audio(src: Path) -> Path:
    """Decode to mono 16 kHz wav (what Whisper wants) via ffmpeg."""
    out = Path(tempfile.gettempdir()) / (src.stem + ".whisper16k.wav")
    cmd = [config.FFMPEG_EXECUTABLE, "-y", "-i", str(src),
           "-vn", "-ac", "1", "-ar", "16000", str(out)]
    subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    return out


def _get_model():
    """Reuse the GUI's preloaded Whisper model (no extra VRAM); else load one."""
    try:
        import stt
        m = getattr(stt, "_preloaded_model", None)
        if m is not None:
            return m
    except Exception:
        pass
    from faster_whisper import WhisperModel
    return WhisperModel(
        config.WHISPER_MODEL_SIZE, device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
        download_root=str(config.MODELS_DIR / "whisper"),
    )


def generate_srt(media_path: str, language: str | None = None, on_event=None) -> dict:
    """Transcribe *media_path* and write an .srt next to it. Returns {ok, path}."""
    src = Path(media_path)
    if not src.exists():
        return {"ok": False, "error": "File not found."}

    def emit(msg: str) -> None:
        if on_event:
            try:
                on_event(msg)
            except Exception:
                pass

    wav = None
    try:
        emit("Extracting audio…")
        wav = _extract_audio(src)
        if not wav.exists() or wav.stat().st_size == 0:
            return {"ok": False, "error": "Could not read audio (is ffmpeg installed?)."}

        emit("Transcribing with Whisper…")
        model = _get_model()
        segments, _info = model.transcribe(
            str(wav), language=language, vad_filter=config.WHISPER_VAD_FILTER)

        lines: list[str] = []
        n = 0
        for seg in segments:                       # iterating drives the transcription
            text = (seg.text or "").strip()
            if not text:
                continue
            n += 1
            lines += [str(n), f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}", text, ""]
            if n % 25 == 0:
                emit(f"Transcribing… {n} lines")

        if not lines:
            return {"ok": False, "error": "No speech detected."}

        srt = src.with_suffix(".srt")
        srt.write_text("\n".join(lines), encoding="utf-8")
        emit("Done")
        return {"ok": True, "path": str(srt), "cues": n}
    except Exception as exc:
        log.error("subtitle generation failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    finally:
        if wav is not None:
            try:
                wav.unlink(missing_ok=True)
            except Exception:
                pass
