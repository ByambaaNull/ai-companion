"""
settings.py — User-editable settings persisted to data/settings.json.

This is the single source of truth for everything a user can change from
the GUI (no more editing config.py / .env by hand). config.py reads these
values at startup; the GUI reads/writes them live.

Usage:
    import settings
    settings.get("voice.rvc_enabled")          # -> False
    settings.set("voice.rvc_enabled", True)    # persisted immediately
    settings.all()                             # full merged dict

Values are deep-merged over DEFAULTS, so adding new defaults in a future
version never breaks an existing settings.json.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Anchor settings next to the exe when frozen (matches config.ROOT) so
# data/settings.json lives where config.py reads the rest of the data — and
# survives the in-app updater's rebuild, which preserves dist\Assistant\data
# (NOT the regenerated _internal\ dir where __file__ would otherwise point).
if getattr(sys, "frozen", False):
    _ROOT = Path(sys.executable).parent.resolve()
else:
    _ROOT = Path(__file__).parent.resolve()
_DATA_DIR = _ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE: Path = _DATA_DIR / "settings.json"

# ─── Defaults — every setting the app understands ────────────────────────────
DEFAULTS: dict = {
    "general": {
        "companion_name": "Assistant",
        # "fun" = casual/playful tone, "professional" = neutral assistant tone
        "personality": "fun",
        "hotkey": "ctrl+shift+g",
        "language": "en",
    },
    "appearance": {
        "theme": "dark",          # dark | light
    },
    "voice": {
        "tts_enabled": True,       # speak replies out loud (voice turns only)
        "rvc_enabled": False,      # OPTIONAL anime voice conversion (heavy, GPU)
    },
    "audio": {
        # Output device name for TTS + music. "" = system default.
        "output_device": "",
    },
    "api_keys": {
        # Filled from the GUI; .env vars still win if set.
        "groq": "",
        "gemini": "",
        "github": "",
    },
    "screen_watcher": {
        "enabled": False,          # periodic screen vision (uses API tokens)
    },
    "email": {
        "enabled": False,
        "imap_host": "imap.gmail.com",
        "address": "",
        "app_password": "",        # Gmail App Password / provider app password
        "max_fetch": 25,
    },
    "automations": {
        # Master switch for the background automation engine
        "enabled": True,
        # Only run background jobs when the user has been idle this long (s)
        "idle_threshold_s": 180,
        # Soft cap on background LLM calls per day (foreground chat unaffected)
        "daily_llm_budget": 150,
        # Individual jobs
        "email_sweep": False,
        "email_sweep_interval_min": 30,
        "downloads_organizer": False,
        "downloads_organizer_dry_run": True,
        "daily_brief": False,
        "daily_brief_time": "08:30",
        "reminder_check": True,
    },
}

_lock = threading.Lock()
_cache: dict | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    user: dict = {}
    if SETTINGS_FILE.exists():
        try:
            user = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("settings.json unreadable (%s) — using defaults", exc)
    _cache = _deep_merge(DEFAULTS, user)
    return _cache


def _save(data: dict) -> None:
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def all() -> dict:  # noqa: A001 — intentional, mirrors dict-like API
    """Full merged settings dict (a deep copy — safe to mutate)."""
    with _lock:
        return copy.deepcopy(_load())


def get(path: str, default: Any = None) -> Any:
    """Read a setting by dotted path, e.g. get("voice.rvc_enabled")."""
    with _lock:
        node: Any = _load()
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def set(path: str, value: Any) -> None:  # noqa: A001
    """Write a setting by dotted path and persist immediately."""
    with _lock:
        data = _load()
        node = data
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        _save(data)


def update(values: dict) -> None:
    """Deep-merge a dict of changes (e.g. from the settings dialog) and persist."""
    global _cache
    with _lock:
        data = _deep_merge(_load(), values)
        _cache = data
        _save(data)


def reload() -> None:
    """Drop the cache so the next read hits disk (used after external edits)."""
    global _cache
    with _lock:
        _cache = None
