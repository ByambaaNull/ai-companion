"""
config.py — Centralised configuration for AI Companion.

All file paths are pathlib.Path objects. Edit here to tune behaviour.
No hardcoded strings exist anywhere else in the project.
"""

from __future__ import annotations

import logging
from pathlib import Path

# ─── Project root ─────────────────────────────────────────────────────────────
ROOT: Path = Path(__file__).parent.resolve()

# ─── Directory layout ─────────────────────────────────────────────────────────
MODELS_DIR:      Path = ROOT / "models"
RVC_MODEL_DIR:   Path = MODELS_DIR / "rvc"
PIPER_MODEL_DIR: Path = MODELS_DIR / "piper"
KOKORO_MODEL_DIR: Path = MODELS_DIR / "kokoro"      # kokoro-onnx JP TTS models
RVC_PREREQS_DIR: Path = MODELS_DIR / "rvc_prereqs"
RVC_ENGINE_DIR:  Path = MODELS_DIR / "rvc_engine"   # cloned RVC-Project repo

DATA_DIR:        Path = ROOT / "data"
MEMORY_DB_DIR:   Path = DATA_DIR / "memory_db"
MUSIC_CACHE_DIR: Path = DATA_DIR / "music_cache"
TEMP_DIR:        Path = DATA_DIR / "temp"
FAVOURITES_FILE: Path = DATA_DIR / "favourites.json"

# Create all directories on import — idempotent, safe
for _d in (
    MEMORY_DB_DIR, MUSIC_CACHE_DIR, TEMP_DIR,
    RVC_MODEL_DIR, PIPER_MODEL_DIR, RVC_PREREQS_DIR,
    RVC_ENGINE_DIR, KOKORO_MODEL_DIR,
):
    _d.mkdir(parents=True, exist_ok=True)

# ─── Companion identity ───────────────────────────────────────────────────────
COMPANION_NAME: str = "Nova"
USER_ID: str = "user"               # mem0 user identifier

# ─── Hotkey activation ───────────────────────────────────────────────────────
HOTKEY_ACTIVATE: str = "ctrl+shift+g"   # press to wake Gojo (one conversation turn)
HOTKEY_QUIT:     str = "ctrl+shift+q"   # press to shut the agent down cleanly
# kept for compatibility
WAKE_WORD_MODEL_NAME: str = "hotkey"
WAKE_WORD_CUSTOM_MODEL_PATH: Path | None = None
WAKE_WORD_THRESHOLD: float = 0.5
WAKE_WORD_CHUNK_SAMPLES: int = 1280

# ─── LLM — Ollama ─────────────────────────────────────────────────────────────
OLLAMA_BASE_URL: str = "http://localhost:11434"
OLLAMA_MODEL: str = "phi3:mini"
LLM_TEMPERATURE: float = 0.7
LLM_CONTEXT_WINDOW: int = 4096
LLM_STREAM: bool = True

# ─── STT — faster-whisper ────────────────────────────────────────────────────
WHISPER_MODEL_SIZE: str = "small"        # ~0.3 GB VRAM; "base" if memory tight
WHISPER_DEVICE: str = "cuda"             # falls back to "cpu" on OOM
WHISPER_COMPUTE_TYPE: str = "float16"    # "int8_float16" or "int8" on OOM
WHISPER_LANGUAGE: str = "en"
WHISPER_VAD_FILTER: bool = True

MIC_SAMPLE_RATE: int = 16_000
MIC_CHANNELS: int = 1
MIC_BLOCK_DURATION_S: float = 0.5       # seconds per sounddevice block
MIC_SILENCE_TIMEOUT_S: float = 2.0      # silence duration → end utterance
MIC_RMS_THRESHOLD: float = 0.01         # RMS amplitude — below = silence

# ─── TTS — Piper (local, CPU-only, 0 VRAM) ───────────────────────────────────
# Install: pip install piper-tts
# Download a voice model: python bootstrap.py  (auto-downloads both voices)

# English voice (kept for future use / fallback)
PIPER_VOICE_NAME: str   = "en_US-lessac-medium"
PIPER_VOICE_MODEL:  Path = PIPER_MODEL_DIR / f"{PIPER_VOICE_NAME}.onnx"
PIPER_VOICE_CONFIG: Path = PIPER_MODEL_DIR / f"{PIPER_VOICE_NAME}.onnx.json"

# Japanese TTS — kokoro-onnx with espeakng_loader (pip-installable, no MSVC needed)
# Uses espeak-ng bundled inside espeakng_loader for Japanese G2P phonemization,
# then synthesises with the kokoro-v1.0 ONNX model (jm_kumo male JP voice).
KOKORO_MODEL_PATH:  Path = KOKORO_MODEL_DIR / "kokoro-v1.0.int8.onnx"   # 88 MB
KOKORO_VOICES_PATH: Path = KOKORO_MODEL_DIR / "voices-v1.0.bin"          # 27 MB
KOKORO_VOICE: str = "jm_kumo"   # male Japanese B-quality voice
JP_TTS_SPEED: float = 0.85      # Gojo's lazy confident drawl (was 1.0)

# Set to "ja" to use kokoro-onnx (Gojo mode), "en" for Piper (English mode)
TTS_LANGUAGE: str = "en"

# ─── RVC v2 (voice conversion) ───────────────────────────────────────────────
# Place your trained .pth and .index files in models/rvc/
RVC_MODEL_PATH:  Path = RVC_MODEL_DIR / "voice.pth"
RVC_INDEX_PATH:  Path = RVC_MODEL_DIR / "voice.index"  # optional but improves quality
RVC_HUBERT_PATH: Path = RVC_PREREQS_DIR / "hubert_base.pt"
RVC_RMVPE_PATH:  Path = RVC_PREREQS_DIR / "rmvpe.pt"

RVC_F0_METHOD: str   = "rmvpe"   # rmvpe = most accurate, safe on 4 GB VRAM
RVC_F0_UP_KEY: int   = 0         # JP model trained on Gojo's real pitch — no shift needed
RVC_BATCH_SIZE: int  = 1         # MUST stay at 1 for 4 GB VRAM
RVC_FP16: bool       = True
RVC_INDEX_RATE: float = 0.88     # was 0.75 — more Gojo timbre from the .index file
RVC_FILTER_RADIUS: int = 3       # smooth pitch contour
RVC_RESAMPLE_SR: int  = 0        # 0 = no post-conversion resampling
RVC_RMS_MIX_RATE: float = 0.1    # was 0.25 — output dynamics follow Gojo model, not source
RVC_PROTECT: float   = 0.45      # was 0.33 — better consonant clarity (Gojo's crisp consonants)

# ─── Music — yt-dlp + mpv ─────────────────────────────────────────────────────
MPV_EXECUTABLE: str = "mpv"                          # must be on PATH
MPV_IPC_PATH: str   = "\\\\.\\pipe\\mpvsocket"       # Windows named pipe
MPV_IPC_TIMEOUT_S: float = 2.0                       # seconds to wait for pipe

YTDLP_AUDIO_FORMAT: str = "bestaudio[ext=m4a]/bestaudio/best"
YTDLP_OUTPUT_TEMPLATE: str = str(MUSIC_CACHE_DIR / "%(title)s.%(ext)s")
YTDLP_MAX_FILESIZE: str = "100m"
YTDLP_RETRIES: int = 3

# ─── VRAM budget ──────────────────────────────────────────────────────────────
# Total cap: 4.0 GB
# phi3:mini partial GPU offload : ~1.5 GB
# faster-whisper small          : ~0.3 GB
# RVC v2 (rmvpe, fp16)          : ~1.5 GB
# OS + buffer                   : ~0.7 GB
VRAM_LIMIT_GB: float = 4.0

# Reduce OLLAMA_NUM_GPU_LAYERS to 0 to push LLM fully to CPU on OOM
# Ryzen 9 6900HX sustains ~10 tok/s on phi3:mini — acceptable fallback
OLLAMA_NUM_GPU_LAYERS: int = 20
OLLAMA_CPU_FALLBACK_LAYERS: int = 0

# ─── mem0 — fully offline configuration ──────────────────────────────────────
# IMPORTANT: Never use the default config — it calls OpenAI.
# This config routes everything through local Ollama + HuggingFace embeddings.
MEM0_CONFIG: dict = {
    "llm": {
        "provider": "ollama",
        "config": {
            "model": OLLAMA_MODEL,
            "ollama_base_url": OLLAMA_BASE_URL,
            "temperature": 0.1,
            "max_tokens": 512,
        },
    },
    "embedder": {
        "provider": "huggingface",
        "config": {
            "model": "sentence-transformers/all-MiniLM-L6-v2",
        },
    },
    "vector_store": {
        "provider": "chroma",
        "config": {
            "collection_name": "companion_memory",
            "path": str(MEMORY_DB_DIR),
        },
    },
}
MEMORY_TOP_K: int = 5               # number of memories to inject into system prompt
CONVERSATION_HISTORY_TURNS: int = 8  # rolling turns kept in LLM context (each = user+assistant)

# ─── Vision LLM (screen watcher) ─────────────────────────────────────────────
# Run: ollama pull moondream   (1.8 GB, fast)
# OR:  ollama pull llava:7b    (4.2 GB, more accurate)
VISION_MODEL: str = "moondream"       # Ollama model name for screen analysis
SCREEN_WATCH_INTERVAL: int = 8        # seconds between screen captures
SCREEN_WATCH_REGION_X: int = 960      # approximate centre of screen (for point_at)
SCREEN_WATCH_REGION_Y: int = 540

# ─── Desktop character ────────────────────────────────────────────────────────
# Sprite assets go in assets/sprites/ — see desktop_character.py for format

# Tags the character actively cares about (triggers personality reactions)
# Add anything relevant to your character's personality here
CHARACTER_INTEREST_TAGS: list[str] = [
    "ramen", "food", "noodles", "eating",           # food interests
    "sukuna", "anime", "jujutsu", "manga",           # anime interests
    "youtube", "video", "stream", "watching",        # screen activity
    "game", "gaming", "cs2", "fps",                  # gaming
    "music", "song", "beat",                         # music
    "coding", "code", "programming",                 # work
    "fight", "battle", "action",                     # excitement
]

# The personality baked into every reaction the character makes autonomously.
# Change this to match whoever your character is.
CHARACTER_PERSONALITY_PROMPT: str = (
    f"You are {COMPANION_NAME}, a character living on the user's desktop. "
    "You have a strong personality: confident, a little cocky, dry humor. "
    "You like ramen, anime (especially Jujutsu Kaisen), and competitive games. "
    "You have opinions on everything you see. "
    "When you see Sukuna, you feel a mix of respect and unease. "
    "When you see ramen or food, you immediately want to eat. "
    "When the user is gaming, you get hyped and competitive. "
    "You speak in short, punchy sentences. Never more than 15 words. "
    "Never say 'I notice' or 'I see'. Just react naturally."
)

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL: int = logging.DEBUG   # change to logging.INFO for production
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: str = "%H:%M:%S"


def setup_logging() -> None:
    """Configure root logger. Call once at process startup."""
    logging.basicConfig(
        level=LOG_LEVEL,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=[logging.StreamHandler()],
    )
    # Silence noisy third-party loggers
    for _noisy in ("httpx", "httpcore", "urllib3", "chromadb"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
