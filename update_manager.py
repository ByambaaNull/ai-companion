"""
update_manager.py — in-app "Update" support for the AI Companion.

The app's own code is frozen into Assistant.exe, so updating means: pull the
latest source from GitHub and rebuild the exe. This module is what the GUI calls;
the heavy, multi-minute rebuild runs in a separate detached process (updater.py)
so it can replace the exe the app is running from.

Flow (triggered by the Settings → Updates button):
    check_for_update()  → git fetch + compare local vs origin/<branch>
    start_update(pid)   → spawn updater.py in its own console, then the app quits

Everything degrades gracefully: if this isn't a git checkout, or the venv /
PyInstaller aren't present (e.g. a user who only downloaded a release zip),
preflight() reports ready=False with a human reason and the button stays inert.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Avoid flashing a console window when the windowed app shells out to git.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# ─── Locating the source checkout ──────────────────────────────────────────────

def repo_root() -> Path | None:
    """Find the cloned repo root (the dir holding .git + assistant.spec).

    Works both when frozen (walk up from dist\\Assistant\\) and from source
    (walk up from this file). Returns None if no checkout is found — e.g. the
    user unzipped a release into a folder with no .git.
    """
    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
    else:
        start = Path(__file__).resolve().parent
    for d in (start, *start.parents):
        if (d / ".git").exists() and (d / "assistant.spec").exists():
            return d
    return None


def _venv_python(root: Path) -> Path:
    return root / "venv" / "Scripts" / "python.exe"


def _pyinstaller_exe(root: Path) -> Path:
    return root / "venv" / "Scripts" / "pyinstaller.exe"


def _git_ok() -> bool:
    try:
        subprocess.run(
            ["git", "--version"], capture_output=True, creationflags=_NO_WINDOW
        )
        return True
    except Exception:
        return False


# ─── Readiness ─────────────────────────────────────────────────────────────────

def preflight() -> dict:
    """Can this machine actually pull + rebuild? Returns a status dict."""
    root = repo_root()
    if root is None:
        return {"ready": False,
                "reason": "Not a source checkout — clone the repo to enable updates."}
    if not _git_ok():
        return {"ready": False, "root": str(root),
                "reason": "git is not installed or not on PATH."}
    if not _venv_python(root).exists():
        return {"ready": False, "root": str(root),
                "reason": "Build environment (venv) not found next to the repo."}
    if not _pyinstaller_exe(root).exists() and not (root / "assistant.spec").exists():
        return {"ready": False, "root": str(root),
                "reason": "PyInstaller / build spec missing — can't rebuild the exe."}
    return {"ready": True, "root": str(root)}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        creationflags=_NO_WINDOW,
    )


# ─── Check ─────────────────────────────────────────────────────────────────────

def check_for_update() -> dict:
    """Fetch and report whether origin has newer commits than the local HEAD."""
    pf = preflight()
    if not pf.get("ready"):
        return {"ok": True, "ready": False, "available": False,
                "reason": pf.get("reason", "Updates unavailable.")}
    root = Path(pf["root"])
    try:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
        fetched = _git(root, "fetch", "origin", branch)
        if fetched.returncode != 0:
            return {"ok": False, "ready": True, "available": False,
                    "reason": "Could not reach GitHub: "
                              + (fetched.stderr.strip() or "fetch failed")}
        behind = _git(root, "rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip()
        behind_n = int(behind) if behind.isdigit() else 0
        subject = _git(root, "log", "-1", "--format=%s", f"origin/{branch}").stdout.strip()
        return {"ok": True, "ready": True, "available": behind_n > 0,
                "behind": behind_n, "branch": branch, "subject": subject}
    except Exception as exc:
        return {"ok": False, "ready": True, "available": False,
                "reason": f"Update check failed: {exc}"}


# ─── Apply ─────────────────────────────────────────────────────────────────────

def start_update(app_pid: int) -> dict:
    """Launch updater.py in its own console window and return immediately.

    The caller (the GUI) should quit shortly after a successful start so the
    rebuild can overwrite the running exe.
    """
    pf = preflight()
    if not pf.get("ready"):
        return {"ok": False, "reason": pf.get("reason", "Updates unavailable.")}
    root = Path(pf["root"])
    py = _venv_python(root)
    updater = root / "updater.py"
    if not updater.exists():
        return {"ok": False, "reason": "updater.py missing from the repo."}
    try:
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [str(py), str(updater), "--root", str(root), "--wait-pid", str(app_pid)],
            cwd=str(root), creationflags=flags, close_fds=True,
        )
        return {"ok": True, "root": str(root)}
    except Exception as exc:
        return {"ok": False, "reason": f"Could not start updater: {exc}"}
