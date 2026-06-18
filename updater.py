"""
updater.py — detached rebuild helper for the AI Companion "Update" button.

Runs in its own console window (launched by update_manager.start_update) using
the project's venv Python. It outlives the app so it can replace the very exe the
app was running from.

Steps:
    1. wait for the app process to exit (so dist\\Assistant\\Assistant.exe unlocks)
    2. git pull --ff-only        (read-only update of the source)
    3. rename dist\\Assistant -> dist\\Assistant.bak   (keep a working fallback)
    4. pyinstaller assistant.spec --noconfirm  +  fix_bundle.ps1
    5. on success: move the user's data/models/.env/.pth from .bak into the
       fresh bundle, write a result marker, delete .bak, relaunch
       on failure:  delete the half-built bundle, restore .bak, relaunch the
       old exe — the user is never left without a working app

Usage (invoked automatically):
    venv\\Scripts\\python.exe updater.py --root <repo> --wait-pid <pid>
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# User content that must survive PyInstaller's --noconfirm wipe of dist\Assistant.
_PRESERVE_DIRS = ["data", "models"]
_PRESERVE_GLOBS = [".env", "*.pth", "*.index"]


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    """Windows: is the given PID still running?"""
    if pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def wait_for_exit(pid: int, timeout: float = 90.0) -> None:
    log(f"Waiting for the app (pid {pid}) to close…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            log("App closed.")
            return
        time.sleep(0.5)
    log("Timed out waiting for the app to close — continuing anyway.")


def run(cmd: list[str], cwd: Path) -> int:
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=str(cwd)).returncode


def move_preserved(src_bundle: Path, dst_bundle: Path) -> None:
    """Move user data/models and copy keys/models from the old bundle to the new."""
    for d in _PRESERVE_DIRS:
        s = src_bundle / d
        if not s.exists():
            continue
        t = dst_bundle / d
        if t.exists():
            # Merge (e.g. fix_bundle seeded some models; keep the user's superset).
            shutil.copytree(s, t, dirs_exist_ok=True)
        else:
            shutil.move(str(s), str(t))
        log(f"Restored {d}\\")
    for pattern in _PRESERVE_GLOBS:
        for s in src_bundle.glob(pattern):
            try:
                shutil.copy2(str(s), str(dst_bundle / s.name))
                log(f"Restored {s.name}")
            except Exception as exc:
                log(f"Could not restore {s.name}: {exc}")


def write_result(bundle: Path, ok: bool, detail: str) -> None:
    """Drop a marker the app reads on next launch to toast the outcome."""
    try:
        data_dir = bundle / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "last_update.json").write_text(
            json.dumps({"ok": ok, "detail": detail,
                        "time": datetime.now().isoformat(timespec="seconds")}),
            encoding="utf-8",
        )
    except Exception:
        pass


def relaunch(bundle: Path) -> None:
    exe = bundle / "Assistant.exe"
    if exe.exists():
        log("Relaunching the app…")
        try:
            os.startfile(str(exe))  # noqa: S606 (intentional app relaunch)
        except Exception as exc:
            log(f"Could not relaunch automatically: {exc}\n  Start it from: {exe}")
    else:
        log(f"Built exe not found at {exe}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--wait-pid", type=int, default=0)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    dist = root / "dist" / "Assistant"
    bak = root / "dist" / "Assistant.bak"

    global _LOG_FILE
    _LOG_FILE = root / ".update" / "update.log"

    print("=" * 60)
    print(" AI Companion — Updater")
    print(" Pulling the latest version and rebuilding. This takes a few")
    print(" minutes. Please don't close this window.")
    print("=" * 60)

    if args.wait_pid:
        wait_for_exit(args.wait_pid)

    # 1) Pull latest source (read-only, fast-forward only).
    if run(["git", "pull", "--ff-only", "origin"], root) != 0:
        log("git pull failed (local changes or no network?). Aborting; app unchanged.")
        write_result(dist if dist.exists() else root, False, "git pull failed")
        relaunch(dist)
        _pause_on_error()
        return 1

    # 2) Stash the existing working bundle as a fallback.
    had_bundle = dist.exists()
    if had_bundle:
        if bak.exists():
            shutil.rmtree(bak, ignore_errors=True)
        log("Setting aside the current build as a fallback…")
        os.rename(dist, bak)

    # 3) Rebuild.
    pyinstaller = root / "venv" / "Scripts" / "pyinstaller.exe"
    log("Building the exe (PyInstaller)…")
    build_ok = run([str(pyinstaller), "assistant.spec", "--noconfirm"], root) == 0
    if build_ok:
        fix = root / "fix_bundle.ps1"
        if fix.exists():
            log("Aligning runtime + seeding models (fix_bundle.ps1)…")
            run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(fix)], root)

    # 4) Success → restore user content; Failure → roll back.
    if build_ok and dist.exists():
        if had_bundle:
            log("Carrying your data, models and keys into the new build…")
            move_preserved(bak, dist)
            shutil.rmtree(bak, ignore_errors=True)
        write_result(dist, True, "Updated and rebuilt successfully.")
        log("Update complete.")
        relaunch(dist)
        time.sleep(2)
        return 0
    else:
        log("BUILD FAILED — rolling back to the previous working version.")
        shutil.rmtree(dist, ignore_errors=True)
        if had_bundle and bak.exists():
            os.rename(bak, dist)
        write_result(dist if dist.exists() else root, False,
                     "Build failed — restored the previous version.")
        relaunch(dist)
        _pause_on_error()
        return 1


def _pause_on_error() -> None:
    try:
        input("\nPress Enter to close this window…")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
