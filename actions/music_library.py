"""
actions/music_library.py — JSON-backed offline music library + playlist store.

This is the persistence layer for the new offline-first music system. It owns
two files (paths come from config):

    MUSIC_LIBRARY_FILE  → every known track (downloaded or not)
    PLAYLISTS_FILE      → user-created playlists (ordered lists of track ids)

Design goals:
    * Pure / file-backed — no audio, no network, no heavy deps.
    * Thread-safe — a single module-level lock guards every read-modify-write so
      the GUI thread and the agent thread can both call in safely.
    * Crash-proof — atomic writes (.tmp + os.replace) and corrupt/missing files
      degrade to empty structures. Public functions never raise to callers.

Track schema (values of library["tracks"]):
    {
      "id":         "<12-hex sha1>",
      "title":      "...",
      "artist":     "",
      "source":     "youtube" | "spotify" | "local",
      "source_url": "",
      "local_path": "",          # absolute path to the cached mp3 (if downloaded)
      "duration":   0,           # seconds
      "added_ts":   0.0,
      "liked":      false,
      "favourite":  false,
      "play_count": 0,
      "last_played": 0.0
    }

Playlist schema (values of playlists["playlists"]):
    { "id": "pl_<10-hex>", "name": "...", "created_ts": 0.0, "track_ids": [] }
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from config import (
    FAVOURITES_FILE,
    MUSIC_LIBRARY_FILE,
    MUSIC_PREFS_FILE,
    PLAYLISTS_FILE,
)

log = logging.getLogger(__name__)

# A single re-entrant lock guards every file mutation. Re-entrant so a public
# function may call another public function (e.g. delete_track → playlist edits)
# without dead-locking.
_LOCK = threading.RLock()

# Guards the one-time legacy migration so concurrent _load() calls don't double-run.
_MIGRATED = False


# ─── Low-level JSON IO (atomic, tolerant) ─────────────────────────────────────

def _read_json(path: Path, default: dict) -> dict:
    """Load JSON from *path*; return a copy of *default* on any error."""
    try:
        if not path.exists():
            return json.loads(json.dumps(default))
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return json.loads(json.dumps(default))
        return data
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log.warning("music_library: could not read %s (%s) — using empty store", path, exc)
        return json.loads(json.dumps(default))


def _write_json(path: Path, data: dict) -> None:
    """Atomically write *data* as JSON to *path* (.tmp then replace)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.error("music_library: failed to write %s: %s", path, exc)


def _load_library() -> dict:
    lib = _read_json(MUSIC_LIBRARY_FILE, {"tracks": {}})
    if not isinstance(lib.get("tracks"), dict):
        lib["tracks"] = {}
    return lib


def _save_library(lib: dict) -> None:
    _write_json(MUSIC_LIBRARY_FILE, lib)


def _load_playlists() -> dict:
    pls = _read_json(PLAYLISTS_FILE, {"playlists": {}})
    if not isinstance(pls.get("playlists"), dict):
        pls["playlists"] = {}
    return pls


def _save_playlists(pls: dict) -> None:
    _write_json(PLAYLISTS_FILE, pls)


# ─── ID helpers ───────────────────────────────────────────────────────────────

def _track_id(source_url: str, title: str, artist: str) -> str:
    basis = (source_url or f"{title}|{artist}").lower()
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _playlist_id(name: str) -> str:
    # Stable per (name + creation nonce). The uuid nonce means two playlists with
    # the same name still get distinct ids.
    basis = f"{name}|{uuid.uuid4().hex}".lower()
    return "pl_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]


def _blank_track(track_id: str) -> dict:
    return {
        "id": track_id,
        "title": "",
        "artist": "",
        "source": "youtube",
        "source_url": "",
        "local_path": "",
        "duration": 0,
        "added_ts": 0.0,
        "liked": False,
        "favourite": False,
        "play_count": 0,
        "last_played": 0.0,
    }


def _normalise_track(track_id: str, raw: dict) -> dict:
    """Merge a stored (possibly partial) dict onto the canonical blank shape."""
    out = _blank_track(track_id)
    if isinstance(raw, dict):
        for key in out:
            if key in raw and raw[key] is not None:
                out[key] = raw[key]
    out["id"] = track_id  # id is authoritative from the map key
    return out


def _ensure_loaded() -> None:
    """Run the one-time legacy migration on first access."""
    global _MIGRATED
    if _MIGRATED:
        return
    with _LOCK:
        if _MIGRATED:
            return
        try:
            migrate_legacy()
        except Exception as exc:  # never let migration break startup
            log.warning("music_library: legacy migration skipped (%s)", exc)
        _MIGRATED = True


# ─── Track CRUD ───────────────────────────────────────────────────────────────

def add_track(title: str, artist: str = "", source: str = "youtube",
              source_url: str = "", local_path: str = "", duration: int = 0) -> dict:
    """
    Upsert a track by computed id.

    If the track already exists, liked / favourite / play_count / last_played /
    added_ts are preserved; title/artist/source/source_url/duration are refreshed
    when new non-empty values are supplied, and local_path is set/refreshed.
    Returns the resulting track dict.
    """
    _ensure_loaded()
    title = (title or "").strip()
    artist = (artist or "").strip()
    track_id = _track_id(source_url, title, artist)
    with _LOCK:
        lib = _load_library()
        existing = lib["tracks"].get(track_id)
        track = _normalise_track(track_id, existing) if existing else _blank_track(track_id)

        if title:
            track["title"] = title
        if artist:
            track["artist"] = artist
        if source:
            track["source"] = source
        if source_url:
            track["source_url"] = source_url
        if local_path:
            track["local_path"] = str(local_path)
        if duration:
            try:
                track["duration"] = int(duration)
            except (TypeError, ValueError):
                pass
        if not existing:
            track["added_ts"] = time.time()

        lib["tracks"][track_id] = track
        _save_library(lib)
        return dict(track)


def get_track(track_id: str) -> dict | None:
    _ensure_loaded()
    with _LOCK:
        lib = _load_library()
        raw = lib["tracks"].get(track_id)
        return _normalise_track(track_id, raw) if raw else None


def all_tracks() -> list[dict]:
    """Every track, newest first (by added_ts)."""
    _ensure_loaded()
    with _LOCK:
        lib = _load_library()
        tracks = [_normalise_track(tid, raw) for tid, raw in lib["tracks"].items()]
    tracks.sort(key=lambda t: t.get("added_ts", 0.0), reverse=True)
    return tracks


def _file_exists(track: dict) -> bool:
    lp = track.get("local_path") or ""
    try:
        return bool(lp) and Path(lp).exists()
    except OSError:
        return False


def list_tracks(filter: str = "all", search: str = "", playlist_id: str = "") -> list[dict]:
    """
    Filtered/searched view of the library.

    filter ∈ {all, liked, favourites, downloaded, playlist}
        downloaded → local_path set AND the file exists on disk
        playlist   → tracks in playlist_id, in playlist order
    search → case-insensitive substring over title + artist
    """
    _ensure_loaded()
    if filter == "playlist":
        tracks = playlist_tracks(playlist_id)
    else:
        tracks = all_tracks()
        if filter == "liked":
            tracks = [t for t in tracks if t.get("liked")]
        elif filter == "favourites":
            tracks = [t for t in tracks if t.get("favourite")]
        elif filter == "downloaded":
            tracks = [t for t in tracks if _file_exists(t)]

    needle = (search or "").strip().lower()
    if needle:
        tracks = [
            t for t in tracks
            if needle in t.get("title", "").lower() or needle in t.get("artist", "").lower()
        ]
    return tracks


def _update_track(track_id: str, mutate) -> dict | None:
    with _LOCK:
        lib = _load_library()
        raw = lib["tracks"].get(track_id)
        if not raw:
            return None
        track = _normalise_track(track_id, raw)
        mutate(track)
        lib["tracks"][track_id] = track
        _save_library(lib)
        return dict(track)


def set_like(track_id: str, liked: bool) -> dict | None:
    return _update_track(track_id, lambda t: t.__setitem__("liked", bool(liked)))


def set_favourite(track_id: str, favourite: bool) -> dict | None:
    return _update_track(track_id, lambda t: t.__setitem__("favourite", bool(favourite)))


def record_play(track_id: str) -> None:
    """Increment play_count and stamp last_played for *track_id* (no-op if unknown)."""
    def _mutate(t: dict) -> None:
        t["play_count"] = int(t.get("play_count", 0)) + 1
        t["last_played"] = time.time()
    _update_track(track_id, _mutate)


def delete_track(track_id: str, delete_file: bool = False) -> bool:
    """Remove a track from the library and from every playlist. Optionally unlink its mp3."""
    _ensure_loaded()
    with _LOCK:
        lib = _load_library()
        raw = lib["tracks"].pop(track_id, None)
        if raw is None:
            return False
        _save_library(lib)

        # purge from playlists
        pls = _load_playlists()
        changed = False
        for pl in pls["playlists"].values():
            ids = pl.get("track_ids", [])
            if track_id in ids:
                pl["track_ids"] = [t for t in ids if t != track_id]
                changed = True
        if changed:
            _save_playlists(pls)

        if delete_file:
            lp = (raw or {}).get("local_path") or ""
            if lp:
                p = Path(lp)
                # On Windows the audio player can hold the file handle for a
                # moment after being stopped — retry briefly before giving up.
                for attempt in range(5):
                    try:
                        if p.exists():
                            p.unlink()
                        break
                    except OSError as exc:
                        if attempt == 4:
                            log.warning("music_library: could not delete file %s: %s", lp, exc)
                        else:
                            time.sleep(0.2)
        return True


# ─── Playlist CRUD ────────────────────────────────────────────────────────────

def create_playlist(name: str) -> dict:
    _ensure_loaded()
    name = (name or "Untitled").strip() or "Untitled"
    with _LOCK:
        pls = _load_playlists()
        pid = _playlist_id(name)
        while pid in pls["playlists"]:  # vanishingly unlikely collision
            pid = _playlist_id(name)
        playlist = {
            "id": pid,
            "name": name,
            "created_ts": time.time(),
            "track_ids": [],
        }
        pls["playlists"][pid] = playlist
        _save_playlists(pls)
        return dict(playlist)


def delete_playlist(playlist_id: str) -> bool:
    _ensure_loaded()
    with _LOCK:
        pls = _load_playlists()
        if pls["playlists"].pop(playlist_id, None) is None:
            return False
        _save_playlists(pls)
        return True


def rename_playlist(playlist_id: str, name: str) -> bool:
    _ensure_loaded()
    name = (name or "").strip()
    if not name:
        return False
    with _LOCK:
        pls = _load_playlists()
        pl = pls["playlists"].get(playlist_id)
        if pl is None:
            return False
        pl["name"] = name
        _save_playlists(pls)
        return True


def list_playlists() -> list[dict]:
    """Summaries {id, name, count, created_ts}, newest first."""
    _ensure_loaded()
    with _LOCK:
        pls = _load_playlists()
        out = [
            {
                "id": pl.get("id", pid),
                "name": pl.get("name", "Untitled"),
                "count": len(pl.get("track_ids", [])),
                "created_ts": pl.get("created_ts", 0.0),
            }
            for pid, pl in pls["playlists"].items()
        ]
    out.sort(key=lambda p: p.get("created_ts", 0.0), reverse=True)
    return out


def add_to_playlist(playlist_id: str, track_id: str) -> bool:
    """Append a track to a playlist (idempotent — no duplicates)."""
    _ensure_loaded()
    with _LOCK:
        pls = _load_playlists()
        pl = pls["playlists"].get(playlist_id)
        if pl is None:
            return False
        ids = pl.setdefault("track_ids", [])
        if track_id in ids:
            return True  # already present — still a success
        ids.append(track_id)
        _save_playlists(pls)
        return True


def remove_from_playlist(playlist_id: str, track_id: str) -> bool:
    _ensure_loaded()
    with _LOCK:
        pls = _load_playlists()
        pl = pls["playlists"].get(playlist_id)
        if pl is None:
            return False
        ids = pl.get("track_ids", [])
        if track_id not in ids:
            return False
        pl["track_ids"] = [t for t in ids if t != track_id]
        _save_playlists(pls)
        return True


def playlist_tracks(playlist_id: str) -> list[dict]:
    """Tracks of a playlist in playlist order (skips ids no longer in the library)."""
    _ensure_loaded()
    with _LOCK:
        pls = _load_playlists()
        pl = pls["playlists"].get(playlist_id)
        if pl is None:
            return []
        ids = list(pl.get("track_ids", []))
        lib = _load_library()
        out: list[dict] = []
        for tid in ids:
            raw = lib["tracks"].get(tid)
            if raw:
                out.append(_normalise_track(tid, raw))
    return out


# ─── Legacy migration ─────────────────────────────────────────────────────────

def migrate_legacy() -> int:
    """
    One-time best-effort import of the old stores into the library.

      FAVOURITES_FILE  {key: {url, title|description, local_path}}  → favourite=True
      MUSIC_PREFS_FILE {key: {rating, title, play_count, last_played}}
                       → rating=="liked" sets liked=True

    Idempotent: a track id already present in the library is updated in place
    (flags merged) rather than duplicated, so re-running causes no harm.
    Returns the number of legacy entries processed. Never raises.
    """
    imported = 0
    with _LOCK:
        lib = _load_library()
        tracks = lib["tracks"]

        # --- favourites.json ---
        try:
            if FAVOURITES_FILE.exists():
                favs = json.loads(FAVOURITES_FILE.read_text(encoding="utf-8"))
                if isinstance(favs, dict):
                    for key, entry in favs.items():
                        if not isinstance(entry, dict):
                            continue
                        title = (entry.get("title") or entry.get("description")
                                 or str(key)).strip()
                        url = (entry.get("url") or "").strip()
                        local = entry.get("local_path") or ""
                        local = str(local) if local else ""
                        tid = _track_id(url, title, "")
                        track = _normalise_track(tid, tracks.get(tid)) \
                            if tid in tracks else _blank_track(tid)
                        if not track["title"]:
                            track["title"] = title
                        if url and not track["source_url"]:
                            track["source_url"] = url
                        if local and not track["local_path"]:
                            track["local_path"] = local
                        if not track["added_ts"]:
                            track["added_ts"] = time.time()
                        track["favourite"] = True
                        tracks[tid] = track
                        imported += 1
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            log.warning("music_library: favourites migration error: %s", exc)

        # --- music_prefs.json ---
        try:
            if MUSIC_PREFS_FILE.exists():
                prefs = json.loads(MUSIC_PREFS_FILE.read_text(encoding="utf-8"))
                if isinstance(prefs, dict):
                    for key, entry in prefs.items():
                        if not isinstance(entry, dict):
                            continue
                        title = (entry.get("title") or str(key)).strip()
                        tid = _track_id("", title, "")
                        track = _normalise_track(tid, tracks.get(tid)) \
                            if tid in tracks else _blank_track(tid)
                        if not track["title"]:
                            track["title"] = title
                        if entry.get("rating") == "liked":
                            track["liked"] = True
                        try:
                            track["play_count"] = max(
                                int(track.get("play_count", 0)),
                                int(entry.get("play_count", 0)),
                            )
                        except (TypeError, ValueError):
                            pass
                        try:
                            track["last_played"] = max(
                                float(track.get("last_played", 0.0)),
                                float(entry.get("last_played", 0.0)),
                            )
                        except (TypeError, ValueError):
                            pass
                        if not track["added_ts"]:
                            track["added_ts"] = track.get("last_played") or time.time()
                        tracks[tid] = track
                        imported += 1
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            log.warning("music_library: prefs migration error: %s", exc)

        if imported:
            _save_library(lib)

        # Archive the legacy stores so the next launch can't re-import them and
        # resurrect tracks the user has since deleted (data is already merged in).
        for legacy in (FAVOURITES_FILE, MUSIC_PREFS_FILE):
            try:
                if legacy.exists():
                    legacy.replace(legacy.with_suffix(legacy.suffix + ".migrated"))
            except OSError as exc:
                log.warning("music_library: could not archive %s: %s", legacy, exc)
    log.info("music_library: migrated %d legacy entr%s",
             imported, "y" if imported == 1 else "ies")
    return imported


# ─── Standalone smoke test (no network) ───────────────────────────────────────

if __name__ == "__main__":
    # Exercises CRUD + playlists against the REAL files, then cleans up after
    # itself so the user's library is left exactly as it was found.
    logging.basicConfig(level=logging.INFO)
    print("music_library self-test (offline)…")

    before_ids = {t["id"] for t in all_tracks()}
    before_pls = {p["id"] for p in list_playlists()}

    # add_track upsert + flag preservation
    t = add_track("ZZ Self Test Song", artist="Tester",
                  source="youtube", source_url="https://example.com/selftest",
                  duration=123)
    assert t["title"] == "ZZ Self Test Song", t
    assert t["artist"] == "Tester"
    assert t["duration"] == 123
    tid = t["id"]
    assert get_track(tid) is not None

    set_like(tid, True)
    set_favourite(tid, True)
    record_play(tid)
    record_play(tid)
    again = add_track("ZZ Self Test Song", artist="Tester",
                      source_url="https://example.com/selftest")
    assert again["liked"] is True, "like flag must survive re-add"
    assert again["favourite"] is True, "favourite flag must survive re-add"
    assert again["play_count"] == 2, ("play_count must survive re-add", again)

    # search + filters
    assert any(x["id"] == tid for x in list_tracks(search="self test")), "search failed"
    assert any(x["id"] == tid for x in list_tracks(filter="liked")), "liked filter failed"
    assert any(x["id"] == tid for x in list_tracks(filter="favourites")), "fav filter failed"
    assert not any(x["id"] == tid for x in list_tracks(filter="downloaded")), \
        "undownloaded track must not appear in 'downloaded'"

    # playlists
    pl = create_playlist("ZZ Self Test Playlist")
    pid = pl["id"]
    assert add_to_playlist(pid, tid) is True
    assert add_to_playlist(pid, tid) is True, "duplicate add should be idempotent"
    assert [x["id"] for x in playlist_tracks(pid)] == [tid], "playlist ordering wrong"
    assert any(p["id"] == pid and p["count"] == 1 for p in list_playlists())
    assert rename_playlist(pid, "ZZ Renamed") is True
    assert any(p["id"] == pid and p["name"] == "ZZ Renamed" for p in list_playlists())
    assert remove_from_playlist(pid, tid) is True
    assert playlist_tracks(pid) == []

    # cleanup — restore original state
    assert delete_playlist(pid) is True
    assert delete_track(tid, delete_file=False) is True

    after_ids = {t["id"] for t in all_tracks()}
    after_pls = {p["id"] for p in list_playlists()}
    # We only added/removed our own temp entries.
    assert after_ids - before_ids == set(), ("leaked track", after_ids - before_ids)
    assert after_pls - before_pls == set(), ("leaked playlist", after_pls - before_pls)

    print("music_library self-test: OK")
