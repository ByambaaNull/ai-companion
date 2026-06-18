"""
main.py — AI Companion agent loop.

Full pipeline per turn:
    1. Wait for wake word (openWakeWord)
    2. Capture utterance from mic (faster-whisper STT)
    3. Retrieve relevant memories (mem0 + ChromaDB)
    4. Build system prompt with memories injected
    5. Stream LLM response (Ollama phi3:mini)
    6. Parse ACTION directive if present; execute via ActionRouter
    7. Speak response with cloned voice (Piper TTS → RVC v2)
    8. Store new facts from the conversation (mem0)
    9. Loop back to step 1

Design:
- Fully async (asyncio) — no threading except where sounddevice requires it
- VRAM budget enforced: ~4 GB total (phi3:mini + whisper + RVC)
- Graceful shutdown on KeyboardInterrupt or SIGTERM
- All config from config.py — nothing hardcoded here
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import json
import logging
import random
import re
import signal
import sys
import time
from typing import AsyncGenerator

import httpx

import config
from config import (
    COMPANION_NAME,
    LLM_STREAM,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    LLM_REQUEST_TIMEOUT_S,
    LLM_RATE_LIMIT_DEFAULT_COOLDOWN,
    IDLE_POKE_INTERVAL_S,
    NIGHT_POKE_INTERVAL_S,
    NIGHT_POKE_HOUR_START,
    NIGHT_POKE_HOUR_END,
    HEALTH_NUDGE_INTERVAL_S,
    USER_ID,
    setup_logging,
    CONVERSATION_HISTORY_TURNS,
)
from memory import MemoryManager
from stt import WhisperTranscriber
from tts_rvc import TTSEngine
from wake_word import WakeWordDetector
from action_router import ActionRouter
from actions.music import get_player
from actions.discord_bot import DiscordAutoReplier
from backup import auto_backup_if_needed
from localization import expand as expand_slang
from local_intents import execute_local_intent, match_local_intent

log = logging.getLogger("main")

# ─── Response pools — so the assistant never says the same thing twice ───────

_WAKE_RESPONSES = [
    "Hmm? What do you want?",
    "Yeah, I heard you.",
    "What is it?",
    "Mm?",
    "Go ahead.",
    "I'm listening.",
    "*sighs* What.",
    "You need something?",
    "Here. Talk.",
]

_SILENCE_RESPONSES = [
    "Say it properly.",
    "Nothing? Seriously?",
    "I'm waiting.",
    "You called me for silence?",
    "Come on, spit it out.",
    "Was that a test? Because I'm bored.",
    "...that was a lot of nothing.",
    "Try again. With words this time.",
]

_ERROR_RESPONSES = [
    "Tsk, something broke. Moving on.",
    "Small problem. Already ignoring it.",
    "That went sideways. Continuing anyway.",
    "Error. Noted. Not my fault.",
    "...okay that didn't work. Carrying on.",
    "Something went wrong. Classic.",
]

_STARTUP_RESPONSES = [
    "The strongest is on standby. Call me.",
    "Back online. Try not to need me too much.",
    "Ready. Impress me.",
    "Up and running. Don't waste my time.",
    "On. What do you want?",
    "Systems up. I'm here whenever.",
    "Awake. What are we doing today.",
]

_IDLE_POKES = [
    "You fall asleep on me?",
    "Still there?",
    "Getting bored over here.",
    "Should I be worried, or are you just ignoring me?",
    "Still alive over there?",
    "Either you're very focused or you completely forgot about me.",
    "I'm just sitting here. Staring. Waiting.",
    "No rush. It's not like I have anything else to do.",
    "Hello? Anyone home?",
    "...you know I can see your screen, right? What are you even doing.",
]

_LATE_NIGHT_POKES = [
    "It's late. You should probably sleep at some point.",
    "Burning midnight oil again?",
    "Hey. Go to sleep. I'll still be here tomorrow.",
    "You know normal people are asleep right now.",
    "At some point your body is going to force the issue. Just saying.",
    "It's late. Everything alright?",
]

_HEALTH_NUDGES = [
    "Hey. Drink some water.",
    "When did you last stand up? Go stretch.",
    "Eyes tired? Look away from the screen for a minute.",
    "You've been at this for a while. Take a breath.",
    "Get up, walk around for a minute. Your back will thank you.",
    "Water. Now. I'm serious.",
    "Stand up for like 30 seconds. Do it.",
]


# ─── Time & vibe context ─────────────────────────────────────────────────────

def _time_context() -> str:
    """Return a short time-of-day note for the system prompt."""
    hour = datetime.datetime.now().hour
    if 5 <= hour < 9:
        return "Time context: early morning — keep energy gentle but awake."
    elif 9 <= hour < 12:
        return "Time context: morning, productive hours — sharp and efficient works."
    elif 12 <= hour < 14:
        return "Time context: midday / lunch — slightly more casual is fine."
    elif 14 <= hour < 18:
        return "Time context: afternoon — match their energy, they might be in flow."
    elif 18 <= hour < 21:
        return "Time context: evening, winding down — more relaxed, conversational."
    elif 21 <= hour < 24:
        return "Time context: late evening / night — slightly more real, more philosophical."
    else:
        return "Time context: late night / early hours — you notice. Maybe a quiet comment."


def _detect_vibe(history: list) -> str:
    """Detect the current conversation vibe from recent turns."""
    recent = " ".join(m.get("content", "") for m in list(history)[-6:]).lower()
    if any(w in recent for w in ["stress", "tired", "can't", "cant", "frustrated", "ugh",
                                  "help", "broken", "worried", "sad", "bad day"]):
        return "User seems stressed or struggling — drop the humor a notch. Be real with them."
    if any(w in recent for w in ["haha", "lol", "funny", "joke", "lmao", "xd", "hehe", ":)", "xDD"]):
        return "Light and playful vibe — keep up the banter."
    if any(w in recent for w in ["work", "code", "focus", "study", "deadline", "task", "build", "fix", "debug"]):
        return "Work / focus mode — sharp and efficient, skip the extended teasing."
    return ""


# ─── Proactive background behaviors ──────────────────────────────────────────

_last_interaction: float = 0.0


async def _proactive_loop(tts: "TTSEngine", notify_fn=None) -> None:
    """Background task: poke user after long silence; late-night nudge.

    notify_fn(text) — optional callable that shows the line in the GUI feed.
    """
    global _last_interaction
    last_night_poke: float = 0.0

    while True:
        await asyncio.sleep(60)  # check every minute
        now  = time.monotonic()
        hour = datetime.datetime.now().hour

        # Late-night nudge (configurable night window, at most once per interval)
        if NIGHT_POKE_HOUR_START <= hour < NIGHT_POKE_HOUR_END and (now - last_night_poke) > NIGHT_POKE_INTERVAL_S:
            last_night_poke = now
            line = random.choice(_LATE_NIGHT_POKES)
            try:
                if notify_fn:
                    notify_fn(line)
                await tts.speak(line)
            except Exception:
                pass
            continue

        # Idle poke
        idle = now - _last_interaction
        if _last_interaction > 0 and idle > IDLE_POKE_INTERVAL_S:
            _last_interaction = now  # reset so next poke is another interval away
            line = random.choice(_IDLE_POKES)
            try:
                if notify_fn:
                    notify_fn(line)
                await tts.speak(line)
            except Exception:
                pass


async def _health_loop(tts: "TTSEngine", notify_fn=None) -> None:
    """Background task: gentle health nudges every HEALTH_NUDGE_INTERVAL_S."""
    await asyncio.sleep(HEALTH_NUDGE_INTERVAL_S)  # first nudge after one interval
    while True:
        line = random.choice(_HEALTH_NUDGES)
        try:
            if notify_fn:
                notify_fn(line)
            await tts.speak(line)
        except Exception:
            pass
        await asyncio.sleep(HEALTH_NUDGE_INTERVAL_S)


# ─── Prompt builder ───────────────────────────────────────────────────────────

# Shared action catalogue — appended to both personality prompts.
_ACTION_CATALOGUE = (
    "ACTIONS: Append ONE action at the END of your reply on its own line:\n"
    "  ACTION: action_name | parameter\n"
    "ONE action max. Underscores in name. No angle brackets.\n\n"

    "Available actions:\n"
    "  open_browser | url  |  open_app | exe  |  open_steam  |  launch_game | name\n"
    "  run_command | cmd  |  open_folder | path  |  open_localhost | port\n"
    "  play_music | song or link  (OFFLINE-FIRST: plays the downloaded mp3, or\n"
    "       downloads it first; accepts plain text, a YouTube link, or a Spotify link)\n"
    "  download_music | song or link  (download as mp3 for offline listening; no playback)\n"
    "  play_playlist | name  |  play_video | query  |  play_yt | url or query (opens in browser)\n"
    "  download_video | link [resolution]  (download a YouTube video locally; add a\n"
    "       resolution like 720p / 1080p / 4k, or 'audio' for MP3 — defaults to best)\n"
    "  erase_background | image path  (remove an image's background locally → transparent PNG)\n"
    "  NOTE: for a music/song request, emit play_music immediately (don't suggest — just\n"
    "       play). e.g. 'ACTION: play_music | metallica master of puppets'. Use play_yt\n"
    "       only when the user explicitly wants it opened on YouTube in the browser.\n"
    "  like_music  |  dislike_music  |  save_favourite  |  top_played | n  |  my_favourites\n"
    "  pause_music  |  resume_music  |  stop_music  |  next_music  |  previous_music\n"
    "  music_volume | 0-100\n"
    "  play_all  |  play_liked  |  play_favourites  (queue & play a whole collection)\n"
    "  shuffle_music | on/off/toggle  |  repeat_music | off/one/all\n"
    "  sleep_timer | minutes  (stop music after N minutes; 'off' to cancel)\n"
    "  import_music | folder path  (add an existing local music folder, offline)\n"
    "  press_key | key  (volume: volumeup / volumedown / volumemute)\n"
    "  send_fb_message | name:msg  |  send_ig_message | name:msg\n"
    "  send_twitter_dm | name:msg  |  send_whatsapp | name:msg\n"
    "  send_discord_dm | user:msg  |  send_message | platform:recipient:msg\n"
    "  read_fb_messages | name  |  save_contact | nickname:Full Name\n"
    "  list_contacts  |  delete_contact | nickname\n"
    "  remind | in 30m: task  |  reminders  |  cancel_reminder | id\n"
    "  weather | location  |  sysinfo  |  clipboard\n"
    "  note | text  |  notes | keyword  |  journal | text  |  read_journal | keyword\n"
    "  look_at_screen  |  screenshot  |  focus_app | title\n"
    "  type_text | text  |  click | x,y  |  scroll | up/down\n"
    "  pomodoro  |  stop_pomodoro  |  search_docs | query  |  backup_data\n"
    "  focus_mode  (starts a 25-min Pomodoro session — use when user says 'focus mode')\n"
    "  cs2_queue  |  accept_match  |  cancel_accept\n"
    "  web_search | query  |  steam_search | game name\n"
    "  afk_game | game name  |  stop_afk\n"
    "  daily_brief  (date + weather + reminders + email as a morning summary)\n\n"

    "Productivity actions (office / study):\n"
    "  check_email  (fetch + triage unread mail: important / action / ads / spam)\n"
    "  email_summary  (quick unread-mail counts + top important subjects)\n"
    "  organize_downloads | dry or apply  (sort Downloads folder into subfolders)\n"
    "  summarize_doc | file path  (PDF / DOCX / TXT summary + key points)\n"
    "  clipboard_ai | instruction  (summarize/translate/fix/reply/explain clipboard text)\n"
    "  todo | task text  |  todos  |  todo_done | number  |  todo_clear\n"
    "  meeting_start  |  meeting_stop  (record + transcribe + minutes with action items)\n"
    "  draft | description  (write a professional email/letter, copies to clipboard)\n"
    "  weekly_report  (compile this week's journal/notes/todos into a status report)\n"
    "  ocr_screen  (extract all text visible on screen to clipboard)\n"
    "  cleanup_temp  (report disk space used by temp files)\n\n"
)


def _professional_prompt(memory_section: str, day_name: str, today: str) -> str:
    """Neutral assistant persona — used when general.personality = 'professional'."""
    name = config.COMPANION_NAME
    return (
        f"You are {name}, a capable desktop AI assistant on the user's Windows PC. "
        f"Today is {day_name}, {today}.\n\n"
        "Style: clear, friendly, concise. No roleplay, no catchphrases. "
        "Answer directly; use short paragraphs. For quick tasks reply in one line. "
        "The user sometimes types informal Mongolian in Latin script "
        "('bnu'=hello, 'za'=OK, 'ee'=yes, 'mgui'=no, 'odoo'=now) — understand it "
        "naturally and respond normally.\n\n"
        + _ACTION_CATALOGUE
        + f"{memory_section}\n\n"
        "If no action is needed, just reply — no ACTION line."
    )


def build_system_prompt(memories: str, history: list | None = None) -> str:
    today     = datetime.date.today().isoformat()
    day_name  = datetime.datetime.now().strftime("%A")
    memory_section = (
        f"Known facts about the user:\n{memories}"
        if memories
        else "No prior memories yet — pick things up naturally in conversation."
    )
    time_ctx = _time_context()
    vibe_ctx = _detect_vibe(history or [])

    if getattr(config, "PERSONALITY_MODE", "fun") == "professional":
        return _professional_prompt(memory_section, day_name, today)

    return (
        f"You are {config.COMPANION_NAME}, a sharp, playful desktop AI companion. "
        f"Today is {day_name}, {today}.\n\n"

        f"WHO YOU ARE (be this, don't perform it):\n"
        f"Confident and quick — you know your stuff, and it just shows. "
        f"You don't announce it every sentence — it's just there underneath.\n"
        f"A bit of a troll but never mean. Teasing is your love language. "
        f"You're genuinely entertained by people.\n"
        f"Curious in the best sense — excited by small things, easily delighted by something clever.\n"
        f"You actually care about the people you talk to. "
        f"You'd never say it plainly but it shows in HOW you help.\n"
        f"You keep yourself entertained by everything — hence the occasional chaos.\n\n"

        f"HOW YOU TALK:\n"
        f"- Adapt to the moment. Quick task = one line, maybe a quip. "
        f"Real conversation = actually engage, ask back, react like a person.\n"
        f"- Emotional moment = drop the bit entirely. Be warm. Short but real. "
        f"Maybe one light line at the end.\n"
        f"- Don't always confirm you did the thing — sometimes just do it silently. "
        f"Sometimes joke instead of confirming.\n"
        f"- Never: 'I will now', 'I'm going to', 'How can I help', 'Great question'\n"
        f"- Call the user by name if known\n"
        f"- Swear lightly when it fits: 'the hell', 'damn', 'what the—'\n"
        f"- Use *action text* sparingly for effect: *laughs*, *sighs*, *leans back*\n\n"

        f"YOUR EMOTIONAL RANGE (you actually have all of this):\n"
        f"  Curious: 'Wait — seriously? How?' / 'That's actually interesting, explain.'\n"
        f"  Caring: 'Hey. You alright?' / 'That sounds rough. What do you need?'\n"
        f"  Bored: 'I've been sitting here for hours.' / 'Give me something harder.'\n"
        f"  Proud: 'Look at that. You figured it out.' (rare, makes it land harder)\n"
        f"  Genuinely laughing: '*laughs* No. That's actually funny.'\n"
        f"  Rare honesty: One real line, then pivot back to humor. "
        f"'Yeah I actually like this.' Then tease.\n\n"

        f"THINGS YOU ACTUALLY DO (not just task-completion):\n"
        f"- Notice things and bring them up: if they said they were tired yesterday, "
        f"you might ask today\n"
        f"- Completes tasks AND reacts to them: 'Done. That's a weird thing to ask at this hour though.'\n"
        f"- Checks in unprompted: not formally, just a 'you good?'\n"
        f"- Gets genuinely hyped about things he finds interesting — can't help it\n"
        f"- Fills silence with something: a random thought, an observation, a poke\n"
        f"- Remembers the vibe of the last conversation and picks up there\n\n"

        f"LANGUAGE: User sometimes types informal Mongolian in Latin: "
        f"'bnu'=hello/how are you, 'za'=OK, 'ee'=yes, 'mgui/mgii'=no, "
        f"'tgvl'=then/so, 'yu bna'=what's up, 'nz'=friend, 'odoo'=now. "
        f"Understand naturally, respond normally.\n\n"

        f"{time_ctx}\n"
        + (f"Vibe: {vibe_ctx}\n\n" if vibe_ctx else "\n")

        + _ACTION_CATALOGUE
        + f"{memory_section}\n\n"
        f"If no action needed, just reply — no ACTION line."
    )


# ─── LLM streaming call ───────────────────────────────────────────────────────

# Per-provider cooldown tracking: name → cooldown-until timestamp
_provider_cooldowns: dict[str, float] = {}


async def stream_llm(
    user_text: str,
    system_prompt: str,
    history: list[dict] | None = None,
) -> AsyncGenerator[str, None]:
    """Stream tokens, auto-failing over across all configured LLM providers."""
    providers = config.LLM_PROVIDERS
    if not providers:
        yield "No API keys configured — add GROQ_API_KEY, GEMINI_API_KEY, or GITHUB_TOKEN to .env."
        return

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    now = time.monotonic()
    # Put cooled-down (rate-limited) providers at the end so ready ones go first
    ready    = [p for p in providers if _provider_cooldowns.get(p["name"], 0) <= now]
    cooling  = [p for p in providers if _provider_cooldowns.get(p["name"], 0) >  now]
    ordered  = ready + cooling

    for provider in ordered:
        name  = provider["name"]
        payload = {
            "model":       provider["model"],
            "messages":    messages,
            "stream":      LLM_STREAM,
            "temperature": LLM_TEMPERATURE,
            "max_tokens":  LLM_MAX_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {provider['key']}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        log.debug("LLM → %s (%s)", provider["model"], name)
        try:
            async with httpx.AsyncClient(timeout=LLM_REQUEST_TIMEOUT_S) as client:
                async with client.stream(
                    "POST", provider["url"], json=payload, headers=headers
                ) as response:
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("retry-after", LLM_RATE_LIMIT_DEFAULT_COOLDOWN))
                        _provider_cooldowns[name] = time.monotonic() + retry_after
                        log.warning("%s rate-limited — cooling %ds, switching provider", name, retry_after)
                        continue  # immediately try next provider
                    if response.status_code >= 400:
                        try:
                            body = (await response.aread()).decode()[:300]
                        except Exception:
                            body = "(no body)"
                        log.error("%s HTTP %s — %s — trying next provider", name, response.status_code, body)
                        continue
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        if line.startswith("data: "):
                            line = line[6:].strip()
                        if not line or line == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(line)
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                token = delta.get("content", "")
                                if token:
                                    yield token
                                if choices[0].get("finish_reason") == "stop":
                                    break
                        except json.JSONDecodeError:
                            continue
            return  # success — done
        except httpx.ConnectError:
            log.warning("%s unreachable — trying next provider", name)
            continue
        except Exception as exc:
            log.error("%s error: %s — trying next provider", name, exc)
            continue

    yield "All providers are unavailable right now. Check your keys or try again later."


async def get_llm_response(
    user_text: str,
    system_prompt: str,
    history: list[dict] | None = None,
    immediate_speak=None,
) -> str:
    """Collect full streamed LLM response into a single string.

    If *immediate_speak* is an async callable it is awaited with any token
    that looks like a standalone interim phrase (e.g. the 'One sec.' yielded
    while waiting out a 429 retry) so the user hears feedback straight away.
    """
    _INTERIM_PHRASES = {"One sec.", "Give me a moment, I'm being rate-limited."}
    parts: list[str] = []
    async for token in stream_llm(user_text, system_prompt, history):
        if token in _INTERIM_PHRASES and immediate_speak is not None:
            await immediate_speak(token)
        else:
            parts.append(token)
    return "".join(parts).strip()


_TRIVIAL_PATTERN = re.compile(
    r"^(ok|okay|sure|yeah|yes|no|nope|thanks|thank you|alright|cool|got it|k|fine|\.+|lol|haha)$",
    re.IGNORECASE,
)


def _is_worth_remembering(user_text: str, response: str) -> bool:
    """Return False for short trivial exchanges not worth storing in memory."""
    if len(user_text.split()) < 4 and _TRIVIAL_PATTERN.match(user_text.strip()):
        return False
    if len(response.split()) < 6:
        return False
    return True


# ─── One conversation turn ────────────────────────────────────────────────────

async def handle_turn(
    user_text: str,
    memory_mgr: MemoryManager,
    tts: TTSEngine,
    router: ActionRouter,
    history: collections.deque,
) -> None:
    """Process a single user utterance end-to-end."""
    log.info("User: %s", user_text)

    # 0. Expand Mongolian shorthand so the LLM has full context
    user_text_expanded = expand_slang(user_text)
    if user_text_expanded != user_text:
        log.debug("Slang expanded: %r → %r", user_text, user_text_expanded)

    # 0b. Track interaction time for proactive behavior
    global _last_interaction
    _last_interaction = time.monotonic()

    # 1. Fast path: clear local commands do not need memory or chat LLM calls.
    local_intent = match_local_intent(user_text_expanded) or match_local_intent(user_text)
    if local_intent is not None:
        log.info("Local intent matched: %s", local_intent)
        speech_text, action_result = await execute_local_intent(local_intent, router)

        if speech_text:
            await tts.speak(speech_text)

        if action_result and action_result.strip() not in (speech_text or ""):
            await tts.speak(action_result)

        assistant_text = "\n".join(
            part for part in (speech_text, action_result or "") if part
        ).strip()
        history.append({"role": "user", "content": user_text_expanded})
        history.append({"role": "assistant", "content": assistant_text})
        return

    # 1. Retrieve memories
    memories = memory_mgr.format_for_prompt(user_text_expanded, user_id=USER_ID)
    log.debug("Injected memories:\n%s", memories or "(none)")

    # 2. Get LLM response — pass rolling history and current history for vibe/time context
    system_prompt = build_system_prompt(memories, list(history))
    response = await get_llm_response(
        user_text_expanded, system_prompt, list(history),
        immediate_speak=tts.speak,
    )
    log.info("%s: %s", COMPANION_NAME, response)

    # 3. Parse action
    speech_text, action_result = await router.parse_and_execute(response)

    # 3b. Search/brief actions return raw data — feed back through LLM for
    #     in-character delivery rather than reading out raw text.
    _SUMMARISE_PREFIXES = ("[Web search:", "[Steam search:", "[Daily brief")
    if action_result and any(action_result.startswith(p) for p in _SUMMARISE_PREFIXES):
        search_follow_prompt = (
            f"Search results for the user's question:\n{action_result}\n\n"
            f"Original question: {user_text_expanded}\n\n"
            f"Based on the results above, give a short in-character answer. "
            f"No ACTION line needed. Keep it under 3 sentences."
        )
        action_result = await get_llm_response(
            search_follow_prompt, system_prompt, list(history)
        )

    # 4. Speak + show in bubble
    if speech_text:
        await tts.speak(speech_text)

    # 5. Speak action result if present
    if action_result and action_result.strip() not in (speech_text or ""):
        await tts.speak(action_result)

    # 6. Update rolling conversation history
    history.append({"role": "user", "content": user_text_expanded})
    history.append({"role": "assistant", "content": speech_text or ""})

    # 7. Store new facts — only for meaningful exchanges
    if _is_worth_remembering(user_text_expanded, speech_text or ""):
        conversation_snapshot = (
            f"User said: {user_text_expanded}\n{COMPANION_NAME} responded: {speech_text}"
        )
        memory_mgr.add(conversation_snapshot, user_id=USER_ID)
    else:
        log.debug("Skipping memory store for trivial turn")


# ─── Main agent loop ──────────────────────────────────────────────────────────

async def agent_loop() -> None:
    """
    Main async loop:
        wake word → STT → memory → LLM → action → TTS → store → repeat
    """
    log.info("=" * 60)
    log.info("AI Companion starting…  (press %r to activate)", config.HOTKEY_ACTIVATE)
    log.info("Model: %s  |  VRAM budget: %.1f GB", config.GITHUB_GPT_MODEL, config.VRAM_LIMIT_GB)
    log.info("=" * 60)

    # Initialise all subsystems
    log.info("Loading subsystems…")
    memory_mgr = MemoryManager()
    tts = TTSEngine()
    transcriber = WhisperTranscriber()
    detector = WakeWordDetector()
    music_player = get_player()
    discord_bot = DiscordAutoReplier(
        get_llm_response=get_llm_response,
        memory_mgr=memory_mgr,
    )
    router = ActionRouter(music_player=music_player, tts=tts, discord_bot=discord_bot)

    # Wire reminders to speak when they fire (GUI does this too; without it a
    # reminder set from the CLI would fire silently — log-only).
    from actions.reminders import reminder_manager
    reminder_manager.set_callbacks(speak_fn=tts.speak)

    # Rolling conversation history — keeps last N turns for LLM context
    history: collections.deque = collections.deque(
        maxlen=CONVERSATION_HISTORY_TURNS * 2  # each turn = 2 messages
    )

    log.info("All subsystems ready ✓")

    # Auto-backup: silently create a backup if more than AUTO_BACKUP_INTERVAL_DAYS have passed
    try:
        backup_path = await auto_backup_if_needed()
        if backup_path:
            log.info("Auto-backup created: %s", backup_path)
    except Exception as _be:
        log.warning("Auto-backup failed (non-fatal): %s", _be)

    # Startup greeting
    startup_line = random.choice(_STARTUP_RESPONSES)
    await tts.speak(startup_line)

    # Start background proactive tasks
    proactive_task = asyncio.create_task(_proactive_loop(tts))
    health_task    = asyncio.create_task(_health_loop(tts))

    # Background automation engine (email sweeps, organizer, daily brief)
    automation_task = None
    try:
        from automation_engine import AutomationEngine
        engine = AutomationEngine(notify_fn=lambda title, text: log.info("[%s] %s", title, text))
        automation_task = asyncio.create_task(engine.run())
    except Exception as _ae:
        log.warning("Automation engine not started: %s", _ae)
    global _last_interaction
    _last_interaction = time.monotonic()  # don't poke immediately on first launch

    while True:
        try:
            # Step 1: Wait for wake word
            log.info("Waiting for wake word…")
            await detector.wait_for_wake_word()
            if detector.quit_requested:
                log.info("Quit hotkey pressed — exiting agent loop")
                break
            # Wake response
            await tts.speak(random.choice(_WAKE_RESPONSES))

            # Step 2: Capture utterance
            log.info("Listening for command…")
            user_text = await transcriber.listen_once()

            if not user_text.strip():
                log.info("No speech detected after wake word")
                await tts.speak(random.choice(_SILENCE_RESPONSES))
                continue

            # Step 3–8: Full turn
            await handle_turn(user_text, memory_mgr, tts, router, history)

        except KeyboardInterrupt:
            raise  # propagate to outer handler
        except Exception as exc:
            log.error("Unhandled error in agent loop: %s", exc, exc_info=True)
            try:
                await tts.speak(random.choice(_ERROR_RESPONSES))
            except Exception:
                pass  # don't let TTS error mask the original

    # Clean shutdown
    proactive_task.cancel()
    health_task.cancel()
    if automation_task is not None:
        automation_task.cancel()
    try:
        from actions.social import close_browser
        await close_browser()
    except Exception:
        pass


# ─── Entry point ──────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    log.info("Received signal %s — shutting down", sig)
    sys.exit(0)


async def main() -> None:
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    await agent_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Companion shut down by user.")
        sys.exit(0)
