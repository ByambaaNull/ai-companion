"""
audio_devices.py — enumerate / resolve audio OUTPUT devices for the speaker chooser.

Two consumers route through here:
  - TTS playback (tts_rvc) uses sounddevice → wants a device *index*.
  - Music (mpv) wants an mpv `--audio-device=` *id*, which we best-effort map by
    matching the saved device name against `mpv --audio-device=help`.

Everything degrades to None (= system default) and never raises.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def list_output_devices() -> list[dict]:
    """All output-capable devices: [{index, name, hostapi, default}]. [] if none."""
    try:
        import sounddevice as sd
    except Exception as exc:
        log.warning("sounddevice unavailable — no speaker list: %s", exc)
        return []
    devices: list[dict] = []
    try:
        try:
            default_out = sd.default.device[1]
        except Exception:
            default_out = -1
        hostapis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) > 0:
                hi = d.get("hostapi")
                ha = hostapis[hi]["name"] if hi is not None and hi < len(hostapis) else ""
                devices.append({
                    "index": i,
                    "name": d.get("name", f"Device {i}"),
                    "hostapi": ha,
                    "default": (i == default_out),
                })
    except Exception as exc:
        log.warning("query output devices failed: %s", exc)
    return devices


def resolve_output_index(name: str) -> int | None:
    """sounddevice output index for a saved device name (prefer WASAPI), or None."""
    if not name:
        return None
    try:
        import sounddevice as sd
        hostapis = sd.query_hostapis()
        exact: list[tuple[int, str]] = []
        loose: list[tuple[int, str]] = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) <= 0:
                continue
            dname = d.get("name", "")
            hi = d.get("hostapi")
            ha = (hostapis[hi]["name"].lower() if hi is not None and hi < len(hostapis) else "")
            if dname == name:
                exact.append((i, ha))
            elif name.lower() in dname.lower():
                loose.append((i, ha))
        for pool in (exact, loose):
            for i, ha in pool:
                if "wasapi" in ha:
                    return i
            if pool:
                return pool[0][0]
    except Exception as exc:
        log.debug("resolve_output_index failed: %s", exc)
    return None


def mpv_audio_device(name: str) -> str | None:
    """Best-effort mpv `--audio-device` id for a saved device name, or None."""
    if not name:
        return None
    try:
        from config import MPV_EXECUTABLE
        if not MPV_EXECUTABLE:
            return None
        res = subprocess.run(
            [MPV_EXECUTABLE, "--audio-device=help"],
            capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW,
        )
        best: str | None = None
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line.startswith("'"):
                continue
            try:
                end = line.index("'", 1)
            except ValueError:
                continue
            dev_id = line[1:end]
            desc = line[end + 1:]
            if name.lower() in desc.lower() or name.lower() in dev_id.lower():
                if "wasapi" in dev_id.lower():
                    return dev_id          # prefer WASAPI exactly as mpv names it
                best = best or dev_id
        return best
    except Exception as exc:
        log.debug("mpv_audio_device failed: %s", exc)
        return None
