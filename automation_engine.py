"""
automation_engine.py — Background job scheduler for AI Companion.

Runs registered jobs (email sweep, downloads organizer, daily brief) while
the user is idle, respecting a daily LLM-call budget so background work never
eats the API quota needed for foreground chat.

Usage (from the app):
    engine = AutomationEngine(notify_fn)        # notify_fn(title, text)
    asyncio.create_task(engine.run())           # long-running task

All toggles are read live from settings.py on every tick:
    automations.enabled
    automations.idle_threshold_s
    automations.daily_llm_budget
    automations.email_sweep / email_sweep_interval_min
    automations.downloads_organizer / downloads_organizer_dry_run
    automations.daily_brief / daily_brief_time ("HH:MM")

State (LLM budget counter + per-job last-run timestamps) is persisted to
DATA_DIR/automation_state.json so restarts don't re-run jobs immediately.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import config
import settings

log = logging.getLogger(__name__)

STATE_FILE = config.DATA_DIR / "automation_state.json"

# How long the downloads organizer waits between runs (no user setting for it)
_DOWNLOADS_ORGANIZER_INTERVAL_S = 6 * 3600

_state_lock = threading.Lock()


# ─── Persistent state (budget counter + last-run timestamps) ─────────────────

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("automation_state.json unreadable (%s) — starting fresh", exc)
    return {}


def _save_state(state: dict) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as exc:
        log.error("Couldn't save automation state: %s", exc)


def budget_spend(n: int = 1) -> bool:
    """
    Reserve *n* background LLM calls from today's budget.

    Returns False when the daily budget (automations.daily_llm_budget) would
    be exceeded — callers must then SKIP the LLM call and log it. The counter
    resets automatically at midnight (date-keyed).
    """
    today = datetime.date.today().isoformat()
    with _state_lock:
        state = _load_state()
        bucket = state.get("llm_budget", {})
        if bucket.get("date") != today:
            bucket = {"date": today, "count": 0}
        try:
            budget = int(settings.get("automations.daily_llm_budget", 150))
        except (TypeError, ValueError):
            budget = 150
        if bucket.get("count", 0) + n > budget:
            return False
        bucket["count"] = bucket.get("count", 0) + n
        state["llm_budget"] = bucket
        _save_state(state)
        return True


def budget_remaining() -> int:
    """How many background LLM calls are left today (for status displays)."""
    today = datetime.date.today().isoformat()
    with _state_lock:
        state = _load_state()
        bucket = state.get("llm_budget", {})
        used = bucket.get("count", 0) if bucket.get("date") == today else 0
        try:
            budget = int(settings.get("automations.daily_llm_budget", 150))
        except (TypeError, ValueError):
            budget = 150
        return max(0, budget - used)


def _get_last_run(job_name: str) -> float:
    with _state_lock:
        return float(_load_state().get("last_run", {}).get(job_name, 0.0))


def _set_last_run(job_name: str, ts: float) -> None:
    with _state_lock:
        state = _load_state()
        state.setdefault("last_run", {})[job_name] = ts
        _save_state(state)


# ─── Idle detection (Windows) ────────────────────────────────────────────────

def user_idle_seconds() -> float:
    """Seconds since last keyboard/mouse input. Assumes idle if undetectable."""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint),
                        ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return max(0.0, millis / 1000.0)
    except Exception as exc:
        log.debug("Idle detection unavailable (%s) — assuming idle", exc)
    return float("inf")  # can't tell → don't block automations


# ─── Job registry ────────────────────────────────────────────────────────────

@dataclass
class Job:
    """One scheduled background job.

    Exactly one of *interval_s* / *daily_time* should be set:
      interval_s  — callable returning the run interval in seconds
      daily_time  — callable returning "HH:MM"; job runs once per day at/after it
    """
    name: str
    title: str                                   # card title for the GUI feed
    enabled: Callable[[], bool]
    run: Callable[[], Awaitable[str]]            # returns report ("" = silent skip)
    interval_s: Optional[Callable[[], float]] = None
    daily_time: Optional[Callable[[], str]] = None


# ─── Built-in job runners ────────────────────────────────────────────────────

async def _job_email_sweep() -> str:
    if not budget_spend():
        log.info("email_sweep skipped — daily background LLM budget exhausted")
        return ""
    from actions.email_assistant import sweep_inbox
    return await sweep_inbox()


async def _job_downloads_organizer() -> str:
    from actions.file_organizer import organize_downloads
    dry = bool(settings.get("automations.downloads_organizer_dry_run", True))
    return await organize_downloads("dry" if dry else "apply")


async def _job_daily_brief() -> str:
    from actions.daily_brief import daily_brief
    try:
        from actions.reminders import reminder_manager
        return await daily_brief(reminder_manager.list_reminders)
    except Exception:
        return await daily_brief()


def _builtin_jobs() -> list[Job]:
    return [
        Job(
            name="email_sweep",
            title="Inbox sweep",
            enabled=lambda: bool(settings.get("automations.email_sweep", False)),
            run=_job_email_sweep,
            interval_s=lambda: max(5.0, float(
                settings.get("automations.email_sweep_interval_min", 30) or 30
            )) * 60.0,
        ),
        Job(
            name="downloads_organizer",
            title="Downloads organizer",
            enabled=lambda: bool(settings.get("automations.downloads_organizer", False)),
            run=_job_downloads_organizer,
            interval_s=lambda: float(_DOWNLOADS_ORGANIZER_INTERVAL_S),
        ),
        Job(
            name="daily_brief",
            title="Daily brief",
            enabled=lambda: bool(settings.get("automations.daily_brief", False)),
            run=_job_daily_brief,
            daily_time=lambda: str(
                settings.get("automations.daily_brief_time", "08:30") or "08:30"
            ),
        ),
    ]


def _format_delta(seconds: float) -> str:
    """Compact human label for a future timestamp delta."""
    if seconds <= 0:
        return "due now"
    if seconds < 60:
        return f"in {int(seconds)}s"
    if seconds < 3600:
        return f"in {math.ceil(seconds / 60)}m"
    if seconds < 86400:
        return f"in {math.ceil(seconds / 3600)}h"
    return f"in {math.ceil(seconds / 86400)}d"


def _format_last_run(ts: float) -> str:
    if not ts:
        return "never"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%b %d %H:%M")
    except Exception:
        return "unknown"


def _daily_due_datetime(job: Job) -> datetime.datetime:
    try:
        hh, mm = (int(p) for p in job.daily_time().strip().split(":"))  # type: ignore[union-attr]
    except Exception:
        hh, mm = 8, 30
    now_dt = datetime.datetime.now()
    return now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _next_run_labels(job: Job, now: float, due: bool) -> tuple[str, str]:
    """Return (long_label, short_label) for UI display."""
    if due:
        return "due now", "now"

    if job.daily_time is not None:
        target = _daily_due_datetime(job)
        now_dt = datetime.datetime.now()
        last = _get_last_run(job.name)
        last_date = datetime.date.fromtimestamp(last) if last else None
        if now_dt >= target and last_date == now_dt.date():
            target = target + datetime.timedelta(days=1)
        return (
            f"{_format_delta((target - now_dt).total_seconds())} ({target.strftime('%H:%M')})",
            _format_delta((target - now_dt).total_seconds()),
        )

    if job.interval_s is not None:
        try:
            interval = float(job.interval_s())
        except Exception:
            return "unknown", "unknown"
        last = _get_last_run(job.name)
        next_ts = (last + interval) if last else now
        return _format_delta(next_ts - now), _format_delta(next_ts - now)

    return "manual", "manual"


def status_snapshot() -> dict:
    """
    Read-only scheduler snapshot for the GUI dashboard.

    This does not run jobs or mutate state; it only mirrors settings, budget,
    idle gate, last-run timestamps, and due calculations.
    """
    try:
        idle_threshold = float(settings.get("automations.idle_threshold_s", 180))
    except (TypeError, ValueError):
        idle_threshold = 180.0
    idle = user_idle_seconds()
    if idle == float("inf"):
        idle = 10**9

    try:
        daily_budget = int(settings.get("automations.daily_llm_budget", 150))
    except (TypeError, ValueError):
        daily_budget = 150

    now = time.time()
    jobs = []
    for job in _builtin_jobs():
        try:
            enabled = bool(job.enabled())
        except Exception:
            enabled = False
        try:
            due = enabled and AutomationEngine._is_due(job, now)
        except Exception:
            due = False
        next_label, next_short = ("disabled", "disabled")
        if enabled:
            next_label, next_short = _next_run_labels(job, now, due)
        last = _get_last_run(job.name)
        jobs.append({
            "name": job.name,
            "title": job.title,
            "enabled": enabled,
            "due": due,
            "last_run": last,
            "last_run_label": _format_last_run(last),
            "next_run_label": next_label,
            "next_run_short": next_short,
        })

    return {
        "enabled": bool(settings.get("automations.enabled", True)),
        "idle_seconds": int(max(0, idle)),
        "idle_threshold_s": int(max(0, idle_threshold)),
        "budget_remaining": budget_remaining(),
        "daily_llm_budget": daily_budget,
        "jobs": jobs,
    }


# ─── Engine ──────────────────────────────────────────────────────────────────

class AutomationEngine:
    """
    Background scheduler. Construct with a notify callback and run forever:

        engine = AutomationEngine(notify_fn)   # notify_fn(title: str, text: str)
        asyncio.create_task(engine.run())
    """

    SLEEP_GRANULARITY_S = 60.0

    def __init__(self, notify_fn: Callable[[str, str], None]) -> None:
        self._notify = notify_fn
        self._jobs: list[Job] = _builtin_jobs()
        self._running = False

    def register_job(self, job: Job) -> None:
        """Add a custom job to the registry."""
        self._jobs.append(job)

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Long-running asyncio task. Errors in jobs never kill the loop."""
        self._running = True
        log.info("AutomationEngine started (%d jobs registered)", len(self._jobs))
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("AutomationEngine tick error: %s", exc, exc_info=True)
            await asyncio.sleep(self.SLEEP_GRANULARITY_S)

    # ─── Internals ───────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        if not settings.get("automations.enabled", True):
            return

        try:
            idle_threshold = float(settings.get("automations.idle_threshold_s", 180))
        except (TypeError, ValueError):
            idle_threshold = 180.0
        idle = user_idle_seconds()
        if idle < idle_threshold:
            log.debug("AutomationEngine: user active (idle %.0fs < %.0fs) — waiting",
                      idle, idle_threshold)
            return

        now = time.time()
        for job in self._jobs:
            try:
                if not job.enabled():
                    continue
                if not self._is_due(job, now):
                    continue
                await self._run_job(job)
            except Exception as exc:
                log.error("Automation job '%s' crashed: %s", job.name, exc,
                          exc_info=True)
                _set_last_run(job.name, time.time())  # don't retry-spin a broken job

    @staticmethod
    def _is_due(job: Job, now: float) -> bool:
        last = _get_last_run(job.name)

        if job.daily_time is not None:
            # Once per day, at/after the configured local time.
            try:
                hh, mm = (int(p) for p in job.daily_time().strip().split(":"))
            except Exception:
                hh, mm = 8, 30
            local_now = datetime.datetime.now()
            due_today = local_now.replace(hour=hh, minute=mm,
                                          second=0, microsecond=0)
            if local_now < due_today:
                return False
            last_date = datetime.date.fromtimestamp(last) if last else None
            return last_date != local_now.date()

        if job.interval_s is not None:
            try:
                interval = float(job.interval_s())
            except Exception:
                return False
            return (now - last) >= interval

        return False

    async def _run_job(self, job: Job) -> None:
        log.info("AutomationEngine: running job '%s'", job.name)
        started = time.time()
        try:
            report = await job.run()
        finally:
            _set_last_run(job.name, started)
        if not report:
            return  # silent skip (e.g. budget exhausted)
        log.info("AutomationEngine: job '%s' done (%.1fs)",
                 job.name, time.time() - started)
        try:
            self._notify(job.title, report)
        except Exception as exc:
            log.error("AutomationEngine: notify failed for '%s': %s", job.name, exc)
