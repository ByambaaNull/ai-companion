"""
actions/file_organizer.py — Downloads folder organizer + temp/recycle report.

organize_downloads sorts loose files in ~/Downloads into category subfolders
by extension. Dry-run by default (controlled by settings
automations.downloads_organizer_dry_run, or explicit param "dry" / "apply").
NEVER deletes anything; collisions get "name (2).ext" renames; files touched
in the last 10 minutes are skipped (may be mid-download).

cleanup_temp is purely informational — reports temp / Recycle Bin sizes and
suggests cleanup, never deletes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import settings

log = logging.getLogger(__name__)

DOWNLOADS_DIR: Path = Path.home() / "Downloads"

_RECENT_FILE_GRACE_S = 10 * 60  # skip files modified within the last 10 min

CATEGORIES: dict[str, set[str]] = {
    "Documents":  {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                   ".txt", ".md", ".rtf", ".odt", ".ods", ".odp", ".csv",
                   ".epub", ".mobi"},
    "Images":     {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
                   ".ico", ".tiff", ".tif", ".heic", ".raw", ".psd"},
    "Videos":     {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv",
                   ".m4v", ".mpg", ".mpeg", ".ts"},
    "Audio":      {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
                   ".wma", ".mid", ".midi"},
    "Archives":   {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
                   ".iso", ".cab"},
    "Installers": {".exe", ".msi", ".msix", ".appx", ".apk", ".deb", ".rpm",
                   ".dmg", ".pkg"},
    "Code":       {".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
                   ".yaml", ".yml", ".toml", ".ini", ".bat", ".ps1", ".sh",
                   ".c", ".cpp", ".h", ".java", ".cs", ".go", ".rs", ".sql",
                   ".ipynb"},
}

_CATEGORY_NAMES = list(CATEGORIES.keys()) + ["Other"]


def _categorise(path: Path) -> str:
    ext = path.suffix.lower()
    for category, extensions in CATEGORIES.items():
        if ext in extensions:
            return category
    return "Other"


def _safe_destination(dest_dir: Path, name: str) -> Path:
    """Collision-safe target path: name.ext → name (2).ext → name (3).ext …"""
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while True:
        candidate = dest_dir / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _scan_downloads() -> tuple[dict[str, list[Path]], int]:
    """Return ({category: [files]}, skipped_recent_count). Skips folders."""
    plan: dict[str, list[Path]] = {}
    skipped_recent = 0
    now = time.time()
    for entry in DOWNLOADS_DIR.iterdir():
        if entry.is_dir():
            continue  # never touch folders (incl. our category folders)
        try:
            if now - entry.stat().st_mtime < _RECENT_FILE_GRACE_S:
                skipped_recent += 1
                continue
        except OSError:
            continue
        plan.setdefault(_categorise(entry), []).append(entry)
    return plan, skipped_recent


def _apply_moves(plan: dict[str, list[Path]]) -> tuple[dict[str, int], list[str]]:
    """Move files per plan. Returns (moved counts per category, error strings)."""
    moved: dict[str, int] = {}
    errors: list[str] = []
    for category, files in plan.items():
        dest_dir = DOWNLOADS_DIR / category
        try:
            dest_dir.mkdir(exist_ok=True)
        except OSError as exc:
            errors.append(f"Couldn't create {category}/: {exc}")
            continue
        for f in files:
            try:
                shutil.move(str(f), str(_safe_destination(dest_dir, f.name)))
                moved[category] = moved.get(category, 0) + 1
            except Exception as exc:
                errors.append(f"{f.name}: {exc}")
    return moved, errors


async def organize_downloads(param: str = "") -> str:
    """Organize ~/Downloads into category subfolders. param: "dry" | "apply"."""
    try:
        if not DOWNLOADS_DIR.exists():
            return f"Downloads folder not found at {DOWNLOADS_DIR}."

        mode = param.strip().lower()
        if mode in ("dry", "dryrun", "dry-run", "preview"):
            dry_run = True
        elif mode in ("apply", "go", "run", "do it"):
            dry_run = False
        else:
            dry_run = bool(settings.get(
                "automations.downloads_organizer_dry_run", True))

        loop = asyncio.get_running_loop()
        plan, skipped_recent = await loop.run_in_executor(None, _scan_downloads)

        total = sum(len(v) for v in plan.values())
        if total == 0:
            note = (f" ({skipped_recent} recent file(s) skipped — possibly "
                    "still downloading)") if skipped_recent else ""
            return f"Downloads folder is already tidy — nothing to move{note}."

        if dry_run:
            lines = [f"Dry run — {total} file(s) WOULD move (nothing touched):"]
            for category in _CATEGORY_NAMES:
                files = plan.get(category, [])
                if not files:
                    continue
                lines.append(f"  {category} ({len(files)}):")
                for f in sorted(files, key=lambda p: p.name.lower())[:8]:
                    lines.append(f"    • {f.name}")
                if len(files) > 8:
                    lines.append(f"    … and {len(files) - 8} more")
            if skipped_recent:
                lines.append(f"  (skipped {skipped_recent} recently-modified file(s))")
            lines.append("Say 'organize downloads apply' to actually move them.")
            return "\n".join(lines)

        moved, errors = await loop.run_in_executor(None, _apply_moves, plan)
        moved_total = sum(moved.values())
        parts = [f"{category}: {n}" for category, n in moved.items()]
        summary = (f"Organized Downloads — moved {moved_total} file(s) "
                   f"({', '.join(parts)}).")
        if skipped_recent:
            summary += f" Skipped {skipped_recent} recent file(s)."
        if errors:
            summary += f" {len(errors)} file(s) couldn't be moved: " + \
                       "; ".join(errors[:3])
        return summary
    except Exception as exc:
        log.error("organize_downloads failed: %s", exc, exc_info=True)
        return f"Couldn't organize Downloads: {exc}"


# ─── Temp / Recycle Bin report (read-only) ───────────────────────────────────

def _dir_size_bytes(root: Path, max_seconds: float = 10.0) -> tuple[int, bool]:
    """Total size of files under root. Returns (bytes, complete). Time-capped."""
    total = 0
    deadline = time.time() + max_seconds
    complete = True
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if time.time() > deadline:
            complete = False
            break
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total, complete


def _recycle_bin_size() -> tuple[int, int] | None:
    """(bytes, item_count) of the Recycle Bin via shell32, or None."""
    try:
        import ctypes

        class SHQUERYRBINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong),
                        ("i64Size", ctypes.c_longlong),
                        ("i64NumItems", ctypes.c_longlong)]

        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
        result = ctypes.windll.shell32.SHQueryRecycleBinW(None,
                                                          ctypes.byref(info))
        if result == 0:  # S_OK
            return int(info.i64Size), int(info.i64NumItems)
    except Exception as exc:
        log.debug("Recycle Bin query failed: %s", exc)
    return None


def _fmt_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


async def cleanup_temp(param: str = "") -> str:
    """Report temp folder + Recycle Bin sizes. Read-only — never deletes."""
    try:
        loop = asyncio.get_running_loop()
        lines: list[str] = []

        temp_dir = Path(tempfile.gettempdir())
        size, complete = await loop.run_in_executor(
            None, _dir_size_bytes, temp_dir)
        approx = "" if complete else " (approx — scan capped)"
        lines.append(f"Temp folder ({temp_dir}): {_fmt_size(size)}{approx}")

        rb = await loop.run_in_executor(None, _recycle_bin_size)
        if rb is not None:
            rb_size, rb_items = rb
            lines.append(f"Recycle Bin: {_fmt_size(rb_size)} "
                         f"in {rb_items} item(s)")
        else:
            lines.append("Recycle Bin: size not measurable right now.")

        total_reclaim = size + (rb[0] if rb else 0)
        if total_reclaim > 500 * 1024 * 1024:
            lines.append(f"You could reclaim roughly {_fmt_size(total_reclaim)} — "
                         "run Windows 'Disk Cleanup' or Storage Sense when ready. "
                         "I never delete anything myself.")
        else:
            lines.append("Nothing worth cleaning — you're in good shape.")
        return "\n".join(lines)
    except Exception as exc:
        log.error("cleanup_temp failed: %s", exc, exc_info=True)
        return f"Couldn't measure temp space: {exc}"
