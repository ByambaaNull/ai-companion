"""
actions/weekly_report.py — Compile a professional weekly status report.

Sources (all read defensively — any may be missing):
    data/journal.txt   — timestamped journal entries (see actions/system_info.py)
    data/notes.txt     — timestamped one-line notes
    data/todos.json    — items completed in the last 7 days
    data/meetings/*.md — meeting minutes saved this week

One LLM call turns the raw material into Done / In progress / Next week.
The report is also copied to the clipboard.

Usage (via LLM actions):
    weekly_report |
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from pathlib import Path

import config

log = logging.getLogger(__name__)

_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\]")

_REPORT_SYSTEM = (
    "You write concise, professional weekly status reports. From the raw "
    "activity log, produce:\n"
    "## Done\nBullet list of completed work.\n"
    "## In progress\nBullet list of ongoing items.\n"
    "## Next week\nBullet list of planned/likely next steps.\n"
    "Group related items, drop trivia, keep each bullet under one line. "
    "Base everything strictly on the provided material — never invent work."
)


def _within_week(date_str: str, cutoff: datetime.datetime) -> bool:
    """True if an ISO-ish date string falls within the last 7 days."""
    try:
        dt = datetime.datetime.fromisoformat(date_str[:19])
        return dt >= cutoff
    except (ValueError, TypeError):
        return False


def _gather(cutoff: datetime.datetime) -> dict[str, list[str]]:
    """Blocking collection of the week's raw material (runs in executor)."""
    out: dict[str, list[str]] = {"journal": [], "notes": [],
                                 "todos_done": [], "todos_open": [],
                                 "meetings": []}

    # Journal — data/journal.txt: "[YYYY-MM-DD HH:MM]\n<entry text>"
    journal_file = config.DATA_DIR / "journal.txt"
    try:
        if journal_file.exists():
            content = journal_file.read_text(encoding="utf-8")
            for block in content.split("\n\n"):
                block = block.strip()
                if not block:
                    continue
                m = _TS_RE.search(block)
                if m and _within_week(f"{m.group(1)}T{m.group(2)}", cutoff):
                    out["journal"].append(block)
    except Exception as exc:
        log.warning("weekly_report: journal read failed: %s", exc)

    # Notes — data/notes.txt: "[YYYY-MM-DD HH:MM] note text" per line
    notes_file = config.DATA_DIR / "notes.txt"
    try:
        if notes_file.exists():
            for line in notes_file.read_text(encoding="utf-8").splitlines():
                m = _TS_RE.search(line)
                if m and _within_week(f"{m.group(1)}T{m.group(2)}", cutoff):
                    out["notes"].append(line.strip())
    except Exception as exc:
        log.warning("weekly_report: notes read failed: %s", exc)

    # Defensive: pick up journal.json / notes.json if some version wrote them
    for name, key in (("journal.json", "journal"), ("notes.json", "notes")):
        f = config.DATA_DIR / name
        try:
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                entries = data if isinstance(data, list) else \
                    data.get("entries", []) if isinstance(data, dict) else []
                for e in entries:
                    if isinstance(e, dict):
                        when = str(e.get("date") or e.get("created")
                                   or e.get("timestamp") or "")
                        text = str(e.get("text") or e.get("entry") or "")
                        if text and _within_week(when, cutoff):
                            out[key].append(f"[{when[:16]}] {text}")
                    elif isinstance(e, str) and e.strip():
                        out[key].append(e.strip())
        except Exception as exc:
            log.debug("weekly_report: %s skipped: %s", name, exc)

    # Todos — data/todos.json (see actions/todo.py)
    todos_file = config.DATA_DIR / "todos.json"
    try:
        if todos_file.exists():
            todos = json.loads(todos_file.read_text(encoding="utf-8"))
            if isinstance(todos, list):
                for t in todos:
                    if not isinstance(t, dict):
                        continue
                    if t.get("done") and _within_week(
                            str(t.get("done_date") or ""), cutoff):
                        out["todos_done"].append(str(t.get("text", "")))
                    elif not t.get("done"):
                        out["todos_open"].append(str(t.get("text", "")))
    except Exception as exc:
        log.warning("weekly_report: todos read failed: %s", exc)

    # Meeting minutes — data/meetings/*.md modified this week
    meetings_dir = config.DATA_DIR / "meetings"
    try:
        if meetings_dir.exists():
            for f in sorted(meetings_dir.glob("*.md")):
                try:
                    mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime >= cutoff:
                        out["meetings"].append(f.stem)
                except OSError:
                    pass
    except Exception as exc:
        log.warning("weekly_report: meetings scan failed: %s", exc)

    return out


def _format_material(material: dict[str, list[str]]) -> str:
    sections: list[str] = []

    def _add(title: str, items: list[str], cap: int = 30) -> None:
        if items:
            body = "\n".join(f"- {i}" for i in items[:cap])
            sections.append(f"{title}:\n{body}")

    _add("Journal entries this week", material["journal"], cap=15)
    _add("Notes this week", material["notes"])
    _add("Todos completed this week", material["todos_done"])
    _add("Todos still open", material["todos_open"], cap=15)
    _add("Meetings held this week (minutes on file)", material["meetings"])
    return "\n\n".join(sections)


async def weekly_report(param: str = "") -> str:
    """Build a Done / In progress / Next week report from the last 7 days."""
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=7)
        loop = asyncio.get_running_loop()
        material = await loop.run_in_executor(None, _gather, cutoff)

        raw = _format_material(material)
        if not raw.strip():
            return ("I couldn't find any activity from the last 7 days "
                    "(no journal entries, notes, completed todos, or meeting "
                    "minutes). Log some work first, then ask again.")

        week_start = cutoff.strftime("%b %d")
        today = datetime.datetime.now().strftime("%b %d, %Y")

        from main import get_llm_response  # lazy — avoids circular import
        report = await get_llm_response(
            f"Raw activity log for the week {week_start} – {today}:\n\n{raw}",
            _REPORT_SYSTEM,
        )
        if not report:
            return "Report generation came back empty — try again in a moment."

        header = f"Weekly report ({week_start} – {today})\n\n"
        full = header + report

        clip_note = ""
        try:
            import pyperclip
            pyperclip.copy(full)
            clip_note = "\n\n(Copied to clipboard.)"
        except Exception as exc:
            log.warning("weekly_report: clipboard copy failed: %s", exc)

        return full + clip_note
    except Exception as exc:
        log.error("weekly_report failed: %s", exc, exc_info=True)
        return f"Couldn't build the weekly report: {exc}"
