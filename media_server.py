"""
media_server.py — tiny localhost HTTP server for in-app media playback.

The UI runs inside QtWebEngine (Chromium) loaded via setHtml, so an HTML5
<video> can't read local files directly (no file:// access from a qrc/data page).
This serves a single directory over http://127.0.0.1:<port>/ so the player can
stream — and, crucially, SEEK — downloaded videos. Range requests are handled so
the scrub bar works.

Usage:
    import media_server
    url = media_server.url_for(r"C:\\...\\video.mp4", VIDEO_CACHE_DIR)
    # -> http://127.0.0.1:54321/video.mp4
"""

from __future__ import annotations

import http.server
import mimetypes
import os
import threading
import urllib.parse
from pathlib import Path

_server: http.server.ThreadingHTTPServer | None = None
_base_url: str = ""
_serve_dir: str = ""
_lock = threading.Lock()


class _MediaHandler(http.server.BaseHTTPRequestHandler):
    directory = ""  # set on the class before the server starts

    def log_message(self, *args) -> None:  # silence per-request console spam
        pass

    def _resolve(self) -> str | None:
        rel = urllib.parse.unquote(self.path.split("?", 1)[0]).lstrip("/")
        base = os.path.normpath(self.directory)
        full = os.path.normpath(os.path.join(base, rel))
        # Path-traversal guard: must stay inside the served directory.
        if full != base and not full.startswith(base + os.sep):
            return None
        return full

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def _serve(self, head: bool) -> None:
        path = self._resolve()
        if not path or not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"

        start, end = 0, size - 1
        partial = False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                s, e = rng[6:].split("-", 1)
                start = int(s) if s else 0
                end = int(e) if e else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                partial = True
            except Exception:
                start, end, partial = 0, size - 1, False

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        # Allow the qrc-origin page (and a crossorigin <video>/<track>) to read this.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break  # client seeked/closed — normal for video
                remaining -= len(chunk)


def start(directory: str | os.PathLike) -> str:
    """Start (once) a localhost server rooted at *directory*; return its base URL."""
    global _server, _base_url, _serve_dir
    with _lock:
        if _server is not None:
            return _base_url
        _serve_dir = str(Path(directory).resolve())
        _MediaHandler.directory = _serve_dir
        # Ensure mp4/webm map to the right type even on minimal Windows installs.
        mimetypes.add_type("video/mp4", ".mp4")
        mimetypes.add_type("video/webm", ".webm")
        mimetypes.add_type("video/x-matroska", ".mkv")
        mimetypes.add_type("audio/mpeg", ".mp3")
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MediaHandler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="media-server").start()
        _server = httpd
        _base_url = f"http://127.0.0.1:{port}/"
        return _base_url


def url_for(file_path: str | os.PathLike, directory: str | os.PathLike) -> str:
    """Return a playable URL for *file_path* (which must live under *directory*)."""
    base = start(directory)
    rel = os.path.relpath(str(file_path), _serve_dir or str(Path(directory).resolve()))
    return base + urllib.parse.quote(rel.replace(os.sep, "/"))
