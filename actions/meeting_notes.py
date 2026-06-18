"""
actions/meeting_notes.py — Record a meeting, transcribe it, produce minutes.

meeting_start: records the mic to a WAV (16 kHz mono) in DATA_DIR/meetings/,
streaming to disk in chunks via a background writer thread so long meetings
never blow up RAM.

meeting_stop: stops recording, transcribes with faster-whisper (reusing the
STT module's pre-loaded model when available), then one LLM call turns the
transcript into minutes (summary / decisions / action items). Transcript +
minutes are saved to DATA_DIR/meetings/<timestamp>.md.

Usage (via LLM actions):
    meeting_start |
    meeting_stop |
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)

MEETINGS_DIR: Path = config.DATA_DIR / "meetings"

_SAMPLE_RATE = 16_000
_CHANNELS = 1

_MINUTES_SYSTEM = (
    "You are a professional minute-taker. From the meeting transcript, write:\n"
    "## Summary\n2-4 sentences.\n"
    "## Decisions\nBullet list (or 'None recorded').\n"
    "## Action items\nBullet list with owner if identifiable (or 'None').\n"
    "Stick strictly to what was actually said."
)

_MAX_TRANSCRIPT_CHARS = 12_000  # cap fed to the LLM


class _Recorder:
    """Module-level recording state: stream + queue + writer thread + file."""

    def __init__(self, wav_path: Path, stream, sound_file) -> None:
        self.wav_path = wav_path
        self.stream = stream
        self.file = sound_file
        self.q: queue.Queue = queue.Queue()
        self.running = True
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._writer, daemon=True,
                                       name="meeting-writer")
        self.thread.start()

    def callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("meeting recorder status: %s", status)
        self.q.put(indata.copy())

    def _writer(self) -> None:
        """Drain the queue to disk until stopped (and queue empty)."""
        while self.running or not self.q.empty():
            try:
                block = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.file.write(block)
            except Exception as exc:
                log.error("meeting writer error: %s", exc)
                break

    def stop(self) -> float:
        """Stop everything, flush to disk. Returns recording duration (s)."""
        duration = time.time() - self.started_at
        try:
            self.stream.stop()
            self.stream.close()
        except Exception as exc:
            log.warning("meeting stream close: %s", exc)
        self.running = False
        self.thread.join(timeout=10)
        try:
            self.file.close()
        except Exception as exc:
            log.warning("meeting file close: %s", exc)
        return duration


_recorder: Optional[_Recorder] = None
_state_lock = threading.Lock()


# ─── Recording ───────────────────────────────────────────────────────────────

def _start_recording() -> _Recorder:
    """Blocking — open the soundfile + input stream (runs in executor)."""
    import sounddevice as sd
    import soundfile as sf

    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    wav_path = MEETINGS_DIR / f"meeting_{stamp}.wav"

    sound_file = sf.SoundFile(str(wav_path), mode="w", samplerate=_SAMPLE_RATE,
                              channels=_CHANNELS, subtype="PCM_16")
    rec = _Recorder(wav_path, stream=None, sound_file=sound_file)
    stream = sd.InputStream(samplerate=_SAMPLE_RATE, channels=_CHANNELS,
                            dtype="float32", callback=rec.callback)
    rec.stream = stream
    stream.start()
    return rec


async def meeting_start(param: str = "") -> str:
    """Start recording the microphone for meeting notes."""
    global _recorder
    try:
        with _state_lock:
            if _recorder is not None:
                mins = (time.time() - _recorder.started_at) / 60
                return (f"Already recording (running {mins:.0f} min). "
                        "Say 'meeting stop' when you're done.")
        try:
            loop = asyncio.get_running_loop()
            rec = await loop.run_in_executor(None, _start_recording)
        except ImportError:
            return ("pip install sounddevice soundfile to enable meeting "
                    "recording.")
        with _state_lock:
            _recorder = rec
        log.info("Meeting recording started → %s", rec.wav_path)
        return ("Recording started. I'm capturing the mic — say "
                "'meeting stop' when the meeting ends and I'll write up "
                "the minutes.")
    except Exception as exc:
        log.error("meeting_start failed: %s", exc, exc_info=True)
        return f"Couldn't start recording: {exc}"


# ─── Transcription + minutes ─────────────────────────────────────────────────

def _get_whisper_model():
    """Reuse stt's pre-loaded model when present, else load our own."""
    try:
        import stt
        if getattr(stt, "_preloaded_model", None) is not None:
            log.info("meeting_notes: reusing pre-loaded Whisper model")
            return stt._preloaded_model
    except Exception:
        pass

    from faster_whisper import WhisperModel
    try:
        return WhisperModel("small", device=config.WHISPER_DEVICE,
                            compute_type=config.WHISPER_COMPUTE_TYPE)
    except Exception as exc:
        log.warning("Whisper on %s failed (%s) — falling back to CPU int8",
                    config.WHISPER_DEVICE, exc)
        return WhisperModel("small", device="cpu", compute_type="int8")


def _transcribe(wav_path: Path) -> str:
    model = _get_whisper_model()
    segments, _info = model.transcribe(str(wav_path), vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


async def meeting_stop(param: str = "") -> str:
    """Stop recording, transcribe, and return LLM-written minutes."""
    global _recorder
    try:
        with _state_lock:
            rec = _recorder
            _recorder = None
        if rec is None:
            return ("No meeting is being recorded. Start one with "
                    "'meeting start'.")

        loop = asyncio.get_running_loop()
        duration = await loop.run_in_executor(None, rec.stop)
        if duration < 2:
            return "That recording was under 2 seconds — nothing to transcribe."

        try:
            transcript = await loop.run_in_executor(
                None, _transcribe, rec.wav_path)
        except ImportError:
            return ("Recording saved to "
                    f"{rec.wav_path}, but pip install faster-whisper to "
                    "enable transcription.")
        if not transcript:
            return (f"Recording saved ({duration / 60:.1f} min) but I couldn't "
                    f"hear any speech in it. Audio kept at {rec.wav_path}")

        clipped = transcript[:_MAX_TRANSCRIPT_CHARS]
        note = ("\n\n[Transcript truncated for minutes generation.]"
                if len(transcript) > _MAX_TRANSCRIPT_CHARS else "")
        try:
            from main import get_llm_response  # lazy — avoids circular import
            minutes = await get_llm_response(
                f"Meeting transcript ({duration / 60:.0f} min):\n\n"
                f"{clipped}{note}",
                _MINUTES_SYSTEM,
            )
        except Exception as exc:
            log.error("Minutes generation failed: %s", exc)
            minutes = ""

        # Save transcript + minutes next to the audio
        md_path = rec.wav_path.with_suffix(".md")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            md_path.write_text(
                f"# Meeting notes — {stamp}\n\n"
                f"Duration: {duration / 60:.1f} min\n\n"
                f"{minutes or '(minutes unavailable)'}\n\n"
                f"---\n\n## Full transcript\n\n{transcript}\n",
                encoding="utf-8",
            )
        except Exception as exc:
            log.error("Couldn't save meeting notes: %s", exc)

        if minutes:
            return f"{minutes}\n\n(Saved to {md_path.name} in data/meetings/)"
        return (f"Transcribed {duration / 60:.1f} min but the minutes step "
                f"failed — full transcript saved to {md_path}")
    except Exception as exc:
        log.error("meeting_stop failed: %s", exc, exc_info=True)
        return f"Couldn't finish the meeting notes: {exc}"
