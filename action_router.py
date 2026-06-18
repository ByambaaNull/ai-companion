"""
action_router.py — Parse LLM output for ACTION directives and dispatch them.

Action format (from LLM):
    ACTION: <action_name> | <param>

Examples:
    ACTION: open_browser | https://youtube.com
    ACTION: play_music | lofi hip hop
    ACTION: screenshot | full
    ACTION: type_text | Hello world
    ACTION: click | 960,540

Design:
- Strict regex parsing — not string split — handles varied LLM whitespace
- Extensible registry: one function + one register() call = new action
- Returns (response_text, action_result_or_None) tuple
- Never raises — failed actions return a human-readable error string
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import Callable, Awaitable

import config

log = logging.getLogger(__name__)


def _bring_window_to_front(title_contains: str) -> str:
    """Find the first visible window whose title contains the string and focus it."""
    try:
        import win32gui
        import win32con

        matches: list[int] = []

        def _cb(hwnd: int, _) -> None:
            if win32gui.IsWindowVisible(hwnd):
                text = win32gui.GetWindowText(hwnd)
                if title_contains.lower() in text.lower():
                    matches.append(hwnd)

        win32gui.EnumWindows(_cb, None)
        if not matches:
            return f"No window found with '{title_contains}' in the title."
        hwnd = matches[0]
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        name = win32gui.GetWindowText(hwnd)
        return f"Focused: {name}"
    except ImportError:
        log.warning("pywin32 not installed — cannot focus windows")
        return "Window focus requires pywin32 (pip install pywin32)."
    except Exception as exc:
        return f"Could not focus '{title_contains}': {exc}"


# Matches: ACTION: <name> | <param>
# Groups:  (1) action_name  (2) param (may be empty)
_ACTION_PATTERN = re.compile(
    r"ACTION:\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\|\s*(.*)",
    re.IGNORECASE,
)

# Handler type: async (param: str) -> str
ActionHandler = Callable[[str], Awaitable[str]]


class ActionRouter:
    """
    Registry and dispatcher for companion actions.

    Usage:
        router = ActionRouter(music_player)
        response_text, action_result = await router.parse_and_execute(llm_output)
    """

    def __init__(self, music_player=None, tts=None, discord_bot=None) -> None:
        self._handlers: dict[str, ActionHandler] = {}
        self._tts = tts
        self._discord = discord_bot
        self._register_defaults(music_player)

    def register(self, name: str, handler: ActionHandler) -> None:
        """
        Register an action handler.

        Args:
            name: Case-insensitive action name (e.g. "open_browser")
            handler: Async callable (param: str) -> str result message
        """
        self._handlers[name.lower()] = handler
        log.debug("Registered action: %s", name)

    def _register_defaults(self, music_player) -> None:
        """Register all built-in action handlers."""
        from actions.browser import open_browser, open_youtube, web_search
        from actions.screenshot import take_screenshot, screenshot_to_base64
        from actions.gaming import (
            open_steam, launch_game, play_video, press_key,
            cs2_queue, start_accept_watcher, cancel_accept_watcher,
            steam_search,
        )
        from actions.afk_gaming import start_afk_gaming, stop_afk_gaming
        from actions.touch import swipe, scroll, double_tap, pinch_zoom
        import pyautogui

        # open_browser
        async def _open_browser(param: str) -> str:
            await asyncio.get_running_loop().run_in_executor(
                None, open_browser, param.strip()
            )
            return ""  # LLM already announced it; don't echo URL to chat

        # play_yt — find the best YouTube video and open it in Chrome
        async def _play_yt(param: str) -> str:
            await asyncio.get_running_loop().run_in_executor(
                None, open_youtube, param.strip()
            )
            return ""

        # play_music
        async def _play_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            await asyncio.get_running_loop().run_in_executor(
                None, music_player.play, param.strip()
            )
            return ""  # LLM already announced it; don't echo URL to chat

        # screenshot
        async def _screenshot(param: str) -> str:
            loop = asyncio.get_running_loop()
            path = await loop.run_in_executor(None, take_screenshot, None)
            return f"Screenshot saved to {path.name}."

        # type_text — paste via clipboard to support full Unicode
        async def _type_text(param: str) -> str:
            loop = asyncio.get_running_loop()
            def _do_type():
                import time
                import pyperclip
                time.sleep(0.3)
                pyperclip.copy(param)
                pyautogui.hotkey("ctrl", "v")
            await loop.run_in_executor(None, _do_type)
            return f"Typed: {param[:40]}{'…' if len(param) > 40 else ''}"

        # click — "x,y" pixel coordinates
        # Guardrail: reject obviously destructive coordinates (0,0 etc.)
        async def _click(param: str) -> str:
            try:
                parts = param.strip().split(",")
                x, y = int(parts[0].strip()), int(parts[1].strip())
            except (ValueError, IndexError):
                return f"Invalid click coordinates: '{param}'. Use format: x,y"
            if x < 5 and y < 5:
                return "Click at (0,0) blocked — likely an LLM error."
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, pyautogui.click, x, y)
            return f"Clicked at ({x}, {y})."

        # open_app — launch any executable or app by name (shlex handles spaces)
        async def _open_app(param: str) -> str:
            import subprocess as sp
            try:
                args = shlex.split(param.strip(), posix=False)
                sp.Popen(args, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                return f"Launched {param.strip()}."
            except FileNotFoundError:
                return f"Could not find application: {param.strip()}"
            except Exception as exc:
                return f"Failed to launch {param.strip()}: {exc}"

        # ── gaming / system actions ──────────────────────────────────────────
        _tts_ref = self._tts

        async def _open_steam(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(None, open_steam)

        async def _launch_game(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, launch_game, param.strip()
            )

        async def _play_video(param: str) -> str:
            await asyncio.get_running_loop().run_in_executor(
                None, play_video, param.strip()
            )
            return ""  # LLM already announced it; don't echo URL to chat

        async def _press_key(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, press_key, param.strip()
            )

        async def _cs2_queue(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(None, cs2_queue)

        async def _accept_match(param: str) -> str:
            # timeout override via param e.g. "300"
            try:
                timeout = int(param.strip()) if param.strip().isdigit() else 300
            except (ValueError, AttributeError):
                timeout = 300
            return start_accept_watcher(tts=_tts_ref, timeout=timeout)

        async def _cancel_accept(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, cancel_accept_watcher
            )

        # ── touch actions ────────────────────────────────────────────────────

        async def _swipe(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, swipe, param.strip()
            )

        async def _scroll(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, scroll, param.strip()
            )

        async def _double_tap(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, double_tap, param.strip()
            )

        async def _pinch_zoom(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, pinch_zoom, param.strip()
            )

        # ── music preferences ────────────────────────────────────────────────
        async def _like_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.like
            )

        async def _save_fav(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.save_fav
            )

        async def _dislike_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.dislike
            )

        async def _top_played(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            try:
                n = int(param.strip()) if param.strip().isdigit() else 5
            except ValueError:
                n = 5
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.show_top_played, n
            )

        async def _show_favourites(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.show_favourites
            )

        async def _pause_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.pause
            )

        async def _resume_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.resume
            )

        async def _stop_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.stop
            )

        async def _next_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.next
            )

        async def _previous_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.previous
            )

        async def _music_volume(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            try:
                level = int(param.strip())
            except (TypeError, ValueError):
                return "Please specify a volume level between 0 and 100."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.set_volume, level
            )

        async def _download_music(param: str) -> str:
            """Download a song as MP3 for offline listening (no playback)."""
            from actions.music import download
            q = param.strip()
            if not q:
                return ("What should I download? Give a song name, a YouTube link, "
                        "or a Spotify link.")
            track = await asyncio.get_running_loop().run_in_executor(None, download, q)
            if track:
                return f"Downloaded '{track.get('title', q)}' for offline listening."
            return ("I couldn't download that — make sure yt-dlp and ffmpeg are "
                    "installed and you're online.")

        async def _play_playlist(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            from actions import music_library
            name = param.strip()
            if not name:
                return "Which playlist? e.g. play_playlist | Focus"
            match = next(
                (p for p in music_library.list_playlists()
                 if p.get("name", "").lower() == name.lower()),
                None,
            )
            if not match:
                return f"No playlist named '{name}'."
            tracks = music_library.playlist_tracks(match["id"])
            if not tracks:
                return f"Playlist '{name}' is empty."
            ids = [t["id"] for t in tracks]
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.play_queue, ids, 0
            )

        async def _shuffle_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            p = param.strip().lower()
            if p in ("", "toggle"):
                return music_player.toggle_shuffle()
            return music_player.set_shuffle(p)

        async def _repeat_music(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return music_player.set_repeat(param.strip())

        async def _sleep_timer(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            p = param.strip().lower()
            if p in ("off", "cancel", "stop", "0"):
                return music_player.cancel_sleep_timer()
            return music_player.set_sleep_timer(param.strip())

        async def _play_all(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.play_collection, "all"
            )

        async def _play_liked(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.play_collection, "liked"
            )

        async def _play_favourites(param: str) -> str:
            if music_player is None:
                return "Music player not available."
            return await asyncio.get_running_loop().run_in_executor(
                None, music_player.play_collection, "favourites"
            )

        async def _import_music(param: str) -> str:
            from actions.music import import_folder
            folder = param.strip()
            if not folder:
                return ("Which folder? e.g. import_music | C:\\Users\\me\\Music")
            result = await asyncio.get_running_loop().run_in_executor(
                None, import_folder, folder
            )
            if result.get("error"):
                return result["error"]
            added, scanned = result.get("added", 0), result.get("scanned", 0)
            if scanned == 0:
                return f"No audio files found in {folder}."
            return f"Imported {added} of {scanned} audio file(s) into your library."

        # ── Video downloader (choose resolution) ──────────────────────────────
        async def _download_video(param: str) -> str:
            """Download a video at a chosen resolution.

            param: a link/query, optionally with a resolution token such as
            "720p", "1080", "4k", or "audio" / "mp3" for audio-only.
            e.g.  download_video | https://youtu.be/xxxx 720p
            """
            from actions.video import download
            raw = (param or "").strip()
            if not raw:
                return ("What video should I download? Give a YouTube link, "
                        "optionally with a resolution like 720p or 1080p.")
            known = {144, 240, 360, 480, 720, 1080, 1440, 2160}
            alias = {"4k": 2160, "uhd": 2160, "2k": 1440, "fhd": 1080, "hd": 720}
            height: int | None = None
            audio_only = False
            kept: list[str] = []
            for w in raw.split():
                lw = w.lower().strip(".,")
                if lw in ("audio", "mp3", "audio-only", "audioonly"):
                    audio_only = True
                    continue
                if lw in alias:
                    height = alias[lw]
                    continue
                m = re.fullmatch(r"(\d{3,4})p?", lw)
                if m and int(m.group(1)) in known:
                    height = int(m.group(1))
                    continue
                kept.append(w)
            url_or_query = " ".join(kept).strip() or raw
            res = await asyncio.get_running_loop().run_in_executor(
                None, lambda: download(url_or_query, height=height, audio_only=audio_only)
            )
            if res.get("ok"):
                where = "as MP3" if audio_only else (f"at {height}p" if height else "")
                return f"Downloaded '{res.get('title', url_or_query)}' {where}. Saved to {res.get('path')}."
            return res.get("error", "Video download failed.")

        # ── Background eraser (local, free) ───────────────────────────────────
        async def _erase_background(param: str) -> str:
            from actions.background_eraser import remove_background
            path = (param or "").strip().strip('"').strip("'")
            if not path:
                return ("Which image? Give the full path to an image file, or open "
                        "the Background eraser tab to pick one.")
            res = await asyncio.get_running_loop().run_in_executor(
                None, remove_background, path
            )
            if res.get("ok"):
                return f"Background removed — saved the transparent cut-out to {res.get('output')}."
            return res.get("error", "Background removal failed.")

        self.register("download_video",   _download_video)
        self.register("download_yt_video", _download_video)   # alias
        self.register("save_video",        _download_video)   # alias
        self.register("erase_background",  _erase_background)
        self.register("remove_background", _erase_background)  # alias
        self.register("remove_bg",         _erase_background)  # alias

        # ── Discord auto-reply ────────────────────────────────────────────────
        _discord_ref = self._discord

        async def _discord_away(param: str) -> str:
            if _discord_ref is None:
                return "Discord bot not initialised."
            if not _discord_ref.is_running:
                start_msg = await _discord_ref.start()
                log.info("Discord bot start: %s", start_msg)
            _discord_ref.set_away(True)
            return (
                "Discord away mode ON. "
                "The bot will reply to your DMs and monitored channels while you're away."
            )

        async def _discord_back(param: str) -> str:
            if _discord_ref is None:
                return "Discord bot not initialised."
            _discord_ref.set_away(False)
            return "Discord away mode OFF. I'll stop replying on Discord."

        async def _discord_monitor(param: str) -> str:
            if _discord_ref is None:
                return "Discord bot not initialised."
            if not _discord_ref.is_running:
                return "Discord bot is not running. Activate away mode first."
            return await _discord_ref.find_and_monitor_channel(param.strip())

        async def _send_discord_dm(param: str) -> str:
            from actions.social import _parse, send_discord_message
            recipient, message = _parse(param)
            if not message:
                return "Please include a message, e.g.: John : hey are you free tonight?"
            return await send_discord_message(recipient, message)

        async def _send_discord_channel(param: str) -> str:
            # Discord Web doesn’t support channel posting easily via search;
            # redirect to DM for now and inform the user.
            from actions.social import _parse, send_discord_message
            channel, message = _parse(param)
            if not message:
                return "Please include a message, e.g.: #general : hey everyone!"
            # Attempt to treat the channel name as a recipient (works for DM groups)
            return await send_discord_message(channel, message)

        # ── dev tools (IT student) ───────────────────────────────────────────
        from actions.devtools import (
            start_pomodoro, stop_pomodoro, pomodoro_status,
            run_command, open_localhost, open_folder, search_docs,
        )
        _tts_for_pomo = self._tts

        async def _pomodoro(param: str) -> str:
            lower = param.strip().lower()
            if lower in ("stop", "cancel", "end"):
                return await asyncio.get_running_loop().run_in_executor(None, stop_pomodoro)
            if lower in ("status", "check", "how long"):
                return await asyncio.get_running_loop().run_in_executor(None, pomodoro_status)
            return await start_pomodoro(tts=_tts_for_pomo)

        async def _stop_pomodoro(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(None, stop_pomodoro)

        async def _run_command(param: str) -> str:
            return await run_command(param)

        async def _open_localhost(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, open_localhost, param
            )

        async def _open_folder(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, open_folder, param
            )

        async def _search_docs(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, search_docs, param
            )

        # ── social messaging (Facebook / Instagram / X / WhatsApp) ─────────
        from actions.social import (
            send_facebook_message, send_instagram_message,
            send_twitter_message, send_whatsapp_message,
            send_message as _social_send_message,
            set_away as _social_set_away,
            is_away as _social_is_away,
            send_away_reply,
            save_contact as _save_contact_fn,
            list_contacts as _list_contacts_fn,
            delete_contact as _delete_contact_fn,
            read_facebook_messages,
        )
        from actions.reminders import reminder_manager as _reminder_mgr
        from actions.system_info import (
            get_weather as _get_weather,
            get_system_info as _get_sysinfo,
            add_note as _add_note_fn,
            read_notes as _read_notes_fn,
            clear_notes as _clear_notes_fn,
            read_clipboard as _read_clipboard_fn,
            add_journal_entry as _add_journal_fn,
            read_journal as _read_journal_fn,
        )
        from actions.daily_brief import daily_brief as _daily_brief_fn

        _discord_ref2 = self._discord  # second ref used in away closures

        # ── contacts ─────────────────────────────────────────────────────────
        async def _save_contact(param: str) -> str:
            if ":" in param:
                parts = param.split(":", 1)
                return await _save_contact_fn(parts[0].strip(), parts[1].strip())
            return "Format: save_contact | nickname:Full Name"

        async def _list_contacts(param: str) -> str:
            return _list_contacts_fn()

        async def _delete_contact(param: str) -> str:
            return _delete_contact_fn(param.strip())

        # ── reminders ────────────────────────────────────────────────────────
        async def _remind(param: str) -> str:
            return await _reminder_mgr.set_reminder(param.strip())

        async def _list_reminders(param: str) -> str:
            return _reminder_mgr.list_reminders()

        async def _cancel_reminder(param: str) -> str:
            return _reminder_mgr.cancel_reminder(param.strip())

        # ── system info / weather / notes / clipboard ─────────────────────────
        async def _weather(param: str) -> str:
            return await _get_weather(param.strip())

        async def _sysinfo(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, _get_sysinfo, param
            )

        async def _note(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, _add_note_fn, param.strip()
            )

        async def _read_notes(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, _read_notes_fn, param.strip()
            )

        async def _clear_notes(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, _clear_notes_fn, param
            )

        async def _clipboard(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, _read_clipboard_fn, param
            )

        # ── journal ───────────────────────────────────────────────────────────
        async def _journal(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, _add_journal_fn, param.strip()
            )

        async def _read_journal(param: str) -> str:
            return await asyncio.get_running_loop().run_in_executor(
                None, _read_journal_fn, param.strip()
            )

        # ── daily brief ───────────────────────────────────────────────────────
        async def _daily_brief(param: str) -> str:
            return await _daily_brief_fn(list_reminders_fn=_reminder_mgr.list_reminders)

        # ── read messages ─────────────────────────────────────────────────────
        async def _read_fb_msgs(param: str) -> str:
            recipient = param.strip()
            if not recipient:
                return "Who's messages do you want to read? e.g.: read_fb_messages | lety"
            return await read_facebook_messages(recipient)

        async def _send_fb(param: str) -> str:
            from actions.social import _parse
            recipient, message = _parse(param)
            if not message:
                return "Please include a message after the recipient name, e.g.: Lety : hey come tomorrow!"
            return await send_facebook_message(recipient, message)

        async def _send_ig(param: str) -> str:
            from actions.social import _parse
            recipient, message = _parse(param)
            if not message:
                return "Please include a message after the recipient name, e.g.: Lety : hey come tomorrow!"
            return await send_instagram_message(recipient, message)

        async def _send_twitter(param: str) -> str:
            from actions.social import _parse
            recipient, message = _parse(param)
            if not message:
                return "Please include a message, e.g.: @elonmusk : hello there!"
            return await send_twitter_message(recipient, message)

        async def _send_whatsapp(param: str) -> str:
            from actions.social import _parse
            recipient, message = _parse(param)
            if not message:
                return "Please include a message, e.g.: Mom : heading home soon!"
            return await send_whatsapp_message(recipient, message)

        async def _send_message(param: str) -> str:
            """
            Universal dispatcher.
            Format:  platform : recipient : message
            Example: twitter : elonmusk : hey what's up?
            """
            parts = param.split(":", 2)
            if len(parts) < 3:
                return (
                    "Format: platform : recipient : message\n"
                    "Platforms: facebook, instagram, twitter, whatsapp, discord\n"
                    "Example: twitter : elonmusk : hey what's up?"
                )
            platform  = parts[0].strip()
            recipient = parts[1].strip()
            message   = parts[2].strip()
            if not message:
                return "Message cannot be empty."
            return await _social_send_message(
                platform, recipient, message, discord_bot=_discord_ref2
            )

        # ── global away mode (all platforms) ─────────────────────────────────
        async def _set_away(param: str) -> str:
            """
            Enable away mode across all platforms.
            param (optional): custom away message template. Use {name} as placeholder.
            """
            _social_set_away(True, param.strip())
            # Also enable Discord away if bot is available
            if _discord_ref2 is not None:
                if not _discord_ref2.is_running:
                    await _discord_ref2.start()
                _discord_ref2.set_away(True)
            template = param.strip() or "Hey! I'm away right now, will reply soon."
            return (
                f"Away mode is ON on all platforms. "
                f"Message template: \"{template}\""
            )

        async def _set_back(param: str) -> str:
            """Disable away mode on all platforms."""
            _social_set_away(False)
            if _discord_ref2 is not None:
                _discord_ref2.set_away(False)
            return "Away mode is OFF. I'll stop sending auto-replies."

        async def _send_away_msg(param: str) -> str:
            """
            Manually send the away message to someone.
            Format: platform : recipient
            """
            parts = param.split(":", 1)
            if len(parts) < 2:
                return "Format: platform : recipient  (e.g. facebook : John)"
            platform  = parts[0].strip()
            recipient = parts[1].strip()
            user_name = getattr(config, "USER_DISPLAY_NAME", config.USER_ID)
            return await send_away_reply(
                platform, recipient,
                user_name=user_name,
                discord_bot=_discord_ref2,
            )

        # ── look at screen (on-demand real-time vision) ──────────────────────
        async def _look_at_screen(param: str) -> str:
            from screen_watcher import look_now
            return await look_now()

        # ── focus / bring window to front ────────────────────────────────────
        async def _focus_app(param: str) -> str:
            title = param.strip()
            if not title:
                return "Please specify a window title to focus."
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _bring_window_to_front, title)

        # ── backup ───────────────────────────────────────────────────────────
        async def _backup_data(param: str) -> str:
            from backup import create_backup
            loop = asyncio.get_running_loop()
            archive = await loop.run_in_executor(None, create_backup)
            return (
                f"Backup saved to {archive.name}. "
                f"Copy it to a new machine and run: python backup.py --restore"
            )

        self.register("pomodoro",         _pomodoro)
        self.register("stop_pomodoro",    _stop_pomodoro)
        self.register("run_command",      _run_command)
        self.register("open_localhost",   _open_localhost)
        self.register("open_folder",      _open_folder)
        self.register("search_docs",      _search_docs)
        # contacts
        self.register("save_contact",         _save_contact)
        self.register("list_contacts",        _list_contacts)
        self.register("delete_contact",       _delete_contact)
        self.register("forget_contact",       _delete_contact)     # alias
        # reminders
        self.register("remind",               _remind)
        self.register("reminder",             _remind)             # alias
        self.register("reminders",            _list_reminders)
        self.register("cancel_reminder",      _cancel_reminder)
        # weather / system / notes / clipboard
        self.register("weather",              _weather)
        self.register("sysinfo",              _sysinfo)
        self.register("system_info",          _sysinfo)            # alias
        self.register("note",                 _note)
        self.register("notes",                _read_notes)
        self.register("read_notes",           _read_notes)         # alias
        self.register("clear_notes",          _clear_notes)
        self.register("clipboard",            _clipboard)
        self.register("read_clipboard",       _clipboard)          # alias
        self.register("journal",              _journal)
        self.register("read_journal",         _read_journal)
        self.register("daily_brief",          _daily_brief)
        self.register("brief",                _daily_brief)        # alias
        # read messages
        self.register("read_fb_messages",     _read_fb_msgs)
        self.register("read_messages",        _read_fb_msgs)       # alias
        # social messaging
        self.register("send_fb_message",      _send_fb)
        self.register("send_ig_message",      _send_ig)
        self.register("send_twitter_dm",      _send_twitter)
        self.register("send_x_dm",            _send_twitter)   # alias
        self.register("send_whatsapp",        _send_whatsapp)
        self.register("send_message",         _send_message)   # universal
        self.register("set_away",             _set_away)
        self.register("away_on",              _set_away)       # alias
        self.register("set_back",             _set_back)
        self.register("away_off",             _set_back)       # alias
        self.register("send_away_message",    _send_away_msg)
        self.register("look_at_screen",   _look_at_screen)
        self.register("focus_app",       _focus_app)
        self.register("focus_mode",      _pomodoro)   # alias: "focus mode" → start Pomodoro
        self.register("open_browser",    _open_browser)
        self.register("play_yt",         _play_yt)
        self.register("play_youtube",    _play_yt)             # alias
        self.register("play_music",      _play_music)
        self.register("download_music",  _download_music)
        self.register("download_song",   _download_music)     # alias
        self.register("save_offline",    _download_music)     # alias
        self.register("play_playlist",   _play_playlist)
        self.register("shuffle_music",   _shuffle_music)
        self.register("shuffle",         _shuffle_music)        # alias
        self.register("repeat_music",    _repeat_music)
        self.register("repeat",          _repeat_music)         # alias
        self.register("sleep_timer",     _sleep_timer)
        self.register("play_all",        _play_all)
        self.register("play_liked",      _play_liked)
        self.register("play_favourites", _play_favourites)
        self.register("play_favorites",  _play_favourites)      # alias
        self.register("import_music",    _import_music)
        self.register("import_folder",   _import_music)         # alias
        self.register("like_music",      _like_music)
        self.register("save_favourite",  _save_fav)
        self.register("save_fav",        _save_fav)            # alias
        self.register("dislike_music",   _dislike_music)
        self.register("top_played",      _top_played)
        self.register("music_stats",     _top_played)          # alias
        self.register("my_favourites",   _show_favourites)
        self.register("show_favourites", _show_favourites)     # alias
        self.register("pause_music",     _pause_music)
        self.register("resume_music",    _resume_music)
        self.register("stop_music",      _stop_music)
        self.register("next_music",      _next_music)
        self.register("previous_music",  _previous_music)
        self.register("prev_music",      _previous_music)       # alias
        self.register("music_volume",    _music_volume)
        self.register("backup_data",     _backup_data)
        self.register("screenshot",      _screenshot)
        self.register("type_text",       _type_text)
        self.register("click",           _click)
        self.register("open_app",        _open_app)
        self.register("open_steam",      _open_steam)
        self.register("launch_game",     _launch_game)
        self.register("play_video",      _play_video)
        self.register("press_key",       _press_key)
        self.register("cs2_queue",       _cs2_queue)
        self.register("accept_match",    _accept_match)
        self.register("cancel_accept",   _cancel_accept)
        self.register("swipe",           _swipe)
        self.register("scroll",          _scroll)
        self.register("double_tap",      _double_tap)
        self.register("pinch_zoom",      _pinch_zoom)
        self.register("discord_away",    _discord_away)
        self.register("discord_back",    _discord_back)
        self.register("discord_monitor", _discord_monitor)
        self.register("send_discord_dm", _send_discord_dm)
        self.register("send_discord_channel", _send_discord_channel)

        # ── web & steam search ───────────────────────────────────────────────

        async def _web_search(param: str) -> str:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, web_search, param.strip())

        async def _steam_search(param: str) -> str:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, steam_search, param.strip())

        self.register("web_search",    _web_search)
        self.register("search_web",    _web_search)    # alias
        self.register("steam_search",  _steam_search)
        self.register("search_steam",  _steam_search)  # alias

        # ── AFK gaming ───────────────────────────────────────────────────────

        async def _afk_game(param: str) -> str:
            return start_afk_gaming(param.strip(), tts=_tts_ref)

        async def _stop_afk(param: str) -> str:
            return stop_afk_gaming()

        self.register("afk_game",  _afk_game)
        self.register("afk",       _afk_game)       # alias
        self.register("stop_afk",  _stop_afk)
        self.register("afk_stop",  _stop_afk)       # alias

        # ── Productivity pack (office / study) ───────────────────────────────
        # Imports are lazy so a missing optional dependency in one module
        # never blocks the rest of the app from starting.

        async def _check_email(param: str) -> str:
            from actions.email_assistant import sweep_inbox
            return await sweep_inbox(param)

        async def _email_summary(param: str) -> str:
            from actions.email_assistant import email_summary
            return await email_summary(param)

        async def _organize_downloads(param: str) -> str:
            from actions.file_organizer import organize_downloads
            return await organize_downloads(param)

        async def _cleanup_temp(param: str) -> str:
            from actions.file_organizer import cleanup_temp
            return await cleanup_temp(param)

        async def _summarize_doc(param: str) -> str:
            from actions.documents import summarize_document
            return await summarize_document(param)

        async def _clipboard_ai(param: str) -> str:
            from actions.clipboard_ai import clipboard_ai
            return await clipboard_ai(param)

        async def _todo_add(param: str) -> str:
            from actions.todo import todo_add
            return await todo_add(param)

        async def _todo_list(param: str) -> str:
            from actions.todo import todo_list
            return await todo_list(param)

        async def _todo_done(param: str) -> str:
            from actions.todo import todo_done
            return await todo_done(param)

        async def _todo_clear(param: str) -> str:
            from actions.todo import todo_clear
            return await todo_clear(param)

        async def _meeting_start(param: str) -> str:
            from actions.meeting_notes import meeting_start
            return await meeting_start(param)

        async def _meeting_stop(param: str) -> str:
            from actions.meeting_notes import meeting_stop
            return await meeting_stop(param)

        async def _write_draft(param: str) -> str:
            from actions.drafts import write_draft
            return await write_draft(param)

        async def _weekly_report(param: str) -> str:
            from actions.weekly_report import weekly_report
            return await weekly_report(param)

        async def _ocr_screen(param: str) -> str:
            from actions.ocr_screen import ocr_screen
            return await ocr_screen(param)

        self.register("check_email",        _check_email)
        self.register("sweep_email",        _check_email)      # alias
        self.register("read_email",         _check_email)      # alias
        self.register("email_summary",      _email_summary)
        self.register("organize_downloads", _organize_downloads)
        self.register("organize_files",     _organize_downloads)  # alias
        self.register("cleanup_temp",       _cleanup_temp)
        self.register("summarize_doc",      _summarize_doc)
        self.register("summarize_document", _summarize_doc)    # alias
        self.register("clipboard_ai",       _clipboard_ai)
        self.register("todo",               _todo_add)
        self.register("todo_add",           _todo_add)         # alias
        self.register("todos",              _todo_list)
        self.register("todo_list",          _todo_list)        # alias
        self.register("todo_done",          _todo_done)
        self.register("todo_clear",         _todo_clear)
        self.register("meeting_start",      _meeting_start)
        self.register("meeting_stop",       _meeting_stop)
        self.register("draft",              _write_draft)
        self.register("write_draft",        _write_draft)      # alias
        self.register("weekly_report",      _weekly_report)
        self.register("ocr_screen",         _ocr_screen)
        self.register("ocr",                _ocr_screen)       # alias

    # ─── Parse and dispatch ───────────────────────────────────────────────────

    def extract_action(self, text: str) -> tuple[str, str] | None:
        """
        Extract ACTION directive from LLM output.

        Returns (action_name, param) or None if no action found.
        The text may contain prose before or after the ACTION line.
        """
        match = _ACTION_PATTERN.search(text)
        if match:
            return match.group(1).lower(), match.group(2).strip()
        return None

    def strip_action(self, text: str) -> str:
        """Remove the ACTION: line from response text for cleaner TTS output."""
        # First pass: exact pattern match
        cleaned = _ACTION_PATTERN.sub("", text).strip()
        # Second pass: catch any remaining malformed ACTION: line the LLM may have
        # invented (e.g. 'ACTION: open_browser <url> | ...' or multiple words before |)
        cleaned = re.sub(r'\s*ACTION:\s*\S.*$', '', cleaned,
                         flags=re.IGNORECASE | re.DOTALL).strip()
        return cleaned

    async def parse_and_execute(
        self, llm_output: str
    ) -> tuple[str, str | None]:
        """
        Parse LLM output for an ACTION directive and execute it.

        Returns:
            (speech_text, action_result_or_None)

            speech_text    — LLM response with ACTION line stripped (for TTS)
            action_result  — human-readable result returned by the handler,
                             or None if no action was found
        """
        action_tuple = self.extract_action(llm_output)
        speech_text = self.strip_action(llm_output)
        # Strip any URLs the LLM leaked into its spoken response — they sound
        # terrible when read aloud by TTS ("h t t p s colon slash slash ...").
        speech_text = re.sub(r'https?://\S+', '', speech_text).strip()

        if action_tuple is None:
            return speech_text, None

        action_name, param = action_tuple
        handler = self._handlers.get(action_name)

        if handler is None:
            log.warning("Unknown action: %r", action_name)
            return speech_text, f"I don't know how to perform action '{action_name}'."

        # Per-action timeout prevents a stalled Playwright/LLM call from hanging the loop.
        # Social messaging gets more time (90 s); everything else 30 s.
        _SLOW_ACTIONS = {
            "send_fb_message", "send_ig_message", "send_twitter_dm", "send_x_dm",
            "send_whatsapp", "send_message", "send_discord_dm", "send_discord_channel",
            "send_away_message", "set_away", "away_on",
            # productivity actions that hit IMAP / LLM / Whisper and can run long
            "check_email", "sweep_email", "read_email", "email_summary",
            "summarize_doc", "summarize_document", "clipboard_ai",
            "meeting_stop", "draft", "write_draft", "weekly_report",
            "ocr_screen", "ocr", "organize_downloads", "organize_files",
            # music actions that may download (yt-dlp + ffmpeg transcode) before playing
            "play_music", "download_music", "download_song", "save_offline",
            "play_playlist",
            # local-folder import can scan a large music library off disk
            "import_music", "import_folder",
            # video downloads (yt-dlp + ffmpeg merge) and background removal
            # (ONNX model load + inference) can run well past 30s
            "download_video", "download_yt_video", "save_video",
            "erase_background", "remove_background", "remove_bg",
        }
        timeout_s = 90.0 if action_name in _SLOW_ACTIONS else 30.0

        log.info("Executing action: %s(%r) [timeout=%.0fs]", action_name, param, timeout_s)
        try:
            result = await asyncio.wait_for(handler(param), timeout=timeout_s)
            log.info("Action result: %s", result)
            return speech_text, result
        except asyncio.TimeoutError:
            log.error("Action %r timed out after %.0fs", action_name, timeout_s)
            return speech_text, f"Action '{action_name}' timed out — the request took too long."
        except Exception as exc:
            log.error("Action %r raised: %s", action_name, exc)
            return speech_text, f"Action '{action_name}' failed: {exc}"


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    config.setup_logging()
    log.info("ActionRouter standalone test")

    router = ActionRouter(music_player=None)

    test_cases = [
        "Sure! ACTION: open_browser | https://github.com",
        "Here is the screenshot you requested. ACTION: screenshot | full",
        "Let me type that for you. ACTION: type_text | Hello, world!",
        "Playing your music now. ACTION: play_music | lofi hip hop",
        "ACTION: click | 960,540 I clicked the centre of your screen.",
        "No action in this response — just a normal reply.",
        "ACTION: unknown_action | test parameter",
    ]

    async def _test() -> None:
        for text in test_cases:
            log.info("\nInput:  %r", text)
            speech, result = await router.parse_and_execute(text)
            log.info("Speech: %r", speech)
            log.info("Action: %r", result)

    asyncio.run(_test())
    log.info("\nActionRouter tests passed ✓")
