"""
tts_rvc.py — English TTS pipeline: Piper → RVC v2 voice conversion.

Pipeline:
    text → Piper TTS (CPU, onnxruntime) → temp WAV
         → RVC v2 (GPU, assistant.pth, ~1.5 GB VRAM) → converted WAV
         → sounddevice playback → cleanup

If RVC model missing: plays Piper output directly.
If CUDA OOM during RVC: falls back to CPU.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
import wave
from pathlib import Path

import sounddevice as sd
import soundfile as sf

import config
from config import (
    PIPER_VOICE_CONFIG,
    PIPER_VOICE_MODEL,
    RVC_BATCH_SIZE,
    RVC_F0_METHOD,
    RVC_F0_UP_KEY,
    RVC_FILTER_RADIUS,
    RVC_FP16,
    RVC_HUBERT_PATH,
    RVC_INDEX_PATH,
    RVC_INDEX_RATE,
    RVC_MODEL_PATH,
    RVC_PROTECT,
    RVC_RESAMPLE_SR,
    RVC_RMS_MIX_RATE,
    TEMP_DIR,
)

log = logging.getLogger(__name__)


class TTSEngine:
    """
    Synthesises speech via Piper TTS then optionally applies RVC v2
    voice conversion.

    One instance per process lifetime — RVC model stays in GPU memory.
    Call set_language(\"ja\") or set_language(\"en\") to switch Piper voices.
    """

    def __init__(self) -> None:
        self._rvc = None
        self._rvc_available = False
        self._rvc_load_attempted = False
        self._piper_voice = None
        self._piper_ok = self._check_piper()
        # RVC is optional (Settings → Voice). Off by default: no torch import,
        # no VRAM used. Loaded lazily on first speak() if the user enables it.
        if self._piper_ok and config.RVC_ENABLED:
            self._try_load_rvc()
            self._rvc_load_attempted = True

    # ─── Setup ────────────────────────────────────────────────────────────────

    def _check_piper(self) -> bool:
        try:
            from piper import PiperVoice  # type: ignore[import]  # noqa: F401
        except ImportError:
            log.error("piper-tts not installed. Run: pip install piper-tts")
            return False

        if not PIPER_VOICE_MODEL.exists():
            log.warning(
                "Piper EN voice model missing: %s — run python bootstrap.py",
                PIPER_VOICE_MODEL,
            )
            return False

        try:
            from piper import PiperVoice
            self._piper_voice = PiperVoice.load(
                str(PIPER_VOICE_MODEL),
                config_path=str(PIPER_VOICE_CONFIG) if PIPER_VOICE_CONFIG.exists() else None,
                use_cuda=False,
            )
            log.info("Piper EN voice ready ✓ (%s)", config.PIPER_VOICE_NAME)
            return True
        except Exception as exc:
            log.error("Failed to load Piper EN voice: %s", exc)
            return False

    def _try_load_rvc(self) -> None:
        """Attempt to load RVC v2. Non-fatal if model or package missing. Supports custom model path via config/env."""
        model_path = getattr(config, "RVC_MODEL_PATH", RVC_MODEL_PATH)
        index_path = getattr(config, "RVC_INDEX_PATH", RVC_INDEX_PATH)
        if not model_path.exists():
            log.warning(
                "RVC model not found at %s — voice conversion disabled. "
                "Piper output will be played directly.",
                model_path,
            )
            return

        try:
            from rvc_python.infer import RVCInference  # type: ignore[import]
        except ImportError:
            log.warning(
                "rvc-python not installed (pip install rvc-python) — "
                "RVC conversion disabled."
            )
            return

        try:
            log.info("Loading RVC v2 model (fp16=%s, device=%s)…", RVC_FP16, config.RVC_DEVICE)
            import torch

            device = config.RVC_DEVICE if torch.cuda.is_available() else "cpu"
            self._rvc = RVCInference(device=device)
            self._rvc.load_model(
                str(model_path),
                index_path=str(index_path) if index_path.exists() else "",
            )
            self._rvc_available = True
            log.info("RVC v2 loaded on %s ✓", device)
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                log.warning(
                    "CUDA OOM loading RVC — retrying on CPU (slower): %s", exc
                )
                try:
                    self._rvc = RVCInference(device="cpu")
                    self._rvc.load_model(
                        str(model_path),
                        index_path=str(index_path) if index_path.exists() else "",
                    )
                    self._rvc_available = True
                    log.info("RVC v2 loaded on CPU (fallback) ✓")
                except Exception as cpu_exc:
                    log.error("RVC CPU fallback also failed: %s", cpu_exc)
            else:
                log.error("RVC load failed: %s", exc)

    # ─── Kokoro-ONNX synthesis (Japanese) ────────────────────────────────────

    # ─── Piper synthesis (English) ────────────────────────────────────────────

    def _run_piper(self, text: str, output_path: Path) -> None:
        """
        Synthesise text → WAV.

        Fast path  : piper Python API (in-process, zero overhead).
        Fallback   : piper CLI subprocess — the model runs in its own process so
                     any ONNX crash there cannot bring down the agent thread.
                     This handles the known onnxruntime Reshape/zero-dim bug that
                     hits on certain phoneme sequences (short sentences, question
                     marks, specific token combinations).
        """
        import subprocess
        import sys

        # ── Fast path: Python API ─────────────────────────────────────────────
        # Open the wave file manually (not as a context manager) so that a
        # wave.close() error in the finally block cannot mask the real ONNX exc.
        api_exc: Exception | None = None
        wav_file = wave.open(str(output_path), "wb")
        try:
            self._piper_voice.synthesize_wav(text, wav_file)
        except Exception as exc:
            api_exc = exc
        finally:
            try:
                wav_file.close()
            except Exception:
                pass  # ignore wave-close errors — they only mask the real exc

        if api_exc is None and output_path.exists() and output_path.stat().st_size > 44:
            return  # success

        log.warning("Piper Python API failed (%s) — falling back to CLI", api_exc)

        # ── Fallback: piper CLI subprocess ────────────────────────────────────
        piper_cli = Path(sys.executable).parent / "piper.exe"
        if not piper_cli.exists():
            piper_cli = Path(sys.executable).parent / "piper"
        if not piper_cli.exists():
            raise RuntimeError(
                f"Piper Python API failed and piper CLI not found. "
                f"Original error: {api_exc}"
            )

        try:
            proc = subprocess.run(
                [
                    str(piper_cli),
                    "--model",       str(PIPER_VOICE_MODEL),
                    "--output_file", str(output_path),
                    "--quiet",
                ],
                input=text,
                text=True,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("piper CLI timed out after 30s")

        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 44:
            return  # CLI succeeded

        raise RuntimeError(
            f"piper CLI exited {proc.returncode}: {proc.stderr[:300] or '(no stderr)'}"
        )

    # ─── RVC conversion ───────────────────────────────────────────────────────

    def _run_rvc(self, input_path: Path, output_path: Path) -> None:
        """Apply RVC v2 voice conversion to input WAV → output WAV."""
        try:
            # rvc-python uses instance attributes for inference settings
            self._rvc.f0method = RVC_F0_METHOD
            self._rvc.f0up_key = RVC_F0_UP_KEY
            self._rvc.index_rate = RVC_INDEX_RATE
            self._rvc.filter_radius = RVC_FILTER_RADIUS
            self._rvc.resample_sr = RVC_RESAMPLE_SR
            self._rvc.rms_mix_rate = RVC_RMS_MIX_RATE
            self._rvc.protect = RVC_PROTECT
            self._rvc.infer_file(
                input_path=str(input_path),
                output_path=str(output_path),
            )
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                log.warning("CUDA OOM during RVC inference — retrying on CPU")
                import torch
                torch.cuda.empty_cache()
                # Move RVC to CPU and retry
                try:
                    from rvc_python.infer import RVCInference
                    self._rvc = RVCInference(device="cpu")
                    self._rvc.load_model(
                        str(RVC_MODEL_PATH),
                        index_path=str(RVC_INDEX_PATH) if RVC_INDEX_PATH.exists() else "",
                    )
                    self._rvc.f0method = RVC_F0_METHOD
                    self._rvc.f0up_key = RVC_F0_UP_KEY
                    self._rvc.index_rate = RVC_INDEX_RATE
                    self._rvc.filter_radius = RVC_FILTER_RADIUS
                    self._rvc.resample_sr = RVC_RESAMPLE_SR
                    self._rvc.rms_mix_rate = RVC_RMS_MIX_RATE
                    self._rvc.protect = RVC_PROTECT
                    self._rvc.infer_file(
                        input_path=str(input_path),
                        output_path=str(output_path),
                    )
                except Exception as cpu_exc:
                    log.error("RVC CPU retry failed — playing Piper audio directly: %s", cpu_exc)
                    raise RuntimeError("RVC unavailable") from cpu_exc
            else:
                raise

    # ─── Playback ─────────────────────────────────────────────────────────────

    def _play_wav(self, wav_path: Path) -> None:
        """Play a WAV file through the default audio output device."""
        data, sample_rate = sf.read(str(wav_path), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)  # stereo → mono
        log.debug("Playing %s (sr=%d, samples=%d)", wav_path.name, sample_rate, len(data))
        # Route to the user-chosen speaker/headset (Settings → Audio); None = default.
        device = None
        try:
            import config
            import audio_devices
            if config.AUDIO_OUTPUT_DEVICE:
                device = audio_devices.resolve_output_index(config.AUDIO_OUTPUT_DEVICE)
        except Exception:
            device = None
        sd.play(data, samplerate=sample_rate, device=device)
        sd.wait()

    # ─── Public interface ─────────────────────────────────────────────────────

    def _speak_sync(self, text: str) -> None:
        """Synchronous implementation. Called from async wrapper via executor."""
        if not self._piper_ok:
            log.error("Piper TTS not available — run: pip install piper-tts")
            return

        # Live settings checks so GUI toggles apply without a restart
        import settings as user_settings
        if not user_settings.get("voice.tts_enabled", True):
            return
        rvc_wanted = bool(user_settings.get("voice.rvc_enabled", False))
        if rvc_wanted and not self._rvc_load_attempted:
            self._try_load_rvc()
            self._rvc_load_attempted = True

        # Strip ACTION lines — never speak them
        import re as _re
        text = _re.sub(r'ACTION:\s*\S+.*', '', text, flags=_re.IGNORECASE).strip()
        # Piper ONNX crashes on empty / punct-only input (Reshape zero-dim bug)
        if not _re.search(r'[a-zA-Z0-9]', text):
            return

        uid = uuid.uuid4().hex[:8]
        piper_out = TEMP_DIR / f"piper_{uid}.wav"
        rvc_out   = TEMP_DIR / f"rvc_{uid}.wav"

        try:
            # 1. Synthesise with Piper
            self._run_piper(text, piper_out)

            # 2. Voice-convert with RVC only when enabled AND loaded
            if rvc_wanted and self._rvc_available:
                try:
                    self._run_rvc(piper_out, rvc_out)
                    playback_path = rvc_out
                except RuntimeError:
                    log.warning("RVC failed — playing Piper output directly")
                    playback_path = piper_out
            else:
                playback_path = piper_out

            # 3. Play
            self._play_wav(playback_path)

        finally:
            # 4. Cleanup temp files regardless of errors
            for p in (piper_out, rvc_out):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    async def speak(self, text: str) -> None:
        """
        Synthesise and play text as speech.

        Non-blocking — runs synthesis and playback in a thread pool so the
        event loop can continue processing.
        """
        if not text.strip():
            return
        log.debug("TTS speak: %r", text[:80])
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._speak_sync, text)


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    config.setup_logging()
    log.info("TTS+RVC pipeline standalone test")

    engine = TTSEngine()

    test_phrases = [
        "Hello. I am your AI companion.",
        "All processing happens on this machine — no cloud, no latency.",
    ]

    async def _test() -> None:
        for phrase in test_phrases:
            log.info("Speaking: %r", phrase)
            await engine.speak(phrase)

    try:
        asyncio.run(_test())
        log.info("TTS test complete ✓")
    except KeyboardInterrupt:
        log.info("Interrupted")
        sys.exit(0)
