"""
assistant_gui.py — AI Companion desktop app (WebEngine-based UI).

Layout: sidebar (Chat / Activity / Settings) + main view.
  • Chat      — clean message thread with markdown rendering
  • Activity  — feed of things the assistant did in the background
                (email sweeps, downloads organizing, briefs, nudges)
  • Settings  — everything user-configurable, persisted to data/settings.json
                (no more editing config.py / .env by hand)

Python ↔ JS via QWebChannel. The agent pipeline runs in a background
asyncio thread; thread-safe queues bridge the two worlds.
"""

from __future__ import annotations

import os

# ── Fix: Intel OMP duplicate-DLL crash on Windows ────────────────────────────
# torch (optional RVC) and ctranslate2 (faster-whisper) each ship their own
# libiomp5md.dll. Pre-aligning the two copies lets Windows reuse one handle.
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

# Smoother QtWebEngine rendering: GPU rasterization + animated scrolling cut the
# scroll tearing/jank in the chat and music lists. Must be set before QtWebEngine
# initialises; setdefault so a user-provided env value still wins.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--enable-gpu-rasterization --enable-smooth-scrolling",
)
if os.name == "nt":
    try:
        import hashlib
        import importlib.util
        import shutil
        from pathlib import Path as _P

        _ts  = importlib.util.find_spec("torch")
        _ct2 = importlib.util.find_spec("ctranslate2")
        if _ts and _ct2:
            _torch_omp = _P(_ts.origin).parent / "lib" / "libiomp5md.dll"
            _ct2_omp   = _P(_ct2.origin).parent / "libiomp5md.dll"
            if _torch_omp.exists() and _ct2_omp.exists():
                _md5 = lambda p: hashlib.md5(p.read_bytes()).hexdigest()
                if _md5(_torch_omp) != _md5(_ct2_omp):
                    shutil.copy2(str(_torch_omp), str(_ct2_omp))
        del _ts, _ct2, _P
    except Exception:
        pass

# ── Pre-load Whisper before Qt WebEngine initialises ─────────────────────────
# ctranslate2's CUDA init conflicts with Qt WebEngine's GPU context if it
# happens later — loading the model here avoids the crash entirely.
try:
    import config as _cfg_stt
    import stt as _stt_mod
    from faster_whisper import WhisperModel as _WM
    _stt_mod._preloaded_model = _WM(
        _cfg_stt.WHISPER_MODEL_SIZE,
        device=_cfg_stt.WHISPER_DEVICE,
        compute_type=_cfg_stt.WHISPER_COMPUTE_TYPE,
        download_root=str(_cfg_stt.MODELS_DIR / "whisper"),
    )
    del _WM, _cfg_stt
except Exception as _e:
    import sys as _sys
    print(f"[assistant_gui] Whisper pre-load failed: {_e} — will retry in agent thread",
          file=_sys.stderr)
    del _sys

import asyncio
import collections
import json
import queue
import sys
import threading
from pathlib import Path

from PyQt5 import QtCore, QtGui
from PyQt5.QtWidgets import (
    QAction, QApplication, QMainWindow, QMenu, QSystemTrayIcon,
)
from PyQt5.QtCore import QObject, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings
from PyQt5.QtWebChannel import QWebChannel

import settings as user_settings

# ─── Thread-safe bridges: GUI ↔ asyncio agent ─────────────────────────────────
_text_in:    queue.Queue[str]                  = queue.Queue()
_text_out:   queue.Queue[tuple[str, str]]      = queue.Queue()
_status_out: queue.Queue[str]                  = queue.Queue()  # idle|thinking|listening|speaking
_feed_out:   queue.Queue[tuple[str, str]]      = queue.Queue()  # (title, text)
_music_out:  queue.Queue[str]                  = queue.Queue()  # JSON payloads for onMusicEvent()
_video_out:  queue.Queue[str]                  = queue.Queue()  # JSON payloads for onVideoEvent()
_tool_out:   queue.Queue[str]                  = queue.Queue()  # JSON payloads for onToolEvent()

# Serialises potentially-slow player operations (play / download) launched from
# the GUI thread so two clicks can't fight over the single mpv process.
_player_lock = threading.Lock()


def push_feed(title: str, text: str) -> None:
    """Thread-safe: add a card to the Activity feed."""
    _feed_out.put((title, text))


def push_music_event(event: dict) -> None:
    """Thread-safe: notify the music UI (download progress, now-playing, etc.)."""
    try:
        _music_out.put(json.dumps(event))
    except (TypeError, ValueError):
        pass


def push_video_event(event: dict) -> None:
    """Thread-safe: notify the Video UI (probe results, download progress)."""
    try:
        _video_out.put(json.dumps(event))
    except (TypeError, ValueError):
        pass


def push_tool_event(event: dict) -> None:
    """Thread-safe: notify the Tools UI (background-eraser progress / result)."""
    try:
        _tool_out.put(json.dumps(event))
    except (TypeError, ValueError):
        pass


def _image_data_uri(path: str, max_px: int = 1100) -> str:
    """Read an image, downscale for preview, return a base64 PNG data URI.

    Used so the WebEngine page can show local images (and transparent cutouts)
    without needing file:// access. Returns "" on any failure.
    """
    import base64
    import io
    try:
        from PIL import Image  # Pillow is a hard dependency already
        with Image.open(path) as im:
            im = im.convert("RGBA")
            if max(im.size) > max_px:
                im.thumbnail((max_px, max_px), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return "data:image/png;base64," + b64
    except Exception as exc:  # noqa: BLE001 - preview is best-effort
        import logging as _log
        _log.getLogger("gui").debug("image preview failed for %s: %s", path, exc)
        return ""


# ─── JS ↔ Python bridge ──────────────────────────────────────────────────────

class _Bridge(QObject):
    """Exposed to JS via QWebChannel."""

    @pyqtSlot(str)
    def send_message(self, text: str) -> None:
        if text.strip():
            _text_in.put(text.strip())

    @pyqtSlot(result=str)
    def get_settings(self) -> str:
        return json.dumps(user_settings.all())

    @pyqtSlot(result=str)
    def get_automation_status(self) -> str:
        try:
            from automation_engine import status_snapshot
            return json.dumps(status_snapshot())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    @pyqtSlot(str, result=str)
    def save_settings(self, payload: str) -> str:
        try:
            user_settings.update(json.loads(payload))
            # Re-read keys/toggles so changes (esp. a freshly pasted API key)
            # take effect immediately — no restart. stream_llm reads
            # config.LLM_PROVIDERS live, so the AI works on the next message.
            try:
                import config
                config.refresh_runtime()
            except Exception as exc:
                import logging as _log
                _log.getLogger("gui").warning("config.refresh_runtime failed: %s", exc)
            return "ok"
        except Exception as exc:
            return f"error: {exc}"

    # ─── Updates ────────────────────────────────────────────────────────────────
    # Settings → Updates pulls the latest source from GitHub and rebuilds the exe.
    # The multi-minute rebuild runs in a detached process (updater.py) so it can
    # replace the running exe; user data/models/keys are preserved across it.

    @pyqtSlot(result=str)
    def check_update(self) -> str:
        try:
            import update_manager
            return json.dumps(update_manager.check_for_update())
        except Exception as exc:
            return json.dumps({"ok": False, "ready": False, "available": False,
                               "reason": f"Update check failed: {exc}"})

    @pyqtSlot(result=str)
    def apply_update(self) -> str:
        try:
            import update_manager
            res = update_manager.start_update(os.getpid())
            if res.get("ok"):
                # Give JS a moment to show the "updating…" message, then quit so
                # the rebuild can overwrite the exe we're running from.
                QTimer.singleShot(1500, QApplication.quit)
            return json.dumps(res)
        except Exception as exc:
            return json.dumps({"ok": False, "reason": f"Could not start update: {exc}"})

    @pyqtSlot(result=str)
    def last_update_note(self) -> str:
        """Return + clear the marker left by a finished update (for a toast)."""
        try:
            import config
            marker = config.DATA_DIR / "last_update.json"
            if marker.exists():
                note = marker.read_text(encoding="utf-8")
                marker.unlink(missing_ok=True)
                return note
        except Exception:
            pass
        return ""

    # ─── Music ────────────────────────────────────────────────────────────────
    # The mini music player UI talks to these. They return JSON strings (like
    # get_settings) and never raise. Slow operations (play that may need a
    # download, and explicit downloads) run on a background thread and report
    # progress back through push_music_event() → onMusicEvent() in the page.

    @pyqtSlot(str, result=str)
    def music_list(self, payload: str) -> str:
        import os
        from actions import music_library
        from actions.music import get_player
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            req = {}
        flt = req.get("filter") or "all"
        search = req.get("search") or ""
        playlist_id = req.get("playlist_id") or ""
        try:
            tracks = music_library.list_tracks(
                filter=flt, search=search, playlist_id=playlist_id
            )
            out = []
            for t in tracks:
                d = dict(t)
                lp = d.get("local_path") or ""
                d["downloaded"] = bool(lp and os.path.exists(lp))
                out.append(d)
            return json.dumps({
                "tracks": out,
                "playlists": music_library.list_playlists(),
                "filter": flt,
                "now": get_player().now_playing(),
            })
        except Exception as exc:
            return json.dumps({"tracks": [], "playlists": [], "filter": flt,
                               "error": str(exc)})

    @pyqtSlot(result=str)
    def music_state(self) -> str:
        from actions.music import get_player
        try:
            return json.dumps(get_player().now_playing())
        except Exception as exc:
            return json.dumps({"playing": False, "error": str(exc)})

    @pyqtSlot(str, result=str)
    def music_action(self, payload: str) -> str:
        from actions import music_library
        from actions.music import get_player
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        cmd = (req.get("cmd") or "").strip()
        player = get_player()
        try:
            # — slow operations run off the GUI thread —
            if cmd == "play":
                queue_ids = req.get("queue") or []
                index = int(req.get("index", 0) or 0)
                track_id = req.get("track_id") or ""
                query = req.get("query") or ""
                if queue_ids:
                    self._player_async(lambda: player.play_queue(queue_ids, index))
                elif track_id:
                    self._player_async(lambda: player.play(track_id=track_id))
                elif query:
                    self._player_async(lambda: player.play(query=query))
                else:
                    return json.dumps({"ok": False, "error": "nothing to play"})
                return json.dumps({"ok": True, "status": "loading",
                                   "now": player.now_playing()})
            if cmd == "next":
                self._player_async(player.next)
                return json.dumps({"ok": True, "status": "loading"})
            if cmd == "prev":
                self._player_async(player.previous)
                return json.dumps({"ok": True, "status": "loading"})
            if cmd == "download":
                q = (req.get("query") or "").strip()
                if not q:
                    return json.dumps({"ok": False, "error": "empty query"})
                # track_id (optional) lets us "upgrade" an existing non-downloaded
                # library entry instead of leaving a duplicate stub behind.
                self._start_download(q, req.get("track_id") or "")
                return json.dumps({"ok": True, "status": "started"})
            if cmd == "play_collection":
                which = req.get("which") or "all"
                self._player_async(lambda: player.play_collection(which))
                return json.dumps({"ok": True, "status": "loading"})

            # — instant operations run inline —
            if cmd == "pause":
                player.pause()
            elif cmd == "resume":
                player.resume()
            elif cmd == "toggle":
                player.toggle_pause()
            elif cmd == "stop":
                player.stop()
            elif cmd == "volume":
                player.set_volume(int(req.get("level", 100)))
            elif cmd == "seek":
                player.seek(int(req.get("to", 0)))
            elif cmd == "shuffle":
                player.set_shuffle(req.get("on", "toggle"))
            elif cmd == "repeat":
                player.set_repeat(req.get("mode", "all"))
            elif cmd == "sleep":
                mins = req.get("minutes", 0)
                if str(mins).strip().lower() in ("off", "0", "cancel", ""):
                    player.cancel_sleep_timer()
                else:
                    player.set_sleep_timer(mins)
            elif cmd in ("like", "unlike"):
                music_library.set_like(req.get("track_id", ""), cmd == "like")
            elif cmd in ("favourite", "unfavourite"):
                music_library.set_favourite(req.get("track_id", ""), cmd == "favourite")
            elif cmd == "delete":
                tid = req.get("track_id", "")
                # Stop first if we're deleting the playing track, so Windows
                # releases the file handle and the mp3 can actually be removed.
                if tid and player.now_playing().get("track_id") == tid:
                    player.stop()
                music_library.delete_track(tid, delete_file=True)
            elif cmd == "create_playlist":
                pl = music_library.create_playlist(req.get("name") or "New Playlist")
                return json.dumps({"ok": True, "playlist": pl})
            elif cmd == "delete_playlist":
                music_library.delete_playlist(req.get("id", ""))
            elif cmd == "add_to_playlist":
                music_library.add_to_playlist(req.get("id", ""), req.get("track_id", ""))
            elif cmd == "remove_from_playlist":
                music_library.remove_from_playlist(req.get("id", ""), req.get("track_id", ""))
            else:
                return json.dumps({"ok": False, "error": f"unknown cmd: {cmd}"})
            return json.dumps({"ok": True, "now": player.now_playing()})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    def _player_async(self, fn) -> None:
        """Run a (possibly slow) player call off the GUI thread, then notify the UI."""
        def _worker() -> None:
            from actions.music import get_player
            with _player_lock:
                try:
                    fn()
                except Exception as exc:
                    push_music_event({"type": "download_error", "error": str(exc)})
                    return
            now = get_player().now_playing()
            # Flatten now-playing fields to the top level (what onMusicEvent reads)
            # while keeping a nested copy for any other consumer.
            push_music_event({"type": "now_playing", "now": now, **now})
        threading.Thread(target=_worker, name="music-play", daemon=True).start()

    def _start_download(self, query: str, replace_id: str = "") -> None:
        """Download a track in the background; report progress via onMusicEvent.

        If replace_id names an existing (not-yet-downloaded) library entry that
        resolves to a different id, carry over its like / favourite state and
        playlist membership, then drop the stub so the library stays tidy.
        """
        def _worker() -> None:
            from actions import music_library
            from actions.music import download
            push_music_event({"type": "download_started", "query": query})
            try:
                track = download(query)
            except Exception as exc:
                push_music_event({"type": "download_error", "query": query,
                                  "error": str(exc)})
                return
            if not track:
                push_music_event({"type": "download_error", "query": query,
                                  "error": "Download failed — check yt-dlp, ffmpeg, "
                                           "and your connection."})
                return
            if replace_id and replace_id != track["id"]:
                try:
                    old = music_library.get_track(replace_id)
                    if old:
                        if old.get("liked"):
                            music_library.set_like(track["id"], True)
                        if old.get("favourite"):
                            music_library.set_favourite(track["id"], True)
                        for pl in music_library.list_playlists():
                            ids = [t["id"] for t in music_library.playlist_tracks(pl["id"])]
                            if replace_id in ids:
                                music_library.add_to_playlist(pl["id"], track["id"])
                        music_library.delete_track(replace_id, delete_file=False)
                except Exception:
                    pass  # reconciliation is best-effort
            push_music_event({"type": "download_done",
                              "title": track.get("title", ""), "track": track})
        threading.Thread(target=_worker, name="music-download", daemon=True).start()

    # ─── Video downloader ───────────────────────────────────────────────────────
    # video_probe + video_download run off the GUI thread (network / ffmpeg) and
    # report back via push_video_event() → onVideoEvent() in the page.

    @pyqtSlot(str, result=str)
    def video_probe(self, payload: str) -> str:
        """Inspect a link and report its available resolutions (background)."""
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            req = {}
        url = (req.get("url") or "").strip()
        if not url:
            return json.dumps({"ok": False, "error": "Paste a video link first."})

        def _worker() -> None:
            from actions.video import probe
            push_video_event({"type": "probing", "url": url})
            info = probe(url)
            if info.get("ok"):
                info["type"] = "formats"
                push_video_event(info)
            else:
                push_video_event({"type": "probe_error",
                                  "error": info.get("error", "Couldn't read that link.")})
        threading.Thread(target=_worker, name="video-probe", daemon=True).start()
        return json.dumps({"ok": True, "status": "probing"})

    @pyqtSlot(str, result=str)
    def video_download(self, payload: str) -> str:
        """Download a video at the chosen resolution (background, with progress)."""
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        url = (req.get("url") or "").strip()
        if not url:
            return json.dumps({"ok": False, "error": "No link to download."})
        height = req.get("height")
        try:
            height = int(height) if height not in (None, "", "best") else None
        except (TypeError, ValueError):
            height = None
        fmt = req.get("format") or ""
        audio_only = bool(req.get("audio_only"))

        def _worker() -> None:
            from actions.video import download
            push_video_event({"type": "video_started", "url": url})
            last = {"pct": -1}

            def _progress(d: dict) -> None:
                pct = int(d.get("pct", 0) or 0)
                # only emit on whole-percent changes / status flips to avoid flooding
                if pct != last["pct"] or d.get("status") in ("processing", "done"):
                    last["pct"] = pct
                    push_video_event({"type": "video_progress", **d})

            res = download(url, height=height, format_selector=fmt,
                           audio_only=audio_only, on_progress=_progress)
            if res.get("ok"):
                push_video_event({"type": "video_done", "title": res.get("title", ""),
                                  "path": res.get("path", ""),
                                  "filename": res.get("filename", "")})
            else:
                push_video_event({"type": "video_error",
                                  "error": res.get("error", "Download failed.")})
        threading.Thread(target=_worker, name="video-download", daemon=True).start()
        return json.dumps({"ok": True, "status": "started"})

    @pyqtSlot(result=str)
    def video_list(self) -> str:
        from actions.video import list_downloaded
        from config import VIDEO_CACHE_DIR
        try:
            return json.dumps({"ok": True, "videos": list_downloaded(),
                               "dir": str(VIDEO_CACHE_DIR)})
        except Exception as exc:
            return json.dumps({"ok": False, "videos": [], "error": str(exc)})

    @pyqtSlot(str, result=str)
    def video_play(self, payload: str) -> str:
        """Return a localhost URL the in-app <video> player can stream + seek."""
        import os as _os
        from config import VIDEO_CACHE_DIR
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        path = (req.get("path") or "").strip()
        if not path or not _os.path.exists(path):
            return json.dumps({"ok": False, "error": "File not found."})
        try:
            import media_server
            url = media_server.url_for(path, str(VIDEO_CACHE_DIR))
            # Auto-attach a sidecar subtitle (movie.srt / movie.vtt next to it).
            sub_url = None
            stem = Path(path).with_suffix("")
            for ext in (".vtt", ".srt"):
                cand = Path(str(stem) + ext)
                if cand.exists():
                    try:
                        import subtitles
                        vtt = subtitles.to_vtt(cand, str(VIDEO_CACHE_DIR))
                        sub_url = media_server.url_for(vtt, str(VIDEO_CACHE_DIR))
                    except Exception:
                        sub_url = None
                    break
            return json.dumps({"ok": True, "url": url, "subtitle_url": sub_url})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    # ─── Audio output device (speaker chooser) ───────────────────────────────────

    @pyqtSlot(result=str)
    def list_speakers(self) -> str:
        try:
            import audio_devices
            return json.dumps({"ok": True, "devices": audio_devices.list_output_devices()})
        except Exception as exc:
            return json.dumps({"ok": False, "devices": [], "error": str(exc)})

    @pyqtSlot(str, result=str)
    def test_speaker(self, payload: str) -> str:
        """Play a short tone on the given device name (preview before saving)."""
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            req = {}
        name = (req.get("name") or "").strip()

        def _beep() -> None:
            try:
                import numpy as _np
                import sounddevice as _sd
                import audio_devices
                idx = audio_devices.resolve_output_index(name) if name else None
                sr = 44100
                t = _np.linspace(0, 0.35, int(sr * 0.35), endpoint=False)
                tone = (0.2 * _np.sin(2 * _np.pi * 660 * t)).astype("float32")
                _sd.play(tone, samplerate=sr, device=idx)
                _sd.wait()
            except Exception as exc:
                import logging as _log
                _log.getLogger("gui").warning("speaker test failed: %s", exc)

        threading.Thread(target=_beep, name="spk-test", daemon=True).start()
        return json.dumps({"ok": True})

    # ─── Subtitles (in-app video player) ─────────────────────────────────────────

    @pyqtSlot(result=str)
    def pick_subtitle(self) -> str:
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            None, "Choose a subtitle file", "", "Subtitles (*.srt *.vtt)")
        if not path:
            return json.dumps({"ok": False, "cancelled": True})
        return json.dumps({"ok": True, "path": path, "name": Path(path).name})

    @pyqtSlot(str, result=str)
    def add_subtitle(self, payload: str) -> str:
        """Convert a chosen .srt/.vtt to WebVTT and return a player URL for it."""
        import os as _os
        from config import VIDEO_CACHE_DIR
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        sub = (req.get("path") or "").strip()
        if not sub or not _os.path.exists(sub):
            return json.dumps({"ok": False, "error": "Subtitle file not found."})
        try:
            import media_server
            import subtitles
            vtt = subtitles.to_vtt(sub, str(VIDEO_CACHE_DIR))
            url = media_server.url_for(vtt, str(VIDEO_CACHE_DIR))
            return json.dumps({"ok": True, "url": url, "name": Path(sub).name})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    # ─── Toolbox: media / image / text utilities + auto-subtitles ────────────────

    @pyqtSlot(str, result=str)
    def pick_file(self, payload: str) -> str:
        from PyQt5.QtWidgets import QFileDialog
        try:
            kind = (json.loads(payload).get("kind") if payload else "") or ""
        except Exception:
            kind = ""
        filt = {
            "video": "Video / audio (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.mp3 *.wav "
                     "*.m4a *.flac *.ogg *.opus);;All files (*.*)",
            "image": "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.heic "
                     "*.heif);;All files (*.*)",
        }.get(kind, "All files (*.*)")
        path, _ = QFileDialog.getOpenFileName(None, "Choose a file", "", filt)
        if not path:
            return json.dumps({"ok": False, "cancelled": True})
        return json.dumps({"ok": True, "path": path, "name": Path(path).name})

    def _run_job(self, job_id: str, fn) -> None:
        """Run fn() on a bg thread and push its result dict as a tool 'job' event."""
        def _worker() -> None:
            push_tool_event({"type": "job", "id": job_id, "status": "running"})
            try:
                res = fn() or {}
            except Exception as exc:
                res = {"ok": False, "error": str(exc)}
            res.update({"type": "job", "id": job_id, "status": "done"})
            push_tool_event(res)
        threading.Thread(target=_worker, name=f"tool-{job_id}", daemon=True).start()

    @pyqtSlot(str, result=str)
    def gen_subtitles(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        path = (req.get("path") or "").strip()
        lang = (req.get("lang") or "").strip() or None
        job = (req.get("job") or "subs").strip() or "subs"
        if not path:
            return json.dumps({"ok": False, "error": "Choose a video/audio file."})

        def _go():
            from actions.subtitle_gen import generate_srt
            return generate_srt(path, language=lang, on_event=lambda m: push_tool_event(
                {"type": "job", "id": job, "status": "running", "msg": m}))
        self._run_job(job, _go)
        return json.dumps({"ok": True, "status": "running"})

    @pyqtSlot(str, result=str)
    def media_tool(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        op, path = (req.get("op") or "").strip(), (req.get("path") or "").strip()
        if not path:
            return json.dumps({"ok": False, "error": "Choose a file."})

        def _go():
            from actions import media_tools as m
            if op == "compress": return m.compress_video(path, int(req.get("crf", 28)))
            if op == "convert":  return m.convert(path, req.get("ext", "mp4"))
            if op == "trim":     return m.trim(path, req.get("start", "0"), req.get("end") or None)
            if op == "gif":      return m.to_gif(path, req.get("start", "0"),
                                                 float(req.get("duration", 5)),
                                                 int(req.get("fps", 12)), int(req.get("width", 480)))
            if op == "audio":    return m.extract_audio(path, req.get("ext", "mp3"))
            return {"ok": False, "error": f"Unknown op {op}"}
        self._run_job("media", _go)
        return json.dumps({"ok": True, "status": "running"})

    @pyqtSlot(str, result=str)
    def image_tool(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        op, path = (req.get("op") or "").strip(), (req.get("path") or "").strip()
        if not path:
            return json.dumps({"ok": False, "error": "Choose an image."})

        def _go():
            from actions import image_tools as im
            if op == "compress":  return im.compress(path, int(req.get("quality", 70)))
            if op == "convert":   return im.convert(path, req.get("ext", "png"))
            if op == "resize":    return im.resize(path, int(req.get("max_px", 1280)))
            if op == "watermark": return im.watermark(path, req.get("text", ""))
            return {"ok": False, "error": f"Unknown op {op}"}
        self._run_job("image", _go)
        return json.dumps({"ok": True, "status": "running"})

    @pyqtSlot(str, result=str)
    def text_tool(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        op = (req.get("op") or "").strip()
        text = req.get("text", "") or ""

        def _go():
            from actions import text_tools as t
            if op == "grammar":           return t.fix_grammar(text)
            if op == "rewrite":           return t.rewrite(text, req.get("style", "clearer and more concise"))
            if op == "translate":         return t.translate(text, req.get("lang", "English"))
            if op == "summarize":         return t.summarize(text)
            if op == "summarize_url":     return t.summarize_url(req.get("url", ""))
            if op == "summarize_youtube": return t.summarize_youtube(req.get("url", ""))
            return {"ok": False, "error": f"Unknown op {op}"}
        self._run_job("text", _go)
        return json.dumps({"ok": True, "status": "running"})

    @pyqtSlot(str, result=str)
    def pick_files(self, payload: str) -> str:
        """Multi-select picker (merge PDFs, images→PDF)."""
        from PyQt5.QtWidgets import QFileDialog
        try:
            kind = (json.loads(payload).get("kind") if payload else "") or ""
        except Exception:
            kind = ""
        filt = {
            "pdf": "PDF files (*.pdf);;All files (*.*)",
            "image": "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;All files (*.*)",
        }.get(kind, "All files (*.*)")
        paths, _ = QFileDialog.getOpenFileNames(None, "Choose files", "", filt)
        if not paths:
            return json.dumps({"ok": False, "cancelled": True})
        return json.dumps({"ok": True, "paths": paths, "names": [Path(p).name for p in paths]})

    @pyqtSlot(str, result=str)
    def pdf_tool(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        op = (req.get("op") or "").strip()
        path = (req.get("path") or "").strip()
        paths = req.get("paths") or ([path] if path else [])

        def _go():
            from actions import pdf_tools as P
            if op == "merge":         return P.merge(paths)
            if op == "split":         return P.split_all(path)
            if op == "images_to_pdf": return P.images_to_pdf(paths)
            if op == "to_images":     return P.pdf_to_images(path)
            if op == "compress":      return P.compress(path)
            if op == "rotate":        return P.rotate(path, int(req.get("degrees", 90)))
            if op == "unlock":        return P.unlock(path, req.get("password", ""))
            if op == "text":          return P.extract_text(path)
            return {"ok": False, "error": f"Unknown op {op}"}
        self._run_job("pdf", _go)
        return json.dumps({"ok": True, "status": "running"})

    @pyqtSlot(str, result=str)
    def qr_generate(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            req = {}
        from actions import qr_tools
        return json.dumps(qr_tools.generate(req.get("text", "")))

    @pyqtSlot(str, result=str)
    def qr_scan(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            req = {}
        from actions import qr_tools
        return json.dumps(qr_tools.scan(req.get("path", "")))

    @pyqtSlot(str, result=str)
    def doc_ingest(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        path = (req.get("path") or "").strip()
        if not path:
            return json.dumps({"ok": False, "error": "Choose a document."})
        self._run_job("doc", lambda: __import__("actions.doc_chat", fromlist=["ingest"]).ingest(path))
        return json.dumps({"ok": True, "status": "running"})

    @pyqtSlot(str, result=str)
    def doc_ask(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        q = (req.get("q") or "").strip()
        if not q:
            return json.dumps({"ok": False, "error": "Type a question."})
        self._run_job("docans", lambda: __import__("actions.doc_chat", fromlist=["ask"]).ask(q))
        return json.dumps({"ok": True, "status": "running"})

    @pyqtSlot(str, result=str)
    def upscale_image(self, payload: str) -> str:
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        path = (req.get("path") or "").strip()
        if not path:
            return json.dumps({"ok": False, "error": "Choose an image."})

        def _go():
            from actions.upscale import upscale
            return upscale(path, on_event=lambda m: push_tool_event(
                {"type": "job", "id": "upscale", "status": "running", "msg": m}))
        self._run_job("upscale", _go)
        return json.dumps({"ok": True, "status": "running"})

    @pyqtSlot(str, result=str)
    def video_delete(self, payload: str) -> str:
        from actions.video import delete_download
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        ok = delete_download(req.get("path", ""))
        return json.dumps({"ok": bool(ok)})

    @pyqtSlot(str, result=str)
    def open_path(self, payload: str) -> str:
        """Open a downloaded file (or its containing folder) in the OS."""
        import os as _os
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        path = (req.get("path") or "").strip()
        reveal = bool(req.get("reveal"))
        if not path or not _os.path.exists(path):
            return json.dumps({"ok": False, "error": "File not found."})
        try:
            target = _os.path.dirname(path) if reveal else path
            if reveal and hasattr(_os, "startfile"):
                # select the file inside Explorer when revealing
                import subprocess as _sp
                _sp.Popen(["explorer", "/select,", _os.path.normpath(path)])
            elif hasattr(_os, "startfile"):
                _os.startfile(target)  # type: ignore[attr-defined]  # Windows
            else:
                import subprocess as _sp
                _sp.Popen(["xdg-open", target])
            return json.dumps({"ok": True})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    # ─── Background eraser (Tools) ───────────────────────────────────────────────

    @pyqtSlot(result=str)
    def pick_image(self) -> str:
        """Open a native file picker (GUI thread) and return the chosen image
        plus a downscaled base64 preview the page can display."""
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            None, "Choose an image",
            "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)")
        if not path:
            return json.dumps({"ok": False, "cancelled": True})
        return json.dumps({"ok": True, "path": path, "name": Path(path).name,
                           "data_uri": _image_data_uri(path)})

    @pyqtSlot(str, result=str)
    def erase_background(self, payload: str) -> str:
        """Remove an image's background locally (background thread)."""
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        path = (req.get("path") or "").strip()
        if not path:
            return json.dumps({"ok": False, "error": "Choose an image first."})

        def _worker() -> None:
            from actions.background_eraser import remove_background
            push_tool_event({"type": "erase_started"})
            res = remove_background(
                path, on_event=lambda m: push_tool_event({"type": "erase_status", "msg": m}))
            if res.get("ok"):
                out = res.get("output", "")
                push_tool_event({"type": "erase_done", "output": out,
                                 "data_uri": _image_data_uri(out)})
            else:
                push_tool_event({"type": "erase_error",
                                 "error": res.get("error", "Failed.")})
        threading.Thread(target=_worker, name="bg-eraser", daemon=True).start()
        return json.dumps({"ok": True, "status": "processing"})

    @pyqtSlot(str, result=str)
    def save_result(self, payload: str) -> str:
        """Copy a generated file (e.g. a cutout) to a user-chosen location."""
        import shutil as _shutil
        from PyQt5.QtWidgets import QFileDialog
        try:
            req = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"ok": False, "error": "bad payload"})
        src = (req.get("path") or "").strip()
        if not src or not Path(src).exists():
            return json.dumps({"ok": False, "error": "Nothing to save yet."})
        suggested = req.get("suggested") or Path(src).name
        dest, _ = QFileDialog.getSaveFileName(None, "Save image", suggested,
                                              "PNG image (*.png)")
        if not dest:
            return json.dumps({"ok": False, "cancelled": True})
        try:
            _shutil.copyfile(src, dest)
            return json.dumps({"ok": True, "saved": dest})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})


# ─── UI loading ──────────────────────────────────────────────────────────────
# The chat interface lives in ui.html (project root) so the UI can be edited
# and reloaded without touching Python code.

# ui.html is bundled as a data file in the frozen build; PyInstaller unpacks it
# to sys._MEIPASS. Fall back to the source dir when running from source.
_UI_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
_UI_FILE = _UI_BASE / "ui.html"


def _load_app_html() -> str:
    try:
        return _UI_FILE.read_text(encoding="utf-8")
    except Exception as exc:
        return (
            "<html><body style=\'font-family:sans-serif;padding:40px;"
            "background:#262624;color:#eceae4\'>"
            "<h2>ui.html is missing</h2>"
            f"<p>Could not load the interface: {exc}</p></body></html>"
        )


_APP_HTML = _load_app_html()


# ─── Main window ─────────────────────────────────────────────────────────────

class ChatWindow(QMainWindow):
    _msg_ready    = pyqtSignal(str, str)
    _status_ready = pyqtSignal(str)
    _feed_ready   = pyqtSignal(str, str)
    _music_ready  = pyqtSignal(str)
    _video_ready  = pyqtSignal(str)
    _tool_ready   = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI Companion")
        self.setMinimumSize(560, 600)
        self.resize(860, 680)

        self._view    = QWebEngineView()
        _vs = self._view.settings()
        _vs.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, True)
        # Let the setHtml (qrc) page stream video from the localhost media server,
        # and start playback from a click without an extra gesture prompt.
        _vs.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        _vs.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
        # Allow the HTML5 video player to go true (OS) fullscreen, not just
        # maximized. We must also honour the page's fullscreen request below.
        _vs.setAttribute(QWebEngineSettings.FullScreenSupportEnabled, True)
        self._was_maximized = False
        self._bridge  = _Bridge()
        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)
        self._view.page().fullScreenRequested.connect(self._on_fullscreen_requested)
        self._view.setHtml(_APP_HTML, QUrl("qrc:///"))
        self.setCentralWidget(self._view)

        self._msg_ready.connect(self._push_msg)
        self._status_ready.connect(self._push_status)
        self._feed_ready.connect(self._push_feed)
        self._music_ready.connect(self._push_music)
        self._video_ready.connect(self._push_video)
        self._tool_ready.connect(self._push_tool)

        poll = QTimer(self)
        poll.timeout.connect(self._drain)
        poll.start(100)

    # ─── Push to JS ──────────────────────────────────────────────────────────

    def _push_msg(self, role: str, text: str) -> None:
        is_voice = "(voice)" in role
        js = f"addMsg({json.dumps(role)}, {json.dumps(text)}, {str(is_voice).lower()})"
        self._view.page().runJavaScript(js)

    def _push_status(self, state: str) -> None:
        self._view.page().runJavaScript(f"setStatus({json.dumps(state)})")

    def _push_feed(self, title: str, text: str) -> None:
        self._view.page().runJavaScript(
            f"addFeed({json.dumps(title)}, {json.dumps(text)})")

    def _push_music(self, payload: str) -> None:
        # payload is already a JSON string; onMusicEvent() accepts a string.
        self._view.page().runJavaScript(f"onMusicEvent({json.dumps(payload)})")

    def _push_video(self, payload: str) -> None:
        self._view.page().runJavaScript(f"onVideoEvent({json.dumps(payload)})")

    def _push_tool(self, payload: str) -> None:
        self._view.page().runJavaScript(f"onToolEvent({json.dumps(payload)})")

    # ─── Queue drain (QTimer, main thread) ───────────────────────────────────

    def _drain(self) -> None:
        while not _text_out.empty():
            try:
                role, text = _text_out.get_nowait()
                self._msg_ready.emit(role, text)
            except queue.Empty:
                break
        while not _status_out.empty():
            try:
                self._status_ready.emit(_status_out.get_nowait())
            except queue.Empty:
                break
        while not _feed_out.empty():
            try:
                title, text = _feed_out.get_nowait()
                self._feed_ready.emit(title, text)
            except queue.Empty:
                break
        while not _music_out.empty():
            try:
                self._music_ready.emit(_music_out.get_nowait())
            except queue.Empty:
                break
        while not _video_out.empty():
            try:
                self._video_ready.emit(_video_out.get_nowait())
            except queue.Empty:
                break
        while not _tool_out.empty():
            try:
                self._tool_ready.emit(_tool_out.get_nowait())
            except queue.Empty:
                break

    def _on_fullscreen_requested(self, request) -> None:
        """Honour the <video> player's fullscreen button with real OS fullscreen."""
        request.accept()
        if request.toggleOn():
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        elif self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        event.ignore()
        self.hide()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        # QtWebEngine can lose its GPU surface while minimized and come back
        # blank. When we leave the minimized state, force the web view to
        # re-render so the UI doesn't restore to a dead/white page.
        if event.type() == QtCore.QEvent.WindowStateChange:
            if not self.isMinimized():
                self._view.hide()
                self._view.show()
                self._view.update()
        super().changeEvent(event)

    def restore(self) -> None:
        """Bring the window back cleanly from tray / minimized state."""
        self.setWindowState(
            (self.windowState() & ~QtCore.Qt.WindowMinimized) | QtCore.Qt.WindowActive
        )
        self.showNormal()
        self.raise_()
        self.activateWindow()


def _make_tray_icon() -> QtGui.QIcon:
    """Draw a simple, professional tray icon (no PNG assets needed)."""
    pix = QtGui.QPixmap(64, 64)
    pix.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pix)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setBrush(QtGui.QColor("#d97757"))
    p.setPen(QtCore.Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QtGui.QColor("#ffffff"))
    font = QtGui.QFont("Segoe UI", 28, QtGui.QFont.Bold)
    p.setFont(font)
    initial = (user_settings.get("general.companion_name", "A") or "A")[0].upper()
    p.drawText(pix.rect(), QtCore.Qt.AlignCenter, initial)
    p.end()
    return QtGui.QIcon(pix)


# ─── Background agent ─────────────────────────────────────────────────────────

def _run_agent(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_agent_main())


async def _agent_main() -> None:
    """Full agent pipeline running in the asyncio background thread."""
    import config
    config.setup_logging()

    # Local imports here so the GUI window can open before heavy models load
    from main import handle_turn, get_llm_response
    from memory import MemoryManager
    from tts_rvc import TTSEngine
    from stt import WhisperTranscriber
    from wake_word import WakeWordDetector
    from action_router import ActionRouter
    from actions.music import get_player
    from actions.discord_bot import DiscordAutoReplier
    from backup import auto_backup_if_needed

    # ── Subsystem init ────────────────────────────────────────────────────────
    memory_mgr   = MemoryManager()
    tts          = TTSEngine()
    transcriber  = WhisperTranscriber()
    detector     = WakeWordDetector()
    music_player = get_player()
    discord_bot  = DiscordAutoReplier(
        get_llm_response=get_llm_response,
        memory_mgr=memory_mgr,
    )
    router = ActionRouter(music_player=music_player, tts=tts, discord_bot=discord_bot)

    # ── Wire reminder manager callbacks ──────────────────────────────────────
    from actions.reminders import reminder_manager as _rmgr
    _rmgr.set_callbacks(
        speak_fn=tts.speak,
        notify_fn=lambda text: _text_out.put((config.COMPANION_NAME, text)),
    )

    history: collections.deque = collections.deque(
        maxlen=config.CONVERSATION_HISTORY_TURNS * 2
    )

    # ── Background automation engine → Activity feed ─────────────────────────
    automation_task = None
    try:
        from automation_engine import AutomationEngine
        engine = AutomationEngine(notify_fn=push_feed)
        automation_task = asyncio.create_task(engine.run())
    except Exception as _ae:
        import logging as _log
        _log.getLogger("gui").warning("Automation engine not started: %s", _ae)

    try:
        backup_path = await auto_backup_if_needed()
        if backup_path:
            import logging as _log
            _log.getLogger("gui").info("Auto-backup: %s", backup_path)
    except Exception:
        pass

    # Prevents TTS from overlapping when voice + text input arrive simultaneously
    _turn_lock = asyncio.Lock()
    # True only during a voice (hotkey) turn — typed chat is text-only, no audio.
    _voice_mode = False

    # ── Single turn handler — posts status + response to GUI queues ──────────
    async def _handle(user_text: str, voice: bool = False) -> None:
        nonlocal _voice_mode
        async with _turn_lock:
            _voice_mode = voice
            _status_out.put("thinking")
            _orig_speak = tts.speak
            async def _speak_and_post(text: str) -> None:
                if text.strip():
                    _text_out.put((config.COMPANION_NAME, text))
                if _voice_mode:
                    _status_out.put("speaking")
                    await _orig_speak(text)
                _status_out.put("idle")
            tts.speak = _speak_and_post  # type: ignore[method-assign]
            try:
                await handle_turn(user_text, memory_mgr, tts, router, history)
            finally:
                tts.speak = _orig_speak
                _voice_mode = False
                _status_out.put("idle")

    # ── GUI text-input background task ────────────────────────────────────────
    async def _gui_text_loop() -> None:
        while True:
            await asyncio.sleep(0.1)
            try:
                user_text = _text_in.get_nowait()
            except queue.Empty:
                continue
            await _handle(user_text, voice=False)  # text: chat only, no audio

    gui_task = asyncio.create_task(_gui_text_loop())

    # ── Startup message ───────────────────────────────────────────────────────
    if not config.LLM_PROVIDERS:
        _text_out.put((config.COMPANION_NAME,
            "Welcome! To get started, add a free API key in **Settings** "
            "(gear icon, bottom-left). Groq is the fastest — grab a key at "
            "console.groq.com, paste it in, save, and restart."))
    else:
        if config.PERSONALITY_MODE == "professional":
            greeting = "Ready when you are. Type below, or press the voice hotkey to talk."
        else:
            greeting = "Hey, I'm here. Type below or use the voice hotkey whenever."
        _text_out.put((config.COMPANION_NAME, greeting))

    # ── Hotkey / voice loop (runs until quit) ─────────────────────────────────
    while True:
        await detector.wait_for_wake_word()
        if detector.quit_requested:
            break

        _status_out.put("listening")
        user_text = await transcriber.listen_once()
        _status_out.put("idle")

        if not user_text.strip():
            continue

        _text_out.put(("You (voice)", user_text))   # echo voice input to GUI
        await _handle(user_text, voice=True)  # voice: play audio

    # ── Clean shutdown ────────────────────────────────────────────────────────
    gui_task.cancel()
    if automation_task is not None:
        automation_task.cancel()


# ─── Application entry point ──────────────────────────────────────────────────

def main() -> None:
    # Honour the monitor's DPI scaling so the UI isn't tiny on scaled / hi-dpi
    # displays (must be set before the QApplication is created).
    QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    # Share one OpenGL context across QtWebEngine views. Recommended by Qt for
    # WebEngine apps and the standard fix for the page rendering blank after the
    # window is minimized and restored.
    QApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("AI Companion")

    # Stop audio (kill the mpv child) when the app actually quits, so music
    # never keeps playing in the background after Quit.
    def _stop_music_on_quit() -> None:
        try:
            from actions.music import get_player
            get_player().stop()
        except Exception:
            pass
    app.aboutToQuit.connect(_stop_music_on_quit)

    # Native Qt tooltips (used for HTML title="" hints inside QWebEngine) default
    # to a pale-yellow "info" box on Windows. Restyle them to match the dark/glass
    # UI so hovering the player/track buttons doesn't flash a yellow rectangle.
    app.setStyleSheet(
        "QToolTip {"
        " background-color: #14171f;"
        " color: #eceefb;"
        " border: 1px solid rgba(255,255,255,0.16);"
        " padding: 4px 8px;"
        " font-size: 12px;"
        "}"
    )

    win = ChatWindow()
    win.show()

    icon = _make_tray_icon()
    win.setWindowIcon(icon)
    tray = QSystemTrayIcon(icon, parent=app)
    tray.setToolTip("AI Companion")

    tray_menu = QMenu()
    show_action = QAction("Show", tray_menu)
    quit_action = QAction("Quit", tray_menu)
    show_action.triggered.connect(win.restore)
    quit_action.triggered.connect(app.quit)
    tray_menu.addAction(show_action)
    tray_menu.addSeparator()
    tray_menu.addAction(quit_action)
    tray.setContextMenu(tray_menu)
    tray.activated.connect(
        lambda reason: win.restore() if reason == QSystemTrayIcon.Trigger else None
    )
    tray.show()

    # Start the full agent pipeline in a background asyncio thread
    loop = asyncio.new_event_loop()
    threading.Thread(
        target=_run_agent,
        args=(loop,),
        daemon=True,
        name="agent",
    ).start()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
