"""
actions/todo.py — Persistent todo list stored at DATA_DIR/todos.json.

No LLM involved — pure local CRUD.

Usage (via LLM actions):
    todo_add | buy milk
    todo_list |
    todo_done | 2              ← number from todo_list
    todo_done | milk           ← fuzzy text match also works
    todo_clear |               ← removes COMPLETED items only
"""

from __future__ import annotations

import datetime
import json
import logging
import threading

import config

log = logging.getLogger(__name__)

_TODO_FILE = config.DATA_DIR / "todos.json"
_lock = threading.Lock()

_RECENT_DONE_SHOWN = 5


def _load() -> list[dict]:
    try:
        if _TODO_FILE.exists():
            data = json.loads(_TODO_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception as exc:
        log.warning("todos.json unreadable (%s) — starting fresh", exc)
    return []


def _save(items: list[dict]) -> None:
    tmp = _TODO_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(_TODO_FILE)


def _open_items(items: list[dict]) -> list[dict]:
    return [t for t in items if not t.get("done")]


# ─── Public actions ──────────────────────────────────────────────────────────

async def todo_add(param: str) -> str:
    """Add a todo item. param = the task text."""
    try:
        text = param.strip()
        if not text:
            return "What's the task? Try: todo_add | finish lab report"
        with _lock:
            items = _load()
            items.append({
                "text": text,
                "created": datetime.datetime.now().isoformat(timespec="seconds"),
                "done": False,
                "done_date": None,
            })
            _save(items)
            open_count = len(_open_items(items))
        return f"Added: {text} ({open_count} open task" \
               f"{'s' if open_count != 1 else ''})"
    except Exception as exc:
        log.error("todo_add failed: %s", exc, exc_info=True)
        return f"Couldn't save that todo: {exc}"


async def todo_list(param: str = "") -> str:
    """List open todos (numbered) followed by recently completed ones."""
    try:
        with _lock:
            items = _load()
        open_items = _open_items(items)
        done_items = [t for t in items if t.get("done")]
        done_items.sort(key=lambda t: t.get("done_date") or "", reverse=True)

        if not open_items and not done_items:
            return "Todo list is empty. Add one with: todo_add | something"

        lines: list[str] = []
        if open_items:
            lines.append(f"Open ({len(open_items)}):")
            for i, t in enumerate(open_items, 1):
                created = (t.get("created") or "")[:10]
                lines.append(f"  {i}. {t['text']}"
                             + (f"  (added {created})" if created else ""))
        else:
            lines.append("No open tasks — all clear!")

        if done_items:
            lines.append(f"Recently done ({min(len(done_items), _RECENT_DONE_SHOWN)}"
                         f" of {len(done_items)}):")
            for t in done_items[:_RECENT_DONE_SHOWN]:
                done_date = (t.get("done_date") or "")[:10]
                lines.append(f"  ✓ {t['text']}"
                             + (f"  ({done_date})" if done_date else ""))
        return "\n".join(lines)
    except Exception as exc:
        log.error("todo_list failed: %s", exc, exc_info=True)
        return f"Couldn't read the todo list: {exc}"


async def todo_done(param: str) -> str:
    """Mark a todo done. param = its number in todo_list, or fuzzy text."""
    try:
        query = param.strip()
        if not query:
            return "Which one? Give me its number from todo_list, or part of its text."
        with _lock:
            items = _load()
            open_items = _open_items(items)
            if not open_items:
                return "No open tasks to complete."

            target: dict | None = None
            if query.lstrip("#").isdigit():
                idx = int(query.lstrip("#")) - 1
                if 0 <= idx < len(open_items):
                    target = open_items[idx]
                else:
                    return (f"There's no open task #{query} — "
                            f"only {len(open_items)} open. Check todo_list.")
            else:
                q = query.lower()
                matches = [t for t in open_items if q in t["text"].lower()]
                if not matches:
                    # Looser fuzzy: every word of the query appears somewhere
                    words = q.split()
                    matches = [t for t in open_items
                               if all(w in t["text"].lower() for w in words)]
                if not matches:
                    return f"No open task matching '{query}'."
                if len(matches) > 1:
                    opts = "; ".join(t["text"] for t in matches[:4])
                    return (f"That matches {len(matches)} tasks ({opts}) — "
                            "be more specific or use the number.")
                target = matches[0]

            target["done"] = True
            target["done_date"] = datetime.datetime.now().isoformat(
                timespec="seconds")
            _save(items)
            remaining = len(_open_items(items))
        return f"Done: {target['text']} ({remaining} left)"
    except Exception as exc:
        log.error("todo_done failed: %s", exc, exc_info=True)
        return f"Couldn't update the todo list: {exc}"


async def todo_clear(param: str = "") -> str:
    """Remove COMPLETED items from the list (open tasks are kept)."""
    try:
        with _lock:
            items = _load()
            done_count = sum(1 for t in items if t.get("done"))
            if done_count == 0:
                return "Nothing to clear — no completed items."
            items = _open_items(items)
            _save(items)
        return (f"Cleared {done_count} completed item"
                f"{'s' if done_count != 1 else ''}. "
                f"{len(items)} open task{'s' if len(items) != 1 else ''} kept.")
    except Exception as exc:
        log.error("todo_clear failed: %s", exc, exc_info=True)
        return f"Couldn't clear completed todos: {exc}"
