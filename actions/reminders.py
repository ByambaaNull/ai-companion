"""
actions/reminders.py — Lightweight reminder / alarm system.

Usage examples (via LLM actions):
    remind | in 30 minutes: submit the assignment
    remind | 2h: call mom
    reminders          ← list active reminders
    cancel_reminder | 0
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable, Awaitable, Optional

log = logging.getLogger(__name__)

_TIME_RE = re.compile(
    r"^(?:in\s+)?(\d+(?:\.\d+)?)\s*"
    r"(seconds|second|secs|sec|minutes|minute|mins|min|hours|hour|hrs|hr|[smh])\s*"
    r"[:\-,]?\s*(.*)?$",
    re.IGNORECASE,
)


def _parse_time(text: str):
    """Parse 'in 30 minutes: message' → (seconds, message). Returns (None, text) on failure."""
    m = _TIME_RE.match(text.strip())
    if not m:
        return None, text.strip()
    amount = float(m.group(1))
    unit   = m.group(2).lower()
    msg    = (m.group(3) or "").strip() or "reminder"
    if unit in ("s", "sec", "secs", "second", "seconds"):
        seconds = amount
    elif unit in ("m", "min", "mins", "minute", "minutes"):
        seconds = amount * 60
    else:
        seconds = amount * 3600
    return seconds, msg


def _fmt(seconds: float) -> str:
    if seconds < 60:
        s = int(seconds)
        return f"{s} second{'s' if s != 1 else ''}"
    elif seconds < 3600:
        m = int(seconds // 60)
        return f"{m} minute{'s' if m != 1 else ''}"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m" if m else f"{h} hour{'s' if h != 1 else ''}"


class ReminderManager:
    def __init__(self):
        self._speak:    Optional[Callable[[str], Awaitable[None]]] = None
        self._notify:   Optional[Callable[[str], None]]            = None
        self._tasks:    dict[str, asyncio.Task]                    = {}
        self._messages: dict[str, str]                             = {}
        self._counter:  int                                        = 0

    def set_callbacks(
        self,
        speak_fn:  Callable[[str], Awaitable[None]],
        notify_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._speak  = speak_fn
        self._notify = notify_fn

    async def set_reminder(self, text: str) -> str:
        seconds, message = _parse_time(text)
        if seconds is None:
            return (
                f"Couldn't parse the time from '{text}'. "
                "Try: 'in 30 minutes: submit assignment' or '2h: call mom'"
            )
        rid = str(self._counter)
        self._counter += 1
        self._messages[rid] = message
        self._tasks[rid] = asyncio.create_task(self._fire(seconds, message, rid))
        return f"Set — I'll remind you in {_fmt(seconds)}: {message}"

    def list_reminders(self) -> str:
        active = {k: self._messages[k] for k, t in self._tasks.items()
                  if not t.done() and k in self._messages}
        if not active:
            return "No active reminders."
        lines = "\n".join(f"  [{rid}] {msg}" for rid, msg in active.items())
        return f"Active reminders:\n{lines}"

    def cancel_reminder(self, param: str) -> str:
        rid = param.strip()
        if rid in self._tasks and not self._tasks[rid].done():
            self._tasks[rid].cancel()
            msg = self._messages.pop(rid, "?")
            del self._tasks[rid]
            return f"Cancelled reminder #{rid}: {msg}"
        return f"No active reminder with id '{rid}'."

    async def _fire(self, seconds: float, message: str, rid: str) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        self._messages.pop(rid, None)
        self._tasks.pop(rid, None)
        alert = f"Hey, reminder: {message}"
        log.info("Reminder fired: %s", message)
        if self._notify:
            try:
                self._notify(alert)
            except Exception as exc:
                log.error("Reminder notify failed: %s", exc)
        if self._speak:
            try:
                await self._speak(alert)
            except Exception as exc:
                log.error("Reminder speak failed: %s", exc)


# Global singleton — initialised once, callbacks set in assistant_gui.py
reminder_manager = ReminderManager()
