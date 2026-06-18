"""
actions/system_info.py — Weather, system stats, notes, and clipboard.

Weather:  open-meteo.com  (free, no key required)
Location: ip-api.com      (free, no key, IP-based)
Geocode:  geocoding-api.open-meteo.com
System:   psutil
Notes:    data/notes.txt  (append/read/clear)
Clipboard: pyperclip
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

import config

log = logging.getLogger(__name__)

_NOTES_FILE: Path = config.DATA_DIR / "notes.txt"


# ─── Weather ─────────────────────────────────────────────────────────────────

_WMO: dict[int, str] = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "icy fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "heavy showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "heavy thunderstorm",
}


def _wmo(code: int) -> str:
    return _WMO.get(code, "mixed conditions")


async def get_weather(location: str = "") -> str:
    """Fetch current weather. Uses IP-based location if none given."""
    try:
        import httpx
    except ImportError:
        return "httpx not installed."

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if location.strip():
                r = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": location.strip(), "count": 1, "language": "en"},
                )
                results = r.json().get("results", [])
                if not results:
                    return f"Couldn't find location: '{location}'"
                geo = results[0]
                lat, lon = geo["latitude"], geo["longitude"]
                city = geo.get("name", location)
            else:
                r = await client.get("http://ip-api.com/json/?fields=city,lat,lon")
                geo = r.json()
                lat, lon = geo["lat"], geo["lon"]
                city = geo.get("city", "your location")

            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude":         lat,
                    "longitude":        lon,
                    "current":          "temperature_2m,apparent_temperature,"
                                        "weathercode,wind_speed_10m,precipitation",
                    "temperature_unit": "celsius",
                    "wind_speed_unit":  "kmh",
                    "forecast_days":    1,
                },
            )
            d = r.json()["current"]
            temp  = d.get("temperature_2m", "?")
            feels = d.get("apparent_temperature", "?")
            cond  = _wmo(d.get("weathercode", 0))
            wind  = d.get("wind_speed_10m", 0)
            rain  = d.get("precipitation", 0)

            parts = [f"{city}: {temp}°C (feels {feels}°C), {cond}"]
            if wind:
                parts.append(f"wind {wind} km/h")
            if rain > 0:
                parts.append(f"{rain} mm rain")
            return ", ".join(parts) + "."
    except Exception as exc:
        log.error("Weather fetch failed: %s", exc)
        return f"Couldn't get weather right now: {exc}"


# ─── System info ─────────────────────────────────────────────────────────────

def get_system_info(_: str = "") -> str:
    """Return CPU, RAM, battery, and disk usage."""
    try:
        import psutil
    except ImportError:
        return "psutil not installed — run: pip install psutil"

    parts: list[str] = []

    cpu = psutil.cpu_percent(interval=0.5)
    parts.append(f"CPU {cpu:.0f}%")

    ram = psutil.virtual_memory()
    parts.append(f"RAM {ram.percent:.0f}% ({ram.available // 1024**2:.0f} MB free)")

    try:
        bat = psutil.sensors_battery()
        if bat:
            status = "charging" if bat.power_plugged else "on battery"
            parts.append(f"Battery {bat.percent:.0f}% ({status})")
    except Exception:
        pass

    try:
        import config as _cfg
        disk = psutil.disk_usage(_cfg.DISK_MONITOR_PATH)
        label = _cfg.DISK_MONITOR_PATH.rstrip("\\/") or "Disk"
        parts.append(f"{label}: {disk.percent:.0f}% used ({disk.free // 1024**3} GB free)")
    except Exception:
        pass

    return ", ".join(parts) + "."


# ─── Notes ───────────────────────────────────────────────────────────────────

def add_note(text: str) -> str:
    """Append a timestamped note to notes.txt."""
    text = text.strip()
    if not text:
        return "Nothing to save — include something after 'note:'."
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"[{ts}] {text}\n"
    with _NOTES_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    return f"Noted: {text}"


def read_notes(query: str = "") -> str:
    """Read saved notes, optionally filtered by keyword."""
    if not _NOTES_FILE.exists() or _NOTES_FILE.stat().st_size == 0:
        return "No notes yet. Say 'note: something' to save one."
    lines = _NOTES_FILE.read_text(encoding="utf-8").strip().splitlines()
    if query.strip():
        q = query.lower()
        lines = [l for l in lines if q in l.lower()]
        if not lines:
            return f"No notes matching '{query}'."
    recent = lines[-10:]
    header = f"Last {len(recent)} note{'s' if len(recent) != 1 else ''}:"
    return header + "\n" + "\n".join(recent)


def clear_notes(_: str = "") -> str:
    """Delete all notes."""
    if _NOTES_FILE.exists():
        _NOTES_FILE.unlink()
    return "All notes cleared."


# ─── Clipboard ───────────────────────────────────────────────────────────────

def read_clipboard(_: str = "") -> str:
    """Return current clipboard contents."""
    try:
        import pyperclip
        text = pyperclip.paste()
        if not text:
            return "Clipboard is empty."
        preview = text[:300]
        suffix  = "…" if len(text) > 300 else ""
        return f"Clipboard: {preview}{suffix}"
    except Exception as exc:
        return f"Couldn't read clipboard: {exc}"


# ─── Journal ─────────────────────────────────────────────────────────────────

_JOURNAL_FILE: Path = config.DATA_DIR / "journal.txt"


def add_journal_entry(text: str) -> str:
    """Append a timestamped journal entry."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n[{timestamp}]\n{text.strip()}\n"
    with open(_JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write(entry)
    return "Saved."


def read_journal(query: str = "") -> str:
    """Return recent journal entries, optionally filtered by keyword."""
    if not _JOURNAL_FILE.exists():
        return "No journal entries yet."
    content = _JOURNAL_FILE.read_text(encoding="utf-8").strip()
    if not content:
        return "Journal is empty."
    # Split on entry headers
    raw_entries = [e.strip() for e in content.split("\n\n") if e.strip()]
    if query:
        raw_entries = [e for e in raw_entries if query.lower() in e.lower()]
    recent = raw_entries[-5:]  # last 5
    if not recent:
        return f"No journal entries matching '{query}'."
    return "\n\n".join(recent)
