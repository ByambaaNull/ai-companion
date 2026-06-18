"""
local_intents.py - deterministic command routing before the LLM.

Clear desktop/productivity commands should not spend LLM tokens just so the
model can emit an ACTION line. This module recognizes conservative command
patterns and maps them straight to ActionRouter actions.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LocalIntent:
    """A local command that can be answered or executed without chat LLM use."""

    action: str | None = None
    param: str = ""
    speech: str = ""
    response: str | None = None


_URL_RE = re.compile(
    r"\b(?:https?://|www\.)\S+|\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/\S*)?",
    re.IGNORECASE,
)

_SITE_ALIASES = {
    "youtube": "youtube",
    "yt": "youtube",
    "google": "google",
    "gmail": "gmail",
    "github": "github",
    "reddit": "reddit",
    "twitter": "twitter",
    "x": "x",
    "discord": "discord",
    "spotify": "spotify",
    "twitch": "twitch",
    "messenger": "https://www.messenger.com",
    "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com",
    "chatgpt": "https://chatgpt.com",
}

_FOLDER_ALIASES = {
    "downloads": "downloads",
    "download folder": "downloads",
    "documents": "documents",
    "document folder": "documents",
    "desktop": "desktop",
    "projects": "projects",
    "github folder": "github",
    "home folder": "home",
}


def _norm(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    return text.strip(" \t\r\n.!?").lower()


def _clean_param(text: str) -> str:
    return text.strip().strip(" \t\r\n:,-")


def _after(pattern: str, text: str, flags: int = re.IGNORECASE) -> Optional[str]:
    m = re.match(pattern, text.strip(), flags)
    if not m:
        return None
    return _clean_param(m.group(1))


def _time_response(lower: str) -> LocalIntent | None:
    now = _dt.datetime.now()
    if (
        re.fullmatch(r"(what'?s )?(the )?(current )?time( now)?", lower)
        or lower in {"time", "what time is it", "what is the time"}
    ):
        return LocalIntent(response=now.strftime("It is %I:%M %p.").replace(" 0", " "))
    if re.fullmatch(r"(what'?s )?(the )?(date|day)( today)?", lower) or lower in {"date", "today"}:
        return LocalIntent(response=now.strftime("Today is %A, %B %d, %Y.").replace(" 0", " "))
    return None


def _todo_intent(text: str, lower: str) -> LocalIntent | None:
    if lower in {"todo", "todos", "show todos", "list todos", "todo list", "tasks", "show tasks"}:
        return LocalIntent(action="todos")

    if lower in {"clear todos", "todo clear", "clear completed todos"}:
        return LocalIntent(action="todo_clear")

    done = _after(r"(?:todo done|done todo|mark todo)\s+(.+?)(?:\s+done)?$", text)
    if done:
        return LocalIntent(action="todo_done", param=done)

    task = _after(r"(?:add (?:a )?todo|todo add|new todo|add task|task)\s*[:\-]?\s+(.+)$", text)
    if task:
        return LocalIntent(action="todo", param=task)

    # "todo buy milk" should add; "todo list/done/clear" was handled above.
    task = _after(r"todo\s+(.+)$", text)
    if task and not task.lower().startswith(("list", "done", "clear")):
        return LocalIntent(action="todo", param=task)
    return None


def _reminder_intent(text: str, lower: str) -> LocalIntent | None:
    if lower in {"reminders", "show reminders", "list reminders", "active reminders"}:
        return LocalIntent(action="reminders")

    rid = _after(r"(?:cancel reminder|delete reminder|remove reminder)\s+#?(.+)$", text)
    if rid:
        return LocalIntent(action="cancel_reminder", param=rid)

    # "remind me in 30 minutes to submit assignment" -> "in 30 minutes: submit assignment"
    m = re.match(r"remind(?: me)?\s+(.+?)\s+to\s+(.+)$", text.strip(), re.IGNORECASE)
    if m:
        return LocalIntent(action="remind", param=f"{_clean_param(m.group(1))}: {_clean_param(m.group(2))}")

    reminder = _after(r"remind(?: me)?\s+(.+)$", text)
    if reminder:
        return LocalIntent(action="remind", param=reminder)
    return None


def _note_intent(text: str, lower: str) -> LocalIntent | None:
    note = _after(r"(?:note|save note)\s*[:\-]?\s+(.+)$", text)
    if note:
        return LocalIntent(action="note", param=note)

    if lower in {"notes", "show notes", "read notes"}:
        return LocalIntent(action="notes")

    q = _after(r"(?:notes about|search notes|read notes about)\s+(.+)$", text)
    if q:
        return LocalIntent(action="notes", param=q)

    journal = _after(r"(?:journal|add journal|journal entry)\s*[:\-]?\s+(.+)$", text)
    if journal:
        return LocalIntent(action="journal", param=journal)

    if lower in {"read journal", "show journal", "journal entries"}:
        return LocalIntent(action="read_journal")
    return None


def _music_intent(text: str, lower: str) -> LocalIntent | None:
    controls = {
        "pause music": ("pause_music", ""),
        "pause": ("pause_music", ""),
        "resume music": ("resume_music", ""),
        "resume": ("resume_music", ""),
        "unpause": ("resume_music", ""),
        "stop music": ("stop_music", ""),
        "next song": ("next_music", ""),
        "next track": ("next_music", ""),
        "skip song": ("next_music", ""),
        "previous song": ("previous_music", ""),
        "previous track": ("previous_music", ""),
        "prev song": ("previous_music", ""),
    }
    if lower in controls:
        action, param = controls[lower]
        return LocalIntent(action=action, param=param)

    # playback modes
    if lower in {"shuffle", "shuffle on", "shuffle music", "shuffle mode"}:
        return LocalIntent(action="shuffle_music", param="on")
    if lower in {"shuffle off", "no shuffle", "turn off shuffle"}:
        return LocalIntent(action="shuffle_music", param="off")
    if lower == "toggle shuffle":
        return LocalIntent(action="shuffle_music", param="toggle")
    if lower in {"repeat", "repeat all", "loop", "loop all", "repeat queue"}:
        return LocalIntent(action="repeat_music", param="all")
    if lower in {"repeat one", "repeat song", "loop song", "repeat track"}:
        return LocalIntent(action="repeat_music", param="one")
    if lower in {"repeat off", "no repeat", "stop repeat", "turn off repeat"}:
        return LocalIntent(action="repeat_music", param="off")

    # collections — play a whole filter as a queue (offline, downloaded tracks)
    if lower in {"play all", "play all music", "play all songs",
                 "play everything", "play my music"}:
        return LocalIntent(action="play_all", speech="Playing your library.")
    if lower in {"play liked", "play liked songs", "play my liked songs", "play my liked"}:
        return LocalIntent(action="play_liked", speech="Playing your liked songs.")
    if lower in {"play favourites", "play favorites",
                 "play my favourites", "play my favorites"}:
        return LocalIntent(action="play_favourites", speech="Playing your favourites.")

    # sleep timer
    if lower in {"cancel sleep timer", "sleep timer off", "stop sleep timer"}:
        return LocalIntent(action="sleep_timer", param="off")
    m = re.match(
        r"(?:sleep timer|set sleep timer|stop music in|sleep in)\s+(\d{1,3})"
        r"\s*(?:min|mins|minute|minutes)?$",
        lower,
    )
    if m:
        return LocalIntent(action="sleep_timer", param=m.group(1))

    # import a local music folder (offline — no download)
    folder = _after(
        r"(?:import music(?: from)?|import folder|import songs(?: from)?|"
        r"scan music folder)\s+(.+)$",
        text,
    )
    if folder:
        return LocalIntent(action="import_music", param=folder)

    m = re.match(r"(?:set )?(?:music )?volume\s+(?:to\s+)?(\d{1,3})$", lower)
    if m:
        return LocalIntent(action="music_volume", param=m.group(1))

    if lower in {"my favourites", "my favorites", "show favourites", "show favorites"}:
        return LocalIntent(action="my_favourites")
    if lower in {"top played", "music stats"}:
        return LocalIntent(action="top_played", param="5")

    playlist = _after(r"(?:play playlist|playlist)\s+(.+)$", text)
    if playlist:
        return LocalIntent(action="play_playlist", param=playlist, speech=f"Playing playlist {playlist}.")

    song = _after(r"(?:play music|play song|play)\s+(.+)$", text)
    if song:
        return LocalIntent(action="play_music", param=song, speech=f"Playing {song}.")

    dl = _after(r"(?:download music|download song|download)\s+(.+)$", text)
    if dl:
        return LocalIntent(action="download_music", param=dl)
    return None


def _browser_intent(text: str, lower: str) -> LocalIntent | None:
    m = re.match(r"(?:open localhost|localhost)\s*:?\s*(\d{2,5})?$", lower)
    if m:
        return LocalIntent(action="open_localhost", param=m.group(1) or "3000", speech="Opening localhost.")

    for phrase, folder in _FOLDER_ALIASES.items():
        if lower in {f"open {phrase}", f"show {phrase}"}:
            return LocalIntent(action="open_folder", param=folder, speech=f"Opening {folder}.")

    url = _URL_RE.search(text)
    if url and re.match(r"^(?:open|go to|visit)\b", lower):
        target = url.group(0)
        return LocalIntent(action="open_browser", param=target, speech="Opening it.")

    site = _after(r"(?:open|go to|visit)\s+([a-z0-9 ._-]+)$", text)
    if site:
        key = site.lower().strip()
        if key in _SITE_ALIASES:
            return LocalIntent(action="open_browser", param=_SITE_ALIASES[key], speech=f"Opening {key}.")

    query = _after(r"(?:web search|search web for|search the web for|google)\s+(.+)$", text)
    if query:
        return LocalIntent(action="web_search", param=query)

    yt = _after(r"(?:play youtube|open youtube video|youtube)\s+(.+)$", text)
    if yt:
        return LocalIntent(action="play_yt", param=yt, speech="Opening it on YouTube.")
    return None


def _utility_intent(text: str, lower: str) -> LocalIntent | None:
    exact_actions = {
        "screenshot": "screenshot",
        "take screenshot": "screenshot",
        "clipboard": "clipboard",
        "read clipboard": "clipboard",
        "system info": "sysinfo",
        "sysinfo": "sysinfo",
        "pc status": "sysinfo",
        "daily brief": "daily_brief",
        "brief": "daily_brief",
        "focus mode": "focus_mode",
        "pomodoro": "pomodoro",
        "stop pomodoro": "stop_pomodoro",
        "cleanup temp": "cleanup_temp",
        "clean temp": "cleanup_temp",
        "organize downloads": "organize_downloads",
        "downloads preview": "organize_downloads",
        "check email": "check_email",
        "email summary": "email_summary",
        "ocr screen": "ocr_screen",
        "read my screen": "look_at_screen",
        "look at screen": "look_at_screen",
        "open steam": "open_steam",
    }
    if lower in exact_actions:
        action = exact_actions[lower]
        param = "dry" if action == "organize_downloads" else ""
        return LocalIntent(action=action, param=param)

    m = re.match(
        r"(?:weather(?:\s+in)?|what(?:'s| is) the weather(?:\s+in)?)\s*(.*)$",
        text.strip(),
        re.IGNORECASE,
    )
    if m:
        return LocalIntent(action="weather", param=_clean_param(m.group(1)))

    organize = _after(r"organize downloads\s+(dry|preview|apply|go|run|do it)$", text)
    if organize:
        param = "dry" if organize.lower() == "preview" else organize
        return LocalIntent(action="organize_downloads", param=param)

    docs = _after(r"(?:search docs|docs search|mdn|stackoverflow|stack overflow)\s+(.+)$", text)
    if docs:
        return LocalIntent(action="search_docs", param=docs)

    command = _after(r"run command\s*[:\-]?\s+(.+)$", text)
    if command:
        return LocalIntent(action="run_command", param=command)

    game = _after(r"(?:launch game|open game)\s+(.+)$", text)
    if game:
        return LocalIntent(action="launch_game", param=game, speech=f"Launching {game}.")

    return None


def match_local_intent(text: str) -> LocalIntent | None:
    """Return a deterministic local intent, or None when the LLM should handle it."""
    stripped = text.strip()
    if not stripped:
        return None

    lower = _norm(stripped)
    for matcher in (
        _time_response,
        _todo_intent,
        _reminder_intent,
        _note_intent,
        _music_intent,
        _browser_intent,
        _utility_intent,
    ):
        if matcher is _time_response:
            intent = matcher(lower)  # type: ignore[arg-type]
        else:
            intent = matcher(stripped, lower)  # type: ignore[misc]
        if intent is not None:
            return intent
    return None


async def execute_local_intent(intent: LocalIntent, router) -> tuple[str, str | None]:
    """Execute a LocalIntent through ActionRouter without calling the chat LLM."""
    if intent.response is not None:
        return intent.response, None
    if not intent.action:
        return intent.speech, None
    synthetic = f"{intent.speech}\nACTION: {intent.action} | {intent.param}"
    return await router.parse_and_execute(synthetic)
