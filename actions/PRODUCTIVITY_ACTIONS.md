# Productivity Actions — wiring reference

Every entry point is `async def fn(param: str) -> str` and returns a short
user-facing string (never raises). Wire these into `action_router.py` as
`action_name → module.function`.

| Action name | Function | Param format | Description |
|---|---|---|---|
| `sweep_inbox` | `actions.email_assistant.sweep_inbox` | (ignored) | Read-only IMAP triage: fetches unread mail, LLM-classifies into Important / Action needed / Personal / Newsletter / Advertisement / Spam, returns grouped report. |
| `email_summary` | `actions.email_assistant.email_summary` | (ignored) | Short inbox digest: counts per category + top 3 important subjects. |
| `organize_downloads` | `actions.file_organizer.organize_downloads` | `"dry"` \| `"apply"` \| empty (uses `automations.downloads_organizer_dry_run` setting) | Sorts ~/Downloads into Documents/Images/Videos/Audio/Archives/Installers/Code/Other; dry-run previews, apply moves with collision-safe renames. |
| `cleanup_temp` | `actions.file_organizer.cleanup_temp` | (ignored) | Read-only report of temp folder + Recycle Bin size with a cleanup suggestion. Never deletes. |
| `summarize_document` | `actions.documents.summarize_document` | file path (quotes OK) — `.pdf` / `.docx` / `.txt` / `.md` | Extracts up to ~8000 chars and returns LLM summary + key points + action items. |
| `clipboard_ai` | `actions.clipboard_ai.clipboard_ai` | `summarize` \| `translate to <lang>` \| `fix` \| `reply` \| `explain` \| free-form instruction | Applies the instruction to current clipboard text; result is copied back to the clipboard and returned. |
| `todo_add` | `actions.todo.todo_add` | task text | Adds an item to the persistent todo list (`data/todos.json`). |
| `todo_list` | `actions.todo.todo_list` | (ignored) | Numbered open items, then up to 5 recently completed. |
| `todo_done` | `actions.todo.todo_done` | item number from `todo_list` OR fuzzy text match | Marks a todo as done. |
| `todo_clear` | `actions.todo.todo_clear` | (ignored) | Removes COMPLETED items only; open tasks are kept. |
| `meeting_start` | `actions.meeting_notes.meeting_start` | (ignored) | Starts mic recording (16 kHz mono WAV → `data/meetings/`). Guards against double-start. |
| `meeting_stop` | `actions.meeting_notes.meeting_stop` | (ignored) | Stops recording, transcribes (faster-whisper), LLM minutes (summary/decisions/action items), saves `<timestamp>.md`, returns minutes. |
| `write_draft` | `actions.drafts.write_draft` | description of what to write (English or Mongolian, incl. Latin-script Mongolian) | Drafts an email/letter/message; copies it to the clipboard and returns it. |
| `weekly_report` | `actions.weekly_report.weekly_report` | (ignored) | Done / In progress / Next week report from the last 7 days of journal, notes, completed todos, and meeting minutes; copied to clipboard. |
| `ocr_screen` | `actions.ocr_screen.ocr_screen` | `region` (centre half) or empty (full screen) | Vision-LLM OCR of the screen; full text → clipboard, display truncated to ~1500 chars. |

## Background automation (not router actions)

`automation_engine.py` (project root) — start from the app:

```python
from automation_engine import AutomationEngine
engine = AutomationEngine(notify_fn)   # notify_fn(title: str, text: str)
asyncio.create_task(engine.run())
```

Built-in jobs (all idle-gated via `automations.idle_threshold_s`, toggled live
from settings):

| Job | Schedule | Calls |
|---|---|---|
| `email_sweep` | every `automations.email_sweep_interval_min` min | `actions.email_assistant.sweep_inbox` (consumes 1 LLM budget unit) |
| `downloads_organizer` | every 6 h (fixed) | `actions.file_organizer.organize_downloads` (dry-run per setting) |
| `daily_brief` | daily at `automations.daily_brief_time` | `actions.daily_brief.daily_brief` |

`automation_engine.budget_spend(n=1) -> bool` — background jobs that need the
LLM must call this first and skip (with a log line) when it returns `False`.
State (budget counter + per-job last-run) persists in
`data/automation_state.json`.
