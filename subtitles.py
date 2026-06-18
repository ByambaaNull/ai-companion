"""
subtitles.py — convert subtitle files to WebVTT for the in-app HTML5 player.

The <video> element's <track> only understands WebVTT (.vtt). SRT is the common
format for movie subtitles, so convert it on the fly. The only real differences:
VTT starts with a "WEBVTT" header and uses '.' (not ',') for the millisecond
separator in cue timestamps.
"""

from __future__ import annotations

from pathlib import Path


def to_vtt(src: str | Path, dest_dir: str | Path) -> Path:
    """Write a .vtt next to the served videos and return its path.

    Accepts .srt or .vtt input. Already-VTT input is passed through (with a header
    added if missing). The output lives in *dest_dir* so the media server can
    serve it same-origin as the video (required for <track> to load).
    """
    src = Path(src)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8", errors="replace").replace("﻿", "")

    if text.lstrip().upper().startswith("WEBVTT"):
        body = text
    elif src.suffix.lower() == ".vtt":
        body = "WEBVTT\n\n" + text
    else:
        # SRT -> VTT: header + '.' millisecond separator on cue-timing lines.
        out = ["WEBVTT", ""]
        for line in text.splitlines():
            if "-->" in line:
                line = line.replace(",", ".")
            out.append(line)
        body = "\n".join(out)

    dest = dest_dir / (src.stem + ".vtt")
    dest.write_text(body, encoding="utf-8")
    return dest
