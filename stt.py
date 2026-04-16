"""
stt.py — Speech-to-text using faster-whisper + sounddevice.

Pipeline:
    Microphone (sounddevice)
        → float32 audio blocks at 16 kHz
        → accumulated until silence detected
        → faster-whisper transcription (GPU, falls back to CPU on OOM)
        → text string returned

Key design decisions:
- sounddevice instead of pyaudio: bundles PortAudio DLLs on Windows
- Silence detection via RMS threshold: avoids sending empty audio to Whisper
- Async generator interface: integrates cleanly with the asyncio agent loop
- Graceful CUDA OOM recovery: re-initialises model in int8 CPU mode
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import AsyncGenerator

import numpy as np
import sounddevice as sd

import config
from config import (
    MIC_BLOCK_DURATION_S,
    MIC_CHANNELS,
    MIC_RMS_THRESHOLD,
    MIC_SAMPLE_RATE,
    MIC_SILENCE_TIMEOUT_S,
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL_SIZE,
    WHISPER_VAD_FILTER,
)

log = logging.getLogger(__name__)


class WhisperTranscriber:
    """
    Wraps faster-whisper with a sounddevice microphone capture loop.

    Typical usage (async):
        transcriber = WhisperTranscriber()
        text = await transcriber.listen_once()

    The instance is reusable — keep it alive for the agent loop lifetime
    to avoid re-loading the model on each turn.
    """

    def __init__(self) -> None:
        self._model = None
        self._device = WHISPER_DEVICE
        self._compute_type = WHISPER_COMPUTE_TYPE
        self._load_model()

    # ─── Model management ─────────────────────────────────────────────────────

    def _load_model(self) -> None:
        """Load faster-whisper. Falls back to CPU int8 on CUDA OOM."""
        from faster_whisper import WhisperModel

        try:
            log.info(
                "Loading Whisper(%s) on %s / %s …",
                WHISPER_MODEL_SIZE, self._device, self._compute_type,
            )
            self._model = WhisperModel(
                WHISPER_MODEL_SIZE,
                device=self._device,
                compute_type=self._compute_type,
                download_root=str(config.MODELS_DIR / "whisper"),
            )
            log.info("Whisper model loaded ✓")
        except Exception as exc:
            if "CUDA" in str(exc) or "out of memory" in str(exc).lower():
                log.warning(
                    "CUDA OOM loading Whisper — falling back to CPU int8: %s", exc
                )
                self._device = "cpu"
                self._compute_type = "int8"
                self._model = WhisperModel(
                    WHISPER_MODEL_SIZE,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(config.MODELS_DIR / "whisper"),
                )
                log.info("Whisper (CPU int8 fallback) loaded ✓")
            else:
                log.error("Failed to load Whisper: %s", exc)
                raise

    # ─── Audio capture ────────────────────────────────────────────────────────

    def _capture_until_silence(self) -> np.ndarray | None:
        """
        Capture microphone audio until MIC_SILENCE_TIMEOUT_S of silence.

        Runs synchronously in a thread. Returns concatenated float32 audio.
        Returns None if no speech detected at all.
        """
        block_samples = int(MIC_SAMPLE_RATE * MIC_BLOCK_DURATION_S)
        audio_q: queue.Queue[np.ndarray] = queue.Queue()
        stop_event = threading.Event()

        def _callback(
            indata: np.ndarray,
            frames: int,
            time_info: object,
            status: sd.CallbackFlags,
        ) -> None:
            if status:
                log.debug("sounddevice status: %s", status)
            audio_q.put(indata.copy().flatten())

        accumulated: list[np.ndarray] = []
        last_speech_time = time.monotonic()
        speech_started = False

        with sd.InputStream(
            samplerate=MIC_SAMPLE_RATE,
            channels=MIC_CHANNELS,
            dtype="float32",
            blocksize=block_samples,
            callback=_callback,
        ):
            log.debug("Mic open — listening…")
            while not stop_event.is_set():
                try:
                    block = audio_q.get(timeout=MIC_BLOCK_DURATION_S * 2)
                except queue.Empty:
                    continue

                rms = float(np.sqrt(np.mean(block ** 2)))

                if rms >= MIC_RMS_THRESHOLD:
                    accumulated.append(block)
                    last_speech_time = time.monotonic()
                    speech_started = True
                elif speech_started:
                    accumulated.append(block)  # include trailing quiet block
                    silence_duration = time.monotonic() - last_speech_time
                    if silence_duration >= MIC_SILENCE_TIMEOUT_S:
                        log.debug(
                            "Silence timeout (%.1fs) — stopping capture",
                            silence_duration,
                        )
                        stop_event.set()

        if not accumulated:
            log.debug("No speech detected")
            return None

        return np.concatenate(accumulated, axis=0)

    # ─── Transcription ────────────────────────────────────────────────────────

    def _transcribe(self, audio: np.ndarray) -> str:
        """Run faster-whisper transcription on captured audio array."""
        try:
            import torch
            if self._device == "cuda" and torch.cuda.is_available():
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
        except ImportError:
            pass

        try:
            segments, info = self._model.transcribe(
                audio,
                language=WHISPER_LANGUAGE,
                vad_filter=WHISPER_VAD_FILTER,
                beam_size=5,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            log.debug(
                "Transcribed (lang=%s, prob=%.2f): %s",
                info.language, info.language_probability, text,
            )
            return text
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                log.warning("CUDA OOM during transcription — reloading on CPU")
                self._device = "cpu"
                self._compute_type = "int8"
                self._load_model()
                return self._transcribe(audio)
            log.error("Transcription error: %s", exc)
            return ""

    # ─── Public async interface ───────────────────────────────────────────────

    async def listen_once(self) -> str:
        """
        Capture one utterance from the microphone and return its transcription.

        Runs mic capture in a thread pool to avoid blocking the event loop.
        Returns empty string if no speech detected.
        """
        loop = asyncio.get_running_loop()
        audio = await loop.run_in_executor(None, self._capture_until_silence)
        if audio is None or len(audio) == 0:
            return ""
        text = await loop.run_in_executor(None, self._transcribe, audio)
        return text

    async def stream_utterances(self) -> AsyncGenerator[str, None]:
        """
        Async generator that yields transcribed utterances indefinitely.

        Break the loop from the caller to stop listening.
        """
        while True:
            text = await self.listen_once()
            if text:
                yield text


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    config.setup_logging()
    log.info("STT standalone test — speak after the prompt, then pause")

    transcriber = WhisperTranscriber()
    log.info("Ready. Speak now…")

    async def _test() -> None:
        for i in range(3):
            log.info("--- Utterance %d / 3 --- (speak now)", i + 1)
            text = await transcriber.listen_once()
            if text:
                log.info("Transcription: %r", text)
            else:
                log.info("(nothing detected)")

    try:
        asyncio.run(_test())
    except KeyboardInterrupt:
        log.info("Test interrupted")
        sys.exit(0)
