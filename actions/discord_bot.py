"""
discord_bot.py — Discord self-bot: auto-reply as YOUR OWN account when away.

Uses discord.py-self which supports user account tokens, so replies come
from your actual Discord identity — friends see messages from you, not a bot.

SETUP (one-time):
  1. pip install discord.py-self
  2. Get your user token:
       • Open discord.com/app in a browser
       • Press F12 → Application tab → Local Storage → https://discord.com → key "token"
         (or F12 → Network → filter "api" → any request → Headers → Authorization)
  3. Paste it into config.py as DISCORD_USER_TOKEN, or set the environment variable.
  4. Tell the companion "I'm going away" — it activates Discord auto-reply.
  5. Tell the companion "I'm back" — auto-reply deactivates.

When away mode is ON:
  • DMs sent to you are replied to automatically.
  • Guild channels you've added with discord_monitor | <channel-name> are replied to.
  • Replies use the LLM with per-channel rolling conversation history.
  • Realistic typing delay (≈40 ms/char) before sending.
  • ACTION directives are stripped — only the natural-language reply is sent.

Note: This is for personal, non-malicious use chatting with friends.
      Requires discord.py-self (pip install discord.py-self).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import time
from typing import Callable

import config

log = logging.getLogger(__name__)

_ACTION_RE = re.compile(r"ACTION:\s*\S+\s*\|.*", re.IGNORECASE)

_OK = False
try:
    import discord          # must be discord.py-self, not plain discord.py
    _OK = True
except ImportError:
    pass


# ─── Client subclass ──────────────────────────────────────────────────────────

if _OK:
    class _SelfClient(discord.Client):
        def __init__(self, on_msg: Callable, **kwargs) -> None:
            super().__init__(**kwargs)
            self._cb = on_msg

        async def on_ready(self) -> None:
            log.info(
                "Discord self-bot online ✓  logged in as %s#%s",
                self.user.name,
                self.user.discriminator,
            )

        async def on_message(self, message: discord.Message) -> None:
            await self._cb(message)
else:
    _SelfClient = None  # type: ignore[assignment,misc]


# ─── Auto-replier ─────────────────────────────────────────────────────────────

class DiscordAutoReplier:
    """
    Replies as the user on Discord using their account token.

    Usage:
        replier = DiscordAutoReplier(get_llm_response, memory_mgr, character)

        # Activate away mode (starts self-bot on first call):
        await replier.set_away_on()

        # Deactivate:
        replier.set_away(False)

        # Monitor a specific guild channel:
        await replier.find_and_monitor_channel("general")
    """

    def __init__(
        self,
        get_llm_response: Callable,
        memory_mgr=None,
        character=None,
    ) -> None:
        self._llm    = get_llm_response
        self._memory = memory_mgr
        self._char   = character
        self._away   = False
        self._client: "_SelfClient | None" = None
        self._task:   asyncio.Task | None  = None

        # channel_id → rolling deque of {role, content} message dicts (40-turn window)
        self._histories: dict[int, collections.deque] = {}
        # guild channel IDs explicitly monitored (DMs are always handled when away)
        self._monitored: set[int] = set()
        # last-reply timestamp per channel — prevents spam when messages arrive fast
        self._last_reply: dict[int, float] = {}

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> str:
        """Connect to Discord using the user token. Returns a status string."""
        if not _OK:
            return (
                "discord.py-self is not installed.\n"
                "Run:  pip install discord.py-self\n"
                "Then restart the companion."
            )
        token = config.DISCORD_USER_TOKEN.strip()
        if not token:
            return (
                "DISCORD_USER_TOKEN is not set.\n"
                "Get your token from discord.com/app (F12 → Application → Local Storage → token key),\n"
                "then add it to config.py or set the DISCORD_USER_TOKEN environment variable."
            )
        if self._client and not self._client.is_closed():
            return "Self-bot is already connected."

        self._client = _SelfClient(on_msg=self._on_message)
        self._task   = asyncio.create_task(
            self._client.start(token), name="discord_selfbot"
        )
        log.info("Discord self-bot connecting…")
        return "Discord self-bot connecting. Check logs for the login confirmation."

    async def stop(self) -> str:
        """Disconnect the self-bot."""
        if self._client and not self._client.is_closed():
            await self._client.close()
        if self._task:
            self._task.cancel()
        self._client = None
        log.info("Discord self-bot stopped")
        return "Discord self-bot disconnected."

    def set_away(self, on: bool) -> None:
        self._away = on
        log.info("Discord away mode: %s", "ON" if on else "OFF")

    def set_character(self, character) -> None:
        """Wire the desktop character after it has been created."""
        self._char = character

    def monitor_channel(self, channel_id: int) -> None:
        """Add a guild channel ID to the monitored set."""
        self._monitored.add(channel_id)
        log.info("Discord: now monitoring channel id=%d", channel_id)

    async def find_and_monitor_channel(self, name: str) -> str:
        """Search all joined guilds for a channel by name and monitor it."""
        if not self._client:
            return "Self-bot is not running. Activate away mode first."
        target = name.strip().lower().lstrip("#")
        for guild in self._client.guilds:
            for channel in guild.text_channels:
                if channel.name.lower() == target:
                    self.monitor_channel(channel.id)
                    return f"Now monitoring #{channel.name} in {guild.name}."
        return (
            f"#{name} not found in any server you're in. "
            "Make sure the channel name is exact (no spaces → use dashes, e.g. general-chat)."
        )

    async def _ensure_running(self) -> str | None:
        """Start the self-bot if not already running and wait until ready. Returns error string or None."""
        if not _OK:
            return "discord.py-self is not installed. Run: pip install discord.py-self"
        if not config.DISCORD_USER_TOKEN.strip():
            return (
                "DISCORD_USER_TOKEN is not set. "
                "Add it to your .env file or config.py."
            )
        if not self.is_running:
            await self.start()
        try:
            await asyncio.wait_for(self._client.wait_until_ready(), timeout=30.0)
        except asyncio.TimeoutError:
            return "Discord bot timed out connecting. Check your token and internet connection."
        return None

    async def send_dm(self, recipient: str, message: str) -> str:
        """
        Send a proactive DM to a Discord user by display name or username.

        Searches all mutual guilds for the recipient.
        """
        if not recipient or not message:
            return "Need both a recipient name and a message."

        err = await self._ensure_running()
        if err:
            return err

        target = recipient.strip().lower()
        best_member = None
        best_score = 0.0

        for guild in self._client.guilds:
            for member in guild.members:
                for candidate in (member.display_name, member.name):
                    from difflib import SequenceMatcher
                    score = SequenceMatcher(
                        None, target, candidate.lower()
                    ).ratio()
                    if score > best_score:
                        best_score = score
                        best_member = member

        if best_member is None or best_score < 0.45:
            return (
                f"Could not find '{recipient}' in any Discord server you share. "
                "Use their exact display name or username."
            )

        try:
            dm_channel = await best_member.create_dm()
            async with dm_channel.typing():
                await asyncio.sleep(min(len(message) * 0.04, 5.0))
            await dm_channel.send(message)
            log.info("Discord DM sent to %s: %s…", best_member.display_name, message[:80])
            return f"Message sent to {best_member.display_name} on Discord ✓"
        except Exception as exc:
            log.warning("Discord DM failed: %s", exc)
            return f"Failed to send Discord DM to {best_member.display_name}: {exc}"

    async def send_channel_message(self, channel_name: str, message: str) -> str:
        """
        Send a message to a text channel in any joined guild.

        channel_name may include a leading '#'.
        """
        if not channel_name or not message:
            return "Need both a channel name and a message."

        err = await self._ensure_running()
        if err:
            return err

        target = channel_name.strip().lower().lstrip("#")
        for guild in self._client.guilds:
            for channel in guild.text_channels:
                if channel.name.lower() == target:
                    try:
                        async with channel.typing():
                            await asyncio.sleep(min(len(message) * 0.04, 5.0))
                        await channel.send(message)
                        log.info("Discord channel msg sent to #%s: %s…", channel.name, message[:80])
                        return f"Message sent to #{channel.name} in {guild.name} ✓"
                    except Exception as exc:
                        return f"Failed to send to #{channel.name}: {exc}"
        return (
            f"#{channel_name} not found in any server. "
            "Use the exact channel name (dashes not spaces, e.g. general-chat)."
        )

    @property
    def is_running(self) -> bool:
        return self._client is not None and not self._client.is_closed()

    @property
    def is_away(self) -> bool:
        return self._away

    # ─── Incoming message handler ─────────────────────────────────────────────

    async def _on_message(self, message: "discord.Message") -> None:
        if not self._away:
            return
        # Never reply to your own messages (infinite loop guard)
        if self._client and message.author.id == self._client.user.id:
            return

        is_dm  = isinstance(message.channel, (discord.DMChannel, discord.GroupChannel))
        cid    = message.channel.id

        # For guild channels: only reply in explicitly monitored ones
        if not is_dm and cid not in self._monitored:
            return

        # Per-channel rate limiting
        now = time.time()
        if now - self._last_reply.get(cid, 0.0) < config.DISCORD_REPLY_COOLDOWN_S:
            return
        self._last_reply[cid] = now

        reply = await self._generate_reply(message, is_dm)
        if not reply:
            return

        try:
            async with message.channel.typing():
                # Simulate human typing speed: ~40 ms/char, max 5 s
                await asyncio.sleep(min(len(reply) * 0.04, 5.0))
            await message.channel.send(reply)
            log.info(
                "Discord replied to %s: %s…",
                message.author.display_name,
                reply[:80],
            )
            if self._char:
                self._char.say(f"Replied to {message.author.display_name} on Discord.")
        except Exception as exc:
            log.warning("Discord send failed: %s", exc)

    # ─── LLM reply generation ─────────────────────────────────────────────────

    async def _generate_reply(
        self, message: "discord.Message", is_dm: bool
    ) -> str | None:
        cid     = message.channel.id
        history = self._histories.setdefault(cid, collections.deque(maxlen=40))
        author  = message.author.display_name
        where   = "a DM" if is_dm else f"#{message.channel.name}"

        memories = ""
        if self._memory:
            try:
                memories = self._memory.format_for_prompt(
                    message.content, user_id=config.USER_ID
                )
            except Exception:
                pass

        system_prompt = (
            f"You are replying as the user in {where} on Discord. "
            f"The person messaging is {author}. "
            f"Reply exactly as the user would — casual, natural, their own voice. "
            f"1 to 3 sentences max. Never mention being an AI or that the user is away. "
            f"Keep the conversation flowing naturally."
            + (f"\n\nAbout the user (context):\n{memories}" if memories else "")
        )

        try:
            raw   = await self._llm(message.content, system_prompt, list(history))
            reply = _ACTION_RE.sub("", raw).strip()   # strip any leaked ACTION directives
            if len(reply) > 1900:                      # Discord 2000-char limit
                reply = reply[:1897] + "…"
        except Exception as exc:
            log.warning("Discord LLM error: %s", exc)
            return None

        if not reply:
            return None

        history.append({"role": "user",      "content": f"{author}: {message.content}"})
        history.append({"role": "assistant",  "content": reply})
        return reply
