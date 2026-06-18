"""
backup.py — Portable export / restore for all companion data.

Creates self-contained ZIP archives that can be moved to any machine.
Memories are exported as plain text so they survive ChromaDB / mem0
version changes — they are re-embedded on the target machine.

── What's backed up ──────────────────────────────────────────────────
  • All memories (text, exported via mem0 get_all → JSON)
  • music_prefs.json   (liked / disliked song ratings)
  • favourites.json    (song aliases and YouTube URLs)

── What's NOT backed up ──────────────────────────────────────────────
  • data/music_cache/  (audio files — large, can be re-downloaded)
  • data/temp/         (ephemeral)
  • data/companion.log (not useful after the fact)
  • models/            (re-downloadable via bootstrap.py)

── CLI usage ─────────────────────────────────────────────────────────
  python backup.py                       create a backup now
  python backup.py --list                list all available backups
  python backup.py --restore             restore from the latest backup
  python backup.py --restore <file.zip>  restore from a specific archive

── Cloud sync tip ────────────────────────────────────────────────────
  Point BACKUP_DIR in config.py to your OneDrive / Google Drive folder:
    BACKUP_DIR: Path = Path.home() / "OneDrive" / "companion_backups"
  Backups then sync automatically to the cloud and any other device.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import sys
import zipfile
from pathlib import Path

import config
from config import setup_logging

log = logging.getLogger("backup")

_MANIFEST_VERSION = "1"

# Flat JSON files included in every backup (memories handled separately)
_DATA_FILES: list[Path] = [
    config.FAVOURITES_FILE,
    config.MUSIC_PREFS_FILE,
]


# ─── Memory export / import ───────────────────────────────────────────────────

def export_memories() -> list[dict]:
    """
    Read every stored memory via mem0 and return as a plain list of dicts.

    Each dict: {"memory": <text>, "metadata": <dict>}
    Text is all that's needed for re-import — IDs and embeddings regenerate.
    """
    try:
        from memory import MemoryManager
        mgr = MemoryManager()
        records = mgr.get_all(user_id=config.USER_ID)
        exported = [
            {"memory": r["memory"], "metadata": r.get("metadata") or {}}
            for r in records
            if r.get("memory")
        ]
        log.info("Exported %d memories", len(exported))
        return exported
    except Exception as exc:
        log.error("Memory export failed: %s", exc)
        return []


def import_memories(memories: list[dict]) -> int:
    """
    Re-add exported memories via mem0.  Returns the number successfully added.

    Each memory is added with mem0.add() which re-extracts facts and
    re-embeds them — fully compatible with whatever version is installed.
    """
    if not memories:
        return 0
    try:
        from memory import MemoryManager
        mgr = MemoryManager()
        count = 0
        total = len(memories)
        for i, entry in enumerate(memories, 1):
            text = entry.get("memory", "").strip()
            if text:
                mgr.add(text, user_id=config.USER_ID)
                count += 1
            if i % 10 == 0 or i == total:
                log.info("  … importing memories %d/%d", i, total)
        log.info("Imported %d/%d memories", count, total)
        return count
    except Exception as exc:
        log.error("Memory import failed: %s", exc)
        return 0


# ─── Archive creation ─────────────────────────────────────────────────────────

def create_backup(dest_dir: Path | None = None) -> Path:
    """
    Create a timestamped ZIP backup.  Returns the path to the archive.

    Safe to call from the asyncio thread via run_in_executor.
    """
    dest_dir = dest_dir or config.BACKUP_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    ts       = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    archive  = dest_dir / f"companion_backup_{ts}.zip"

    log.info("Creating backup: %s", archive)
    memories = export_memories()

    manifest = {
        "version":        _MANIFEST_VERSION,
        "timestamp":      ts,
        "companion_name": config.COMPANION_NAME,
        "user_id":        config.USER_ID,
        "memory_count":   len(memories),
    }

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(
            "memories.json",
            json.dumps(memories, indent=2, ensure_ascii=False),
        )
        for path in _DATA_FILES:
            if path.exists():
                zf.write(path, path.name)
                log.debug("  + %s", path.name)
            else:
                log.debug("  - skipped (missing): %s", path.name)

    size_kb = archive.stat().st_size // 1024
    log.info(
        "Backup complete ✓  %s  (%d memories, %d KB)",
        archive.name, len(memories), size_kb,
    )
    return archive


# ─── Archive restore ──────────────────────────────────────────────────────────

def restore_backup(archive: Path) -> dict:
    """
    Restore data from a backup archive.  Returns a summary dict.

    On a brand-new machine: run bootstrap.py first to set up models,
    then run:  python backup.py --restore
    """
    if not archive.exists():
        raise FileNotFoundError(f"Backup archive not found: {archive}")

    summary: dict = {"memories_restored": 0, "files_restored": []}
    log.info("Restoring from: %s", archive)

    with zipfile.ZipFile(archive, "r") as zf:
        names = zf.namelist()

        # Print manifest for reference
        if "manifest.json" in names:
            mf = json.loads(zf.read("manifest.json"))
            log.info(
                "Archive: %s | companion=%s | memories=%s",
                mf.get("timestamp"), mf.get("companion_name"), mf.get("memory_count"),
            )

        # Restore memories
        if "memories.json" in names:
            memories = json.loads(zf.read("memories.json").decode("utf-8"))
            log.info("Re-importing %d memories (re-embedding on this machine)…", len(memories))
            n = import_memories(memories)
            summary["memories_restored"] = n

        # Restore data files
        for path in _DATA_FILES:
            if path.name in names:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(zf.read(path.name))
                summary["files_restored"].append(path.name)
                log.info("Restored: %s", path.name)

    log.info(
        "Restore complete ✓  memories=%d  files=%s",
        summary["memories_restored"],
        summary["files_restored"],
    )
    return summary


# ─── Listing ──────────────────────────────────────────────────────────────────

def list_backups(backup_dir: Path | None = None) -> list[Path]:
    """Return all backup archives sorted newest-first."""
    d = backup_dir or config.BACKUP_DIR
    if not d.exists():
        return []
    return sorted(d.glob("companion_backup_*.zip"), reverse=True)


# ─── Auto-backup (called from agent loop) ────────────────────────────────────

def _days_since_last_backup() -> float:
    """Return days since the newest backup, or ∞ if no backups exist."""
    archives = list_backups()
    if not archives:
        return float("inf")
    stem = archives[0].stem   # companion_backup_2026-05-03_12-00-00
    try:
        ts_str = stem[len("companion_backup_"):]
        last   = datetime.datetime.strptime(ts_str, "%Y-%m-%d_%H-%M-%S")
        return (datetime.datetime.now() - last).total_seconds() / 86400
    except (ValueError, IndexError):
        return float("inf")


async def auto_backup_if_needed() -> str | None:
    """
    Non-blocking: create a backup in the executor if AUTO_BACKUP_INTERVAL_DAYS
    have passed since the last one.  Returns archive path string or None.

    Call once from agent_loop() at startup.
    """
    if _days_since_last_backup() < config.AUTO_BACKUP_INTERVAL_DAYS:
        log.debug("Auto-backup: not needed yet")
        return None
    log.info("Auto-backup: creating backup (%.1f days since last)…", _days_since_last_backup())
    loop    = asyncio.get_running_loop()
    archive = await loop.run_in_executor(None, create_backup)
    return str(archive)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _cli() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Backup and restore AI Companion data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--restore",
        nargs="?",
        const="latest",
        metavar="FILE.zip",
        help="Restore from a backup archive (omit file to use the latest)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all available backup archives",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help=f"Override backup directory (default: {config.BACKUP_DIR})",
    )
    args = parser.parse_args()

    if args.list:
        archives = list_backups(args.dir)
        if not archives:
            print(f"No backups found in {args.dir or config.BACKUP_DIR}")
        else:
            print(f"Backups in {args.dir or config.BACKUP_DIR}:")
            for i, a in enumerate(archives):
                tag = " ← latest" if i == 0 else ""
                size_kb = a.stat().st_size // 1024
                print(f"  {a.name}  ({size_kb} KB){tag}")
        return

    if args.restore is not None:
        if args.restore == "latest":
            archives = list_backups(args.dir)
            if not archives:
                print("No backups found. Create one first with:  python backup.py")
                sys.exit(1)
            archive = archives[0]
        else:
            archive = Path(args.restore)
        summary = restore_backup(archive)
        print(
            f"\nRestore complete:\n"
            f"  Memories restored : {summary['memories_restored']}\n"
            f"  Files restored    : {', '.join(summary['files_restored']) or 'none'}"
        )
        return

    # Default: create backup
    archive = create_backup(args.dir)
    print(f"\nBackup saved to:\n  {archive}")
    print("\nTo restore on a new machine:")
    print("  1. Install Python dependencies:  pip install -r requirements.txt")
    print("  2. Run bootstrap:                python bootstrap.py")
    print(f"  3. Copy this ZIP to the new machine and run:")
    print(f"       python backup.py --restore {archive.name}")


if __name__ == "__main__":
    _cli()
