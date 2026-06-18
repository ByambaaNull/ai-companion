"""
actions/daily_brief.py — Morning / on-demand daily briefing.

Builds a structured text block (date, time, weather, reminders) and
returns it so the caller can feed it back through the LLM for an
in-character delivery.

Usage:
    ACTION: daily_brief |
"""

from __future__ import annotations

import datetime
import logging

log = logging.getLogger(__name__)


async def daily_brief(list_reminders_fn=None) -> str:
    """
    Compile a daily briefing.

    Args:
        list_reminders_fn: Optional callable that returns active reminders as a string.

    Returns:
        A structured text block for LLM summarisation.
    """
    now      = datetime.datetime.now()
    day_str  = now.strftime("%A, %B %d").replace(" 0", " ")  # e.g. "Monday, May 5"
    time_str = now.strftime("%I:%M %p").lstrip("0")  # e.g. "9:30 AM"

    lines = [
        f"[Daily brief — {day_str}, {time_str}]",
    ]

    # Weather
    try:
        from actions.system_info import get_weather
        weather = await get_weather("")
        lines.append(weather)
    except Exception as exc:
        log.debug("Weather unavailable for brief: %s", exc)
        lines.append("Weather: unavailable right now.")

    # Active reminders
    try:
        if list_reminders_fn is not None:
            reminder_text = list_reminders_fn()
            if reminder_text and "No active" not in reminder_text:
                lines.append(reminder_text)
            else:
                lines.append("No active reminders.")
        else:
            lines.append("No active reminders.")
    except Exception:
        pass

    return "\n".join(lines)
