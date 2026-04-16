"""
tts_rvc.py — Text-to-speech pipeline: Piper TTS → RVC v2 voice conversion.

Pipeline:
    text (str)
        → Piper TTS (CPU, Python API, piper-tts package)
        → temp WAV in data/temp/  (22 kHz, mono)
        → RVC v2 inference (GPU, rvc-python)
        → converted WAV in data/temp/
        → sounddevice playback
        → temp files deleted

If no RVC model is present, Piper output is played directly (no conversion).
If CUDA OOM occurs during RVC, falls back to CPU inference.
If piper-tts package is missing, raises ImportError with install instructions.

VRAM impact:
    Piper:  0 GB  (CPU / onnxruntime)
    RVC v2: ~1.5 GB  (fp16, batch_size=1, rmvpe)
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
    KOKORO_MODEL_PATH,
    KOKORO_VOICES_PATH,
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
        self._kokoro = None            # kokoro-onnx JP TTS model
        self._rvc_available = False
        self._piper_voice = None       # English Piper voice (PiperVoice instance)
        self._openjtalk_ok = False     # whether kokoro JP TTS loaded successfully
        self._language: str = config.TTS_LANGUAGE  # "ja" or "en"
        self._piper_ok = self._check_piper()
        self._openjtalk_ok = self._check_openjtalk()
        if self._piper_ok or self._openjtalk_ok:
            self._try_load_rvc()

    def set_language(self, lang: str) -> None:
        """Switch TTS language: 'ja' uses kokoro-onnx, 'en' uses Piper."""
        if lang not in ("ja", "en"):
            log.warning("Unknown language '%s' — keeping '%s'", lang, self._language)
            return
        self._language = lang
        log.info("TTS language set to '%s'", lang)

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

    def _check_openjtalk(self) -> bool:
        """Load kokoro-onnx model for Japanese TTS (jm_kumo male voice)."""
        if not KOKORO_MODEL_PATH.exists() or not KOKORO_VOICES_PATH.exists():
            log.warning(
                "Kokoro JP model files missing — run python bootstrap.py\n"
                "  Expected: %s\n  Expected: %s",
                KOKORO_MODEL_PATH, KOKORO_VOICES_PATH,
            )
            return False
        try:
            from kokoro_onnx import Kokoro  # type: ignore[import]
            self._kokoro = Kokoro(str(KOKORO_MODEL_PATH), str(KOKORO_VOICES_PATH))
            log.info("Kokoro JP TTS ready ✓ (voice=%s)", config.KOKORO_VOICE)
            return True
        except Exception as exc:
            log.warning("Failed to load Kokoro JP TTS: %s", exc)
            return False

    def _try_load_rvc(self) -> None:
        """Attempt to load RVC v2. Non-fatal if model or package missing."""
        if not RVC_MODEL_PATH.exists():
            log.warning(
                "RVC model not found at %s — voice conversion disabled. "
                "Piper output will be played directly.",
                RVC_MODEL_PATH,
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
            log.info("Loading RVC v2 model (fp16=%s, device=cuda:0)…", RVC_FP16)
            import torch

            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            self._rvc = RVCInference(device=device)
            self._rvc.load_model(
                str(RVC_MODEL_PATH),
                index_path=str(RVC_INDEX_PATH) if RVC_INDEX_PATH.exists() else "",
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
                        str(RVC_MODEL_PATH),
                        index_path=str(RVC_INDEX_PATH) if RVC_INDEX_PATH.exists() else "",
                    )
                    self._rvc_available = True
                    log.info("RVC v2 loaded on CPU (fallback) ✓")
                except Exception as cpu_exc:
                    log.error("RVC CPU fallback also failed: %s", cpu_exc)
            else:
                log.error("RVC load failed: %s", exc)

    # ─── Kokoro-ONNX synthesis (Japanese) ────────────────────────────────────

    def _run_openjtalk(self, text: str, output_path: Path) -> None:
        """
        Synthesise Japanese text via kokoro-onnx.
        Pipeline:
          1. pykakasi: kanji -> hiragana  (prevents espeak reading kanji as Chinese)
          2. espeak "ja": hiragana -> IPA phonemes
          3. kokoro-onnx: IPA -> WAV
        """
        try:
            import pykakasi  # type: ignore[import]
            import espeakng_loader  # type: ignore[import]
            import phonemizer  # type: ignore[import]
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
            from kokoro_onnx.config import DEFAULT_VOCAB
            import warnings

            # Step 1: kanji -> hiragana so espeak never sees kanji (avoids Chinese phonemes)
            kks = pykakasi.kakasi()
            hiragana = "".join(
                item["hira"] if item["hira"] else item["orig"]
                for item in kks.convert(text)
            )
            log.debug("JP kana: %r", hiragana)

            # Step 2: hiragana -> IPA via espeak
            EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
            EspeakWrapper.set_library(espeakng_loader.get_library_path())
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                phonemes_raw = phonemizer.phonemize(
                    hiragana, language="ja", backend="espeak", with_stress=True
                )
            phonemes = "".join(p for p in phonemes_raw if p in DEFAULT_VOCAB)
            log.debug("JP phonemes: %r", phonemes[:60])

            # Step 3: IPA -> audio via kokoro-onnx
            samples, sample_rate = self._kokoro.create(
                phonemes,
                voice=config.KOKORO_VOICE,
                speed=config.JP_TTS_SPEED,
                is_phonemes=True,
            )
            sf.write(str(output_path), samples, sample_rate)
        except Exception as exc:
            raise RuntimeError(f"Kokoro JP synthesis failed: {exc}") from exc

        # ─── Piper synthesis (English) ────────────────────────────────────────────

    def _run_piper(self, text: str, output_path: Path) -> None:
        """
        Synthesise text to a WAV file using the piper-tts Python API.
        Writes a valid WAV file to output_path.
        """
        try:
            with wave.open(str(output_path), "wb") as wav_file:
                self._piper_voice.synthesize_wav(text, wav_file)
        except Exception as exc:
            raise RuntimeError(f"Piper synthesis failed: {exc}") from exc

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
        sd.play(data, samplerate=sample_rate)
        sd.wait()

    # ─── Public interface ─────────────────────────────────────────────────────

    def _speak_sync(self, text: str) -> None:
        """Synchronous implementation. Called from async wrapper via executor."""
        # Select TTS backend: Japanese → kokoro-onnx, English → Piper
        use_jp = (self._language == "ja" and self._openjtalk_ok)
        use_en = (self._language == "en" and self._piper_ok)
        if not use_jp and not use_en:
            # Fallback: try any available backend
            if self._openjtalk_ok:
                use_jp = True
            elif self._piper_ok:
                use_en = True
            else:
                log.error("TTS not available — download Kokoro models (JP) or install piper-tts (EN)")
                return

        uid = uuid.uuid4().hex[:8]
        piper_out = TEMP_DIR / f"piper_{uid}.wav"
        rvc_out   = TEMP_DIR / f"rvc_{uid}.wav"

        try:
            # 1. Synthesise with selected backend
            if use_jp:
                self._run_openjtalk(text, piper_out)
            else:
                self._run_piper(text, piper_out)

            # 2. Voice-convert with RVC if available
            if self._rvc_available:
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

    if config.TTS_LANGUAGE == "ja":
        # Iconic Gojo Satoru lines
        test_phrases = [
            "やばいな、信が最強なんだよね。",          # Yabai na, ore ga saikyou nanda yo ne.
            "はい、ふつうに最強。",                     # Hai, futsuu ni saikyou.
            "この世界はこんなに面白いのか。",           # Kono sekai wa konnani omoshiroi no ka.
            "術式反転、無限。",                         # Jutsushiki hanten, mugen.
            "天上天下、唯我独尊。",                     # Ten jou ten ge, yui ga doku son.
        ]
    else:
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
