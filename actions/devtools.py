"""
devtools.py — IT-student / developer quality-of-life actions.

Actions provided:
  pomodoro        Start a Pomodoro focus timer (25 min work / 5 min break).
  stop_pomodoro   Cancel the running timer.
  run_command     Run a shell command and speak the output.
  open_localhost  Open localhost:<port> in the default browser.
  open_folder     Open a folder in Windows Explorer.
  search_docs     Search Stack Overflow or MDN in the browser.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import webbrowser
from pathlib import Path

log = logging.getLogger(__name__)

# ─── Pomodoro ──────────────────────────────────────────────────────────────────

_pomo_task:   asyncio.Task | None = None
_pomo_count:  int = 0          # completed work sessions this run
_pomo_phase:  str = "idle"     # "work" | "break" | "idle"

_WORK_MINUTES  = 25
_BREAK_MINUTES = 5

_BLOCKED_COMMANDS = [
    # Destructive file/disk ops
    "rm -rf", "rm -r", "remove-item -recurse", "del /f", "del /s",
    "format ", "diskpart", "rmdir /s", "rd /s",
    # Registry / boot manipulation
    "reg delete", "reg add", "bcdedit", "regedit",
    # System state
    "shutdown", "restart-computer", "stop-computer",
    # Privilege escalation / persistence
    "net user", "net localgroup", "schtasks /create", "at ",
    # Obfuscated execution
    "invoke-expression", "iex ", "-encodedcommand", "-enc ",
    "[system.reflection", "[convert]::",
    # Fork bomb
    ":(){", "${function:",
    # DB / data destruction
    "drop table", "drop database", "truncate table",
]


async def start_pomodoro(tts=None, work_min: int = _WORK_MINUTES, break_min: int = _BREAK_MINUTES) -> str:
    """
    Start a Pomodoro cycle: work_min minutes of focus, then break_min minutes of break.
    Repeats until stop_pomodoro() is called.
    tts: TTSEngine instance — used to speak notifications when timers end.
    """
    global _pomo_task, _pomo_count, _pomo_phase

    if _pomo_task and not _pomo_task.done():
        return (
            f"Timer already running ({_pomo_phase} phase). "
            "Say 'stop pomodoro' to cancel it first."
        )

    _pomo_count = 0

    async def _cycle():
        global _pomo_count, _pomo_phase
        try:
            while True:
                _pomo_phase = "work"
                log.info("Pomodoro: work session %d starting (%d min)", _pomo_count + 1, work_min)
                await asyncio.sleep(work_min * 60)
                _pomo_count += 1
                msg = (
                    f"Time's up! That's {_pomo_count} Pomodoro"
                    f"{'s' if _pomo_count > 1 else ''} done. "
                    f"Take a {break_min}-minute break."
                )
                log.info("Pomodoro: %s", msg)
                if tts:
                    await tts.speak(msg)

                _pomo_phase = "break"
                await asyncio.sleep(break_min * 60)
                back_msg = "Break over. Back to work — let's get it."
                log.info("Pomodoro: %s", back_msg)
                if tts:
                    await tts.speak(back_msg)
        except asyncio.CancelledError:
            _pomo_phase = "idle"

    _pomo_task = asyncio.create_task(_cycle(), name="pomodoro")
    _pomo_phase = "work"
    return (
        f"Pomodoro started — {work_min} minutes of focus. "
        "I'll tell you when it's time for a break."
    )


def stop_pomodoro() -> str:
    global _pomo_task, _pomo_phase
    if _pomo_task and not _pomo_task.done():
        _pomo_task.cancel()
        _pomo_task = None
        _pomo_phase = "idle"
        return f"Timer stopped. You completed {_pomo_count} Pomodoro(s) this session."
    return "No timer is running."


def pomodoro_status() -> str:
    if _pomo_phase == "idle" or _pomo_task is None or _pomo_task.done():
        return f"No timer running. Completed {_pomo_count} Pomodoro(s) this session."
    return f"Currently in {_pomo_phase} phase. {_pomo_count} Pomodoro(s) completed so far."


# ─── Terminal command runner ───────────────────────────────────────────────────

def _is_safe_command(cmd: str) -> bool:
    lower = cmd.lower()
    return not any(blocked in lower for blocked in _BLOCKED_COMMANDS)


async def run_command(command: str) -> str:
    """
    Run a PowerShell command and return the first 900 chars of output.
    Blocks destructive commands (rm -rf, format, shutdown, etc.).
    """
    command = command.strip()
    if not command:
        return "No command specified."

    if not _is_safe_command(command):
        return (
            f"That command looks destructive — I won't run it automatically. "
            "Run it yourself in the terminal if you're sure."
        )

    log.info("run_command: %s", command)
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(Path.home()),
            ),
        )
        out = (result.stdout + result.stderr).strip()
        if not out:
            return f"Command ran with exit code {result.returncode}, no output."
        if len(out) > 900:
            out = out[:897] + "…"
        return out
    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds."
    except FileNotFoundError:
        return "PowerShell not found. Try a simpler command."
    except Exception as exc:
        return f"Command failed: {exc}"


# ─── Localhost opener ──────────────────────────────────────────────────────────

def open_localhost(param: str) -> str:
    """Open localhost:<port> in the default browser. Param can be '3000' or 'port 3000'."""
    raw = param.strip().lower().replace("port", "").replace(":", "").strip()
    try:
        port = int(raw) if raw else 3000
    except ValueError:
        port = 3000
    url = f"http://localhost:{port}"
    webbrowser.open(url)
    return f"Opened {url}."


# ─── Folder opener ────────────────────────────────────────────────────────────

def open_folder(param: str) -> str:
    """Open a folder by name or path in Windows Explorer."""
    shortcuts = {
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "desktop":   Path.home() / "Desktop",
        "projects":  Path.home() / "Documents" / "GitHub",
        "github":    Path.home() / "Documents" / "GitHub",
        "home":      Path.home(),
    }
    key = param.strip().lower()
    path = shortcuts.get(key, Path(param.strip()))
    if not path.exists():
        return f"Folder not found: {path}"
    os.startfile(str(path))
    return f"Opened {path}."


# ─── Docs / Stack Overflow search ─────────────────────────────────────────────

def search_docs(param: str) -> str:
    """
    Search developer resources in the browser.
    Prefix with 'mdn:', 'so:', or 'pypi:' to target specific sites.
    Default: Stack Overflow.

    Examples:
      search_docs | how to reverse a list python
      search_docs | mdn: flex box css
      search_docs | pypi: httpx
    """
    import urllib.parse

    query = param.strip()
    if not query:
        return "Please specify what to search for."

    if query.lower().startswith("mdn:"):
        q   = query[4:].strip()
        url = f"https://developer.mozilla.org/en-US/search?q={urllib.parse.quote(q)}"
        label = "MDN"
    elif query.lower().startswith("pypi:"):
        q   = query[5:].strip()
        url = f"https://pypi.org/search/?q={urllib.parse.quote(q)}"
        label = "PyPI"
    elif query.lower().startswith("so:"):
        q   = query[3:].strip()
        url = f"https://stackoverflow.com/search?q={urllib.parse.quote(q)}"
        label = "Stack Overflow"
    else:
        url   = f"https://stackoverflow.com/search?q={urllib.parse.quote(query)}"
        label = "Stack Overflow"

    webbrowser.open(url)
    return f"Searching {label} for: {query}"
