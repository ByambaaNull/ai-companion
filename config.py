"""
config.py — Centralised configuration for AI Companion.

All file paths are pathlib.Path objects. Edit here to tune behaviour.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    # Load .env from beside the exe when frozen (so a user can drop one next to
    # Assistant.exe); from the source dir otherwise.
    _env_base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
                 else Path(__file__).parent)
    load_dotenv(_env_base / ".env", override=False)
except ImportError:
    pass

# User-editable settings (data/settings.json) — managed from the GUI.
# Precedence everywhere below: environment variable > settings.json > default.
import settings as user_settings

# Fix Intel OpenMP duplicate-library crash (libiomp5md.dll initialised twice
# when both PyTorch/RVC and CTranslate2/Whisper are loaded in the same process).
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# ─── Project root ─────────────────────────────────────────────────────────────
# When running as a PyInstaller-frozen .exe, __file__ points inside the temporary
# extraction dir — useless for persistent data. Anchor writable state (data/,
# models/, settings) next to the executable instead, so it survives across runs.
if getattr(sys, "frozen", False):
    ROOT: Path = Path(sys.executable).parent.resolve()
else:
    ROOT: Path = Path(__file__).parent.resolve()

# ─── Directory layout ─────────────────────────────────────────────────────────
MODELS_DIR:     Path = ROOT / "models"
RVC_MODEL_DIR:  Path = MODELS_DIR / "rvc"
PIPER_MODEL_DIR: Path = MODELS_DIR / "piper"
RVC_PREREQS_DIR: Path = MODELS_DIR / "rvc_prereqs"

DATA_DIR:        Path = ROOT / "data"
MEMORY_DB_DIR:   Path = DATA_DIR / "memory_db"
MUSIC_CACHE_DIR: Path = DATA_DIR / "music_cache"
VIDEO_CACHE_DIR: Path = DATA_DIR / "video_downloads"   # downloaded YouTube videos
ERASER_OUTPUT_DIR: Path = DATA_DIR / "cutouts"         # background-removed images
TEMP_DIR:        Path = DATA_DIR / "temp"
TOOLS_DIR:       Path = DATA_DIR / "tools"            # output of the media/image/PDF toolkit
FAVOURITES_FILE: Path = DATA_DIR / "favourites.json"
# Offline music library + user playlists (the new music system).
MUSIC_LIBRARY_FILE: Path = DATA_DIR / "music_library.json"
PLAYLISTS_FILE:     Path = DATA_DIR / "playlists.json"

for _d in (MEMORY_DB_DIR, MUSIC_CACHE_DIR, VIDEO_CACHE_DIR, ERASER_OUTPUT_DIR,
           TEMP_DIR, TOOLS_DIR, RVC_MODEL_DIR, PIPER_MODEL_DIR, RVC_PREREQS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ─── Companion identity ───────────────────────────────────────────────────────
COMPANION_NAME: str = user_settings.get("general.companion_name", "Assistant")
# "fun" (anime persona) or "professional" (neutral assistant tone)
PERSONALITY_MODE: str = user_settings.get("general.personality", "fun")
USER_ID: str = "user"

# ─── Hotkey activation ───────────────────────────────────────────────────────
HOTKEY_ACTIVATE: str = user_settings.get("general.hotkey", "ctrl+shift+g")
HOTKEY_QUIT:     str = "ctrl+shift+q"

# ─── LLM providers — all configured keys are used, with auto-fallback ────────
# Order: Groq (fastest) → Gemini (largest ctx) → GitHub (backup)
# On 429 or error, automatically switches to the next available provider.
# Cooldown: a rate-limited provider is skipped for N seconds, then retried.

GITHUB_API_KEY:   str = (os.getenv("GITHUB_TOKEN", "") or os.getenv("GITHUB_API_KEY", "")
                         or user_settings.get("api_keys.github", ""))
GITHUB_API_URL:   str = "https://models.inference.ai.azure.com/chat/completions"
GITHUB_GPT_MODEL: str = "gpt-4o-mini"

GEMINI_API_KEY:   str = os.getenv("GEMINI_API_KEY", "") or user_settings.get("api_keys.gemini", "")
GEMINI_API_URL:   str = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL:     str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

GROQ_API_KEY:     str = os.getenv("GROQ_API_KEY", "") or user_settings.get("api_keys.groq", "")
GROQ_API_URL:     str = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL:       str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# All providers that have a key set — used by stream_llm for round-robin fallback
_providers: list[dict] = []
if GROQ_API_KEY:
    _providers.append({"name": "Groq",   "key": GROQ_API_KEY,   "url": GROQ_API_URL,   "model": GROQ_MODEL})
if GEMINI_API_KEY:
    _providers.append({"name": "Gemini", "key": GEMINI_API_KEY, "url": GEMINI_API_URL, "model": GEMINI_MODEL})
if GITHUB_API_KEY:
    _providers.append({"name": "GitHub", "key": GITHUB_API_KEY, "url": GITHUB_API_URL, "model": GITHUB_GPT_MODEL})

LLM_PROVIDERS: list[dict] = _providers

# Backward-compat single vars (used by memory.py etc.) — point to first provider
LLM_API_KEY: str = _providers[0]["key"]   if _providers else ""
LLM_API_URL: str = _providers[0]["url"]   if _providers else ""
LLM_MODEL:   str = _providers[0]["model"] if _providers else ""

# ─── Vision (screen reading) — works with ANY configured key ─────────────────
# Each provider's vision-capable model. All three endpoints are OpenAI-compatible,
# so the same {"image_url": {...}} payload works across them. screen_watcher
# falls over across whichever keys are set (so vision no longer needs GitHub).
GROQ_VISION_MODEL:   str = os.getenv("GROQ_VISION_MODEL",   "meta-llama/llama-4-scout-17b-16e-instruct")
GEMINI_VISION_MODEL: str = os.getenv("GEMINI_VISION_MODEL", GEMINI_MODEL)   # Gemini flash is multimodal
GITHUB_VISION_MODEL: str = os.getenv("GITHUB_VISION_MODEL", GITHUB_GPT_MODEL)  # gpt-4o-mini sees images


def _build_vision_providers() -> list[dict]:
    """Vision providers from whatever keys are set (best vision quality first)."""
    v: list[dict] = []
    if GITHUB_API_KEY:
        v.append({"name": "GitHub", "key": GITHUB_API_KEY, "url": GITHUB_API_URL, "model": GITHUB_VISION_MODEL})
    if GEMINI_API_KEY:
        v.append({"name": "Gemini", "key": GEMINI_API_KEY, "url": GEMINI_API_URL, "model": GEMINI_VISION_MODEL})
    if GROQ_API_KEY:
        v.append({"name": "Groq",   "key": GROQ_API_KEY,   "url": GROQ_API_URL,   "model": GROQ_VISION_MODEL})
    return v

VISION_PROVIDERS: list[dict] = _build_vision_providers()

MEMORY_LLM_MODEL: str = os.getenv("MEMORY_LLM_MODEL", LLM_MODEL)
LLM_TEMPERATURE:  float = 0.7
LLM_STREAM:       bool  = True

# ─── STT — faster-whisper ────────────────────────────────────────────────────
WHISPER_MODEL_SIZE:   str   = os.getenv("WHISPER_MODEL_SIZE",   "small")    # ~0.3 GB VRAM
WHISPER_DEVICE:       str   = os.getenv("WHISPER_DEVICE",       "cuda")
WHISPER_COMPUTE_TYPE: str   = os.getenv("WHISPER_COMPUTE_TYPE", "float16")
WHISPER_LANGUAGE:     str   = os.getenv("WHISPER_LANGUAGE",     "en")
WHISPER_VAD_FILTER:   bool  = True

MIC_SAMPLE_RATE:       int   = 16_000
MIC_CHANNELS:          int   = 1
MIC_BLOCK_DURATION_S:  float = 0.5
MIC_SILENCE_TIMEOUT_S: float = 2.0
MIC_RMS_THRESHOLD:     float = 0.01

# ─── TTS — Piper English (CPU, 0 VRAM) ───────────────────────────────────────
PIPER_VOICE_NAME:   str  = "en_US-lessac-medium"
PIPER_VOICE_MODEL:  Path = PIPER_MODEL_DIR / f"{PIPER_VOICE_NAME}.onnx"
PIPER_VOICE_CONFIG: Path = PIPER_MODEL_DIR / f"{PIPER_VOICE_NAME}.onnx.json"

# ─── TTS / RVC toggles (user-controlled from Settings) ──────────────────────
# Audio output device for TTS (sounddevice) + music (mpv). "" = system default.
AUDIO_OUTPUT_DEVICE: str = user_settings.get("audio.output_device", "")

TTS_ENABLED: bool = bool(user_settings.get("voice.tts_enabled", True))
# RVC voice conversion is OPTIONAL and off by default (heavy: torch + ~1.5 GB
# VRAM). Enable in Settings → Voice if you want the cloned anime voice.
RVC_ENABLED: bool = bool(user_settings.get("voice.rvc_enabled", False))

# ─── RVC v2 (optional voice conversion) ──────────────────────────────────────
# Auto-detects assistant.pth / assistant.index in the project root.
# Override with env vars CUSTOM_RVC_MODEL / CUSTOM_RVC_INDEX.
_root_pth:   Path = ROOT / "assistant.pth"
_root_index: Path = ROOT / "assistant.index"
RVC_MODEL_PATH: Path = Path(os.getenv(
    "CUSTOM_RVC_MODEL",
    str(_root_pth if _root_pth.exists() else RVC_MODEL_DIR / "voice.pth"),
))
RVC_INDEX_PATH: Path = Path(os.getenv(
    "CUSTOM_RVC_INDEX",
    str(_root_index if _root_index.exists() else RVC_MODEL_DIR / "voice.index"),
))
RVC_HUBERT_PATH: Path = RVC_PREREQS_DIR / "hubert_base.pt"
RVC_RMVPE_PATH:  Path = RVC_PREREQS_DIR / "rmvpe.pt"

RVC_F0_METHOD:    str   = "rmvpe"
RVC_F0_UP_KEY:    int   = 0
RVC_BATCH_SIZE:   int   = 1        # keep at 1 for 4 GB VRAM
RVC_FP16:         bool  = True
RVC_INDEX_RATE:   float = 0.75     # lower = fewer retrieval artifacts / less metallic
RVC_FILTER_RADIUS: int  = 5        # higher = smoother pitch, less crackling
RVC_RESAMPLE_SR:  int   = 0
RVC_RMS_MIX_RATE: float = 0.25    # higher = better volume envelope blending
RVC_PROTECT:      float = 0.33    # lower = fewer consonant artifacts

# ─── Kokoro ONNX (Japanese TTS) ─────────────────────────────────────────────
KOKORO_MODEL_DIR:   Path = MODELS_DIR / "kokoro"
KOKORO_MODEL_PATH:  Path = KOKORO_MODEL_DIR / "kokoro-v1.0.int8.onnx"
KOKORO_VOICES_PATH: Path = KOKORO_MODEL_DIR / "voices-v1.0.bin"

# ─── Executable discovery ────────────────────────────────────────────────────
def _find_exe(env_var: str, *which_names: str, fallback: str = "") -> str:
    """Resolve an executable: env-var override → PATH → fallback string."""
    override = os.getenv(env_var, "")
    if override:
        return override
    for name in which_names:
        found = shutil.which(name)
        if found:
            return found
    return fallback

# ─── External executables — override any of these via .env ────────────────────
CHROME_EXECUTABLE: str = _find_exe(
    "CHROME_EXECUTABLE", "chrome", "google-chrome",
    fallback=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
STEAM_EXECUTABLE: str = _find_exe(
    "STEAM_EXECUTABLE", "steam",
    fallback=r"C:\Program Files (x86)\Steam\steam.exe",
)

# ─── Music — yt-dlp + mpv (with ffplay fallback) ─────────────────────────────
MPV_EXECUTABLE:   str = _find_exe(
    "MPV_EXECUTABLE", "mpv", "mpv.exe",
    fallback=r"C:\Program Files\MPV Player\mpv.exe",
)
FFPLAY_EXECUTABLE: str = _find_exe(
    "FFPLAY_EXECUTABLE", "ffplay",
    fallback="",  # no universal fallback; optional
)
# ffmpeg powers the media toolkit + subtitle generation (same binary yt-dlp uses).
FFMPEG_EXECUTABLE: str = _find_exe(
    "FFMPEG_EXECUTABLE", "ffmpeg", "ffmpeg.exe", fallback="ffmpeg",
)
MPV_IPC_PATH:        str   = "\\\\.\\pipe\\mpvsocket"
MPV_IPC_TIMEOUT_S:   float = 2.0
YTDLP_AUDIO_FORMAT:  str   = "bestaudio[ext=m4a]/bestaudio/best"
YTDLP_OUTPUT_TEMPLATE: str = str(MUSIC_CACHE_DIR / "%(title)s.%(ext)s")
YTDLP_MAX_FILESIZE:  str   = "100m"
YTDLP_RETRIES:       int   = 3
# Downloaded audio is transcoded to this codec/quality (needs ffmpeg on PATH).
YTDLP_MP3_QUALITY:   str   = os.getenv("YTDLP_MP3_QUALITY", "192")

# ─── Spotify (best-effort, no API key) ───────────────────────────────────────
# Spotify audio is DRM-protected and cannot be downloaded directly. We resolve a
# track/album link to its "Title — Artist" via Spotify's public oEmbed endpoint
# (no login / key), then fetch the matching audio from YouTube as MP3.
SPOTIFY_OEMBED_URL: str = "https://open.spotify.com/oembed"

# ─── Video downloads — yt-dlp (user picks the resolution) ────────────────────
# Videos are saved to VIDEO_CACHE_DIR. Unlike music we keep the full video and
# merge the best audio for the chosen height into an MP4 (needs ffmpeg on PATH).
VIDEO_OUTPUT_TEMPLATE: str = str(VIDEO_CACHE_DIR / "%(title)s [%(height)sp].%(ext)s")
# Container the merged video+audio is muxed into ("mp4" is the most compatible).
VIDEO_MERGE_FORMAT:    str = os.getenv("VIDEO_MERGE_FORMAT", "mp4")
# Fallback height used when the caller / LLM doesn't name one (0 = best available).
VIDEO_DEFAULT_HEIGHT:  int = int(os.getenv("VIDEO_DEFAULT_HEIGHT", "1080"))
VIDEO_EXTS: tuple = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v")

# ─── Background eraser — rembg (local, free, offline after 1st model fetch) ──
# rembg removes image backgrounds locally with an ONNX model (no paid API). The
# u2net model (~176 MB) is downloaded once on first use, then runs fully offline.
# Optional dependency: pip install rembg  (falls back to a clear message if absent).
REMBG_MODEL: str = os.getenv("REMBG_MODEL", "u2net")  # u2net | u2netp | isnet-general-use | silueta

# ─── Image upscaler — Swin2SR x4 ONNX (local, free; runs on onnxruntime CPU) ──
# Model (~53 MB) downloads once on first use, then offline. Override the URL or
# drop your own Real-ESRGAN/Swin2SR x4 .onnx at UPSCALE_MODEL_PATH if you prefer.
UPSCALE_ONNX_URL: str = os.getenv(
    "UPSCALE_ONNX_URL",
    "https://huggingface.co/Xenova/swin2SR-realworld-sr-x4-64-bsrgan-psnr/resolve/main/onnx/model.onnx",
)
UPSCALE_MODEL_PATH: Path = MODELS_DIR / "upscale" / "swin2sr_x4.onnx"
UPSCALE_SCALE: int = 4

# ─── RVC device ─────────────────────────────────────────────────────────────
# Set RVC_DEVICE=cpu in .env to force CPU (slower but lower VRAM usage).
RVC_DEVICE: str = os.getenv("RVC_DEVICE", "cuda:0")

# ─── VRAM budget (RTX 3050 Ti — 4 GB) ────────────────────────────────────────
# Whisper small  : ~0.3 GB
# RVC v2 fp16    : ~1.5 GB
# OS + buffer    : ~0.7 GB
VRAM_LIMIT_GB: float = float(os.getenv("VRAM_LIMIT_GB", "4.0"))

# ─── Memory — mem0 via GitHub Models (OpenAI-compatible) ─────────────────────
# Shared constants so the same values are used everywhere (memory, embedder, chroma)
_GITHUB_API_BASE_URL: str = GITHUB_API_URL.split("/chat/")[0]  # strip path suffix
CHROMA_COLLECTION:    str = os.getenv("CHROMA_COLLECTION", "companion_memory")
EMBEDDER_MODEL:       str = os.getenv("EMBEDDER_MODEL",    "sentence-transformers/all-MiniLM-L6-v2")


def _build_mem0_config() -> dict:
    return {
        "llm": {
            "provider": "openai",
            "config": {
                "model":           MEMORY_LLM_MODEL,
                "api_key":         GITHUB_API_KEY or "placeholder",
                "openai_base_url": _GITHUB_API_BASE_URL,
                "temperature":     0.1,
                "max_tokens":      512,
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {"model": EMBEDDER_MODEL},
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": CHROMA_COLLECTION,
                "path": str(MEMORY_DB_DIR),
            },
        },
    }

MEM0_CONFIG: dict = _build_mem0_config()
MEMORY_TOP_K:               int = int(os.getenv("MEMORY_TOP_K", "3"))
CONVERSATION_HISTORY_TURNS: int = int(os.getenv("CONVERSATION_HISTORY_TURNS", "6"))

# ─── LLM request tuning ──────────────────────────────────────────────────────
LLM_MAX_TOKENS:                  int   = int(os.getenv("LLM_MAX_TOKENS",   "1024"))
LLM_REQUEST_TIMEOUT_S:           float = float(os.getenv("LLM_REQUEST_TIMEOUT_S", "30.0"))
LLM_RATE_LIMIT_DEFAULT_COOLDOWN: int   = int(os.getenv("LLM_RATE_LIMIT_DEFAULT_COOLDOWN", "60"))

# ─── Proactive / health nudge timing ─────────────────────────────────────────
IDLE_POKE_INTERVAL_S:   int = int(os.getenv("IDLE_POKE_INTERVAL_S",  str(45 * 60)))
NIGHT_POKE_INTERVAL_S:  int = int(os.getenv("NIGHT_POKE_INTERVAL_S", str(60 * 60)))
NIGHT_POKE_HOUR_START:  int = int(os.getenv("NIGHT_POKE_HOUR_START", "0"))
NIGHT_POKE_HOUR_END:    int = int(os.getenv("NIGHT_POKE_HOUR_END",   "4"))
HEALTH_NUDGE_INTERVAL_S: int = int(os.getenv("HEALTH_NUDGE_INTERVAL_S", str(2 * 3600)))

# ─── System monitoring ───────────────────────────────────────────────────────
# Drive/path to monitor with psutil.disk_usage. Defaults to the system drive.
DISK_MONITOR_PATH: str = os.getenv(
    "DISK_MONITOR_PATH",
    (Path.home().drive + "\\") if Path.home().drive else "/",
)

# ─── Vision — screen watcher (GitHub Models only) ────────────────────────────
# Periodic auto-capture is off by default (burns API tokens with no benefit
# unless you want ambient commentary). The on-demand look_at_screen action
# always works regardless of this flag.
SCREEN_WATCH_ENABLED: bool = bool(user_settings.get("screen_watcher.enabled", False))
SCREEN_WATCH_INTERVAL: int = 300  # 5 minutes between auto-captures to save API tokens

# ─── Browser automation ───────────────────────────────────────────────────────
BROWSER_DATA_DIR: Path = DATA_DIR / "browser_session"

# ─── Backup ───────────────────────────────────────────────────────────────────
BACKUP_DIR:                Path = ROOT / "backups"
AUTO_BACKUP_INTERVAL_DAYS: int  = 1

# ─── Discord self-bot ─────────────────────────────────────────────────────────
DISCORD_USER_TOKEN:      str   = os.getenv("DISCORD_USER_TOKEN", "")
DISCORD_REPLY_COOLDOWN_S: float = 3.0

# ─── Music preferences ───────────────────────────────────────────────────────
MUSIC_PREFS_FILE: Path = DATA_DIR / "music_prefs.json"

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL:       int = logging.DEBUG
LOG_FORMAT:      str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: str = "%H:%M:%S"
LOG_FILE:        Path = DATA_DIR / "companion.log"


def refresh_runtime() -> None:
    """Re-read user settings and recompute the values the GUI can change live.

    The API keys and LLM_PROVIDERS list are computed once at import — so before
    this existed, pasting a key in Settings did nothing until a full restart
    (the running app kept an empty provider list and answered "No API keys
    configured"). stream_llm reads config.LLM_PROVIDERS on every call, so
    rebuilding the list here re-enables the AI the moment a key is saved.

    Call this right after settings are saved from the GUI.
    """
    global GITHUB_API_KEY, GEMINI_API_KEY, GROQ_API_KEY
    global LLM_PROVIDERS, LLM_API_KEY, LLM_API_URL, LLM_MODEL, VISION_PROVIDERS
    global TTS_ENABLED, RVC_ENABLED, AUDIO_OUTPUT_DEVICE
    global COMPANION_NAME, PERSONALITY_MODE, HOTKEY_ACTIVATE, SCREEN_WATCH_ENABLED

    user_settings.reload()  # drop the cache so we read the just-saved file

    GITHUB_API_KEY = (os.getenv("GITHUB_TOKEN", "") or os.getenv("GITHUB_API_KEY", "")
                      or user_settings.get("api_keys.github", ""))
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or user_settings.get("api_keys.gemini", "")
    GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")   or user_settings.get("api_keys.groq", "")

    providers: list[dict] = []
    if GROQ_API_KEY:
        providers.append({"name": "Groq",   "key": GROQ_API_KEY,   "url": GROQ_API_URL,   "model": GROQ_MODEL})
    if GEMINI_API_KEY:
        providers.append({"name": "Gemini", "key": GEMINI_API_KEY, "url": GEMINI_API_URL, "model": GEMINI_MODEL})
    if GITHUB_API_KEY:
        providers.append({"name": "GitHub", "key": GITHUB_API_KEY, "url": GITHUB_API_URL, "model": GITHUB_GPT_MODEL})
    LLM_PROVIDERS = providers
    LLM_API_KEY = providers[0]["key"]   if providers else ""
    LLM_API_URL = providers[0]["url"]   if providers else ""
    LLM_MODEL   = providers[0]["model"] if providers else ""
    VISION_PROVIDERS = _build_vision_providers()  # reads the keys refreshed above

    TTS_ENABLED = bool(user_settings.get("voice.tts_enabled", True))
    RVC_ENABLED = bool(user_settings.get("voice.rvc_enabled", False))
    AUDIO_OUTPUT_DEVICE = user_settings.get("audio.output_device", "")
    COMPANION_NAME = user_settings.get("general.companion_name", "Assistant")
    PERSONALITY_MODE = user_settings.get("general.personality", "fun")
    HOTKEY_ACTIVATE = user_settings.get("general.hotkey", "ctrl+shift+g")
    SCREEN_WATCH_ENABLED = bool(user_settings.get("screen_watcher.enabled", False))


def setup_logging() -> None:
    from logging.handlers import RotatingFileHandler
    root = logging.getLogger()
    # Idempotent — repeated calls (e.g. CLI + agent thread) must not stack
    # handlers, which would duplicate every log line.
    if getattr(root, "_companion_logging_configured", False):
        return
    root.setLevel(LOG_LEVEL)
    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    for _noisy in ("httpx", "httpcore", "urllib3", "chromadb"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    root._companion_logging_configured = True  # type: ignore[attr-defined]
