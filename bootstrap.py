"""
bootstrap.py — First-run setup for AI Companion.

Checks that all required model files, binaries, and services are present.
Downloads anything missing. After this runs successfully once, the system
operates fully offline.

Run before first use:
    python bootstrap.py
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import config
from config import setup_logging

log = logging.getLogger("bootstrap")

# ─── Download registry ────────────────────────────────────────────────────────
# (url, destination_path, sha256_hex_or_None)
# Both files come from the official RVC project's HuggingFace repo.
REQUIRED_FILES: list[tuple[str, Path, str | None]] = [
    (
        "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/hubert_base.pt",
        config.RVC_HUBERT_PATH,
        None,
    ),
    (
        "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt",
        config.RVC_RMVPE_PATH,
        None,
    ),
]

# Piper Windows release — update URL when a newer version is released
PIPER_WINDOWS_URL = (
    "https://github.com/OHF-Voice/piper1-gpl/releases/download/2024.5.2/"
    "piper_windows_amd64.zip"
)

# Piper voice model — HuggingFace direct download URLs
_PIPER_VOICE_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
    "en/en_US/lessac/medium/"
)
PIPER_VOICE_FILES: list[tuple[str, Path]] = [
    (
        _PIPER_VOICE_BASE + f"{config.PIPER_VOICE_NAME}.onnx",
        config.PIPER_VOICE_MODEL,
    ),
    (
        _PIPER_VOICE_BASE + f"{config.PIPER_VOICE_NAME}.onnx.json",
        config.PIPER_VOICE_CONFIG,
    ),
]


# ─── Utilities ────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, description: str) -> None:
    """Download url to dest with a progress indicator."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s → %s", description, dest)
    try:
        def _reporthook(count: int, block_size: int, total_size: int) -> None:
            if total_size > 0:
                pct = min(100, count * block_size * 100 // total_size)
                print(f"\r  {description}: {pct}%", end="", flush=True)

        urllib.request.urlretrieve(url, dest, reporthook=_reporthook)
        print()  # newline after progress
        log.info("  → saved to %s", dest)
    except Exception as exc:
        log.error("Failed to download %s: %s", url, exc)
        raise


def _check_command(cmd: str) -> bool:
    """Return True if cmd is available on PATH."""
    return shutil.which(cmd) is not None


# ─── Individual checks ────────────────────────────────────────────────────────

def check_python_version() -> None:
    if sys.version_info < (3, 11):
        log.error("Python 3.11+ required. Current: %s", sys.version)
        sys.exit(1)
    log.info("Python %s.%s ✓", sys.version_info.major, sys.version_info.minor)


def check_github_api_key() -> None:
    """Warn if GITHUB_TOKEN is not set — the LLM and vision features won't work without it."""
    if config.GITHUB_API_KEY:
        log.info("GitHub API key ✓ (model: %s)", config.GITHUB_GPT_MODEL)
    else:
        log.warning(
            "GITHUB_TOKEN not set — LLM, memory, and vision features will not work.\n"
            "  Create a .env file in the project root with:\n"
            "  GITHUB_TOKEN=ghp_..."
        )


def check_mpv() -> None:
    if _check_command("mpv"):
        log.info("mpv ✓")
    else:
        log.warning(
            "mpv not found on PATH. Install via:\n"
            "  choco install mpv   (Chocolatey)\n"
            "  scoop install mpv   (Scoop)\n"
            "Music playback will not work."
        )


def download_rvc_prereqs() -> None:
    """Download hubert_base.pt and rmvpe.pt if missing."""
    # Skip entirely if no RVC model is installed yet — prereqs only matter at
    # inference time, and the user may set up their RVC model later.
    if not config.RVC_MODEL_PATH.exists():
        log.info(
            "No RVC model found — skipping prereq download. "
            "Run bootstrap.py again after placing your .pth model in %s.",
            config.RVC_MODEL_DIR,
        )
        return

    for url, dest, expected_sha256 in REQUIRED_FILES:
        if dest.exists():
            log.info("%s ✓ (already present)", dest.name)
            if expected_sha256 and _sha256(dest) != expected_sha256:
                log.warning("  SHA256 mismatch for %s — re-downloading", dest.name)
                dest.unlink()
            else:
                continue
        try:
            _download(url, dest, dest.name)
        except Exception as exc:
            log.warning(
                "Could not download %s: %s\n"
                "  You can download it manually from:\n"
                "  %s",
                dest.name, exc, url,
            )
            continue
        if expected_sha256 and _sha256(dest) != expected_sha256:
            log.error("SHA256 mismatch after download: %s", dest.name)
            dest.unlink()


def check_piper() -> None:
    """Verify piper-tts Python package is installed."""
    try:
        import importlib
        importlib.import_module("piper")
        log.info("piper-tts package ✓")
    except ImportError:
        log.warning(
            "piper-tts not installed. Run:\n"
            "  pip install piper-tts\n"
            "TTS will not work until installed."
        )


def _download_voice_files(files: list[tuple[str, Path]], voice_name: str) -> None:
    all_ok = True
    for url, dest in files:
        if dest.exists():
            log.info("  %s ✓ (already present)", dest.name)
            continue
        try:
            _download(url, dest, dest.name)
        except Exception as exc:
            log.warning(
                "Could not download %s: %s\n"
                "  Manual download: %s",
                dest.name, exc, url,
            )
            all_ok = False

    if all_ok:
        log.info("Piper voice model '%s' downloaded ✓", voice_name)
    else:
        log.warning(
            "Voice model download incomplete. "
            "You can also run: python -m piper.download_voices %s\n"
            "Then move the files to: %s",
            voice_name,
            config.PIPER_MODEL_DIR,
        )


def check_piper_voice() -> None:
    """Download English Piper voice model if not already present."""
    if config.PIPER_VOICE_MODEL.exists() and config.PIPER_VOICE_CONFIG.exists():
        log.info("Piper EN voice ✓ (%s)", config.PIPER_VOICE_NAME)
    else:
        log.info("Piper EN voice '%s' not found — downloading…", config.PIPER_VOICE_NAME)
        _download_voice_files(PIPER_VOICE_FILES, config.PIPER_VOICE_NAME)


_KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_MODEL_FILES: list[tuple[str, Path]] = [
    (_KOKORO_BASE + "kokoro-v1.0.int8.onnx", config.KOKORO_MODEL_PATH),
    (_KOKORO_BASE + "voices-v1.0.bin",       config.KOKORO_VOICES_PATH),
]


def check_kokoro_models() -> None:
    """Download Kokoro ONNX model files for Japanese TTS (jm_kumo voice)."""
    if config.KOKORO_MODEL_PATH.exists() and config.KOKORO_VOICES_PATH.exists():
        log.info("Kokoro JP TTS models ✓")
        return
    log.info("Kokoro JP TTS models not found — downloading (~115 MB)…")
    all_ok = True
    for url, dest in KOKORO_MODEL_FILES:
        if dest.exists():
            log.info("  %s ✓ (already present)", dest.name)
            continue
        try:
            _download(url, dest, dest.name)
        except Exception as exc:
            log.warning("Could not download %s: %s\n  Manual URL: %s", dest.name, exc, url)
            all_ok = False
    if all_ok:
        log.info("Kokoro JP TTS models downloaded ✓")
    else:
        log.warning("Kokoro model download incomplete — Japanese TTS will be unavailable.")


def check_openjtalk() -> None:
    """Legacy stub — no-op. Japanese TTS is now handled by check_kokoro_models()."""
    pass


def check_rvc_model() -> None:
    """Warn if no user RVC .pth model is installed."""
    if config.RVC_MODEL_PATH.exists():
        log.info("RVC voice model ✓")
    else:
        log.warning(
            "No RVC model found at %s\n"
            "Train or download an RVC v2 .pth model and place it there.\n"
            "Voice conversion will be skipped (Piper output played directly).",
            config.RVC_MODEL_PATH,
        )


def check_rvc_python() -> None:
    """Verify rvc-python is importable."""
    try:
        import importlib
        importlib.import_module("rvc_python")
        log.info("rvc-python ✓")
    except ImportError:
        log.warning(
            "rvc-python not installed. Run:\n"
            "  pip install rvc-python\n"
            "Voice conversion will be skipped until installed."
        )


def check_directories() -> None:
    """Ensure all data directories exist."""
    for d in (
        config.MEMORY_DB_DIR,
        config.MUSIC_CACHE_DIR,
        config.TEMP_DIR,
        config.RVC_MODEL_DIR,
        config.PIPER_MODEL_DIR,
        config.RVC_PREREQS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
    log.info("Data directories ✓")


def check_favourites() -> None:
    """Create default favourites.json if it doesn't exist."""
    if not config.FAVOURITES_FILE.exists():
        import json
        default_favourites: dict[str, dict] = {
            "lofi": {
                "url": "https://www.youtube.com/watch?v=jfKfPfyJRdk",
                "local_path": None,
                "description": "Lofi Hip Hop Radio",
            },
        }
        config.FAVOURITES_FILE.write_text(
            json.dumps(default_favourites, indent=2), encoding="utf-8"
        )
        log.info("Created default favourites.json ✓")
    else:
        log.info("favourites.json ✓")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_bootstrap() -> None:
    setup_logging()
    log.info("=" * 60)
    log.info("AI Companion Bootstrap")
    log.info("=" * 60)

    check_python_version()
    check_directories()
    check_github_api_key()
    check_mpv()
    check_piper()
    check_piper_voice()
    check_kokoro_models()
    if config.RVC_ENABLED:
        download_rvc_prereqs()
        check_rvc_model()
        check_rvc_python()
    else:
        log.info("RVC voice conversion disabled (Settings → Voice) — skipping RVC setup.")
    check_favourites()

    log.info("=" * 60)
    log.info("Bootstrap complete. Disconnect network — system is now offline-capable.")
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Companion first-run setup")
    args = parser.parse_args()
    run_bootstrap()
