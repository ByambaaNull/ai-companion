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
import logging
import re
import signal
import sys
from typing import AsyncGenerator

import httpx

import config
from config import (
    COMPANION_NAME,
    LLM_CONTEXT_WINDOW,
    LLM_STREAM,
    LLM_TEMPERATURE,
    MEMORY_TOP_K,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    USER_ID,
    setup_logging,
    CONVERSATION_HISTORY_TURNS,
)
from memory import MemoryManager
from stt import WhisperTranscriber
from tts_rvc import TTSEngine
from wake_word import WakeWordDetector
from action_router import ActionRouter
from actions.music import MusicPlayer
from desktop_character import DesktopCharacter
from screen_watcher import ScreenWatcher
from personality import PersonalityReactor

log = logging.getLogger("main")

# ─── Prompt builder ───────────────────────────────────────────────────────────

def build_system_prompt(memories: str) -> str:
    today = datetime.date.today().isoformat()
    memory_section = (
        f"Known facts about the user:\n{memories}"
        if memories
        else "No prior memories of this user yet."
    )
    return (
        f"You are {COMPANION_NAME}, a local AI companion running on this machine.\n"
        f"Today is {today}.\n\n"
        f"You have access to the following actions — use exactly one per response when needed:\n"
        f"  ACTION: open_browser    | <url or search term>\n"
        f"  ACTION: play_music      | <song name or alias>\n"
        f"  ACTION: play_video      | <youtube search query>\n"
        f"  ACTION: screenshot      | full\n"
        f"  ACTION: type_text       | <text to type>\n"
        f"  ACTION: click           | <x>,<y>\n"
        f"  ACTION: press_key       | <key or combo e.g. enter, ctrl+c, alt+tab>\n"
        f"  ACTION: open_app        | <executable name>\n"
        f"  ACTION: open_steam      | (no param needed)\n"
        f"  ACTION: launch_game     | <game name e.g. cs2, dota 2, rust>\n"
        f"  ACTION: cs2_queue       | (launches CS2 via Steam)\n"
        f"  ACTION: accept_match    | (starts background watcher that auto-clicks the Accept button)\n"
        f"  ACTION: cancel_accept   | (stop the accept watcher)\n"
        f"  ACTION: swipe           | <up/down/left/right [pixels]> or <x1,y1,x2,y2>\n"
        f"  ACTION: scroll          | <up/down [clicks]>\n"
        f"  ACTION: double_tap      | <x,y> or blank for screen centre\n"
        f"  ACTION: pinch_zoom      | <in or out>\n\n"
        f"{memory_section}\n\n"
        f"Respond naturally and concisely. "
        f"If you need to take an action, include the ACTION line at the END of your response. "
        f"Never output more than one ACTION per response."
    )


# ─── LLM streaming call ───────────────────────────────────────────────────────

async def stream_llm(
    user_text: str,
    system_prompt: str,
    history: list[dict] | None = None,
) -> AsyncGenerator[str, None]:
    """
    Stream tokens from Ollama and yield accumulated text chunks.

    Uses httpx async streaming to avoid blocking the event loop.
    Handles Ollama unavailability gracefully.
    """
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": LLM_STREAM,
        "options": {
            "temperature": LLM_TEMPERATURE,
            "num_ctx": LLM_CONTEXT_WINDOW,
            "num_gpu": config.OLLAMA_NUM_GPU_LAYERS,
        },
    }

    url = f"{OLLAMA_BASE_URL}/api/chat"
    log.debug("LLM request → %s", OLLAMA_MODEL)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    import json as _json
                    try:
                        chunk = _json.loads(line)
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            yield token
                        if chunk.get("done"):
                            break
                    except _json.JSONDecodeError:
                        continue
    except httpx.ConnectError:
        log.error("Cannot reach Ollama at %s — is 'ollama serve' running?", OLLAMA_BASE_URL)
        yield "I'm sorry, my language model is not responding right now."
    except httpx.HTTPStatusError as exc:
        log.error("Ollama HTTP error: %s", exc)
        yield "I encountered an error with my language model."
    except Exception as exc:
        log.error("LLM stream error: %s", exc)
        yield "I ran into an unexpected error."


async def get_llm_response(
    user_text: str,
    system_prompt: str,
    history: list[dict] | None = None,
) -> str:
    """Collect full streamed LLM response into a single string."""
    parts: list[str] = []
    async for token in stream_llm(user_text, system_prompt, history):
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
    character: "DesktopCharacter | None" = None,
) -> None:
    """Process a single user utterance end-to-end."""
    log.info("User: %s", user_text)

    # 1. Retrieve memories
    memories = memory_mgr.format_for_prompt(user_text, user_id=USER_ID)
    log.debug("Injected memories:\n%s", memories or "(none)")

    # 2. Get LLM response — pass rolling history for context continuity
    system_prompt = build_system_prompt(memories)
    response = await get_llm_response(user_text, system_prompt, list(history))
    log.info("%s: %s", COMPANION_NAME, response)

    # 3. Parse action
    speech_text, action_result = await router.parse_and_execute(response)

    # 4. Speak + show in bubble
    if speech_text:
        if character:
            character.say(speech_text)
        await tts.speak(speech_text)

    # 5. Speak action result if present
    if action_result and action_result.strip() not in (speech_text or ""):
        await tts.speak(action_result)

    # 6. Update rolling conversation history
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": speech_text or ""})

    # 7. Store new facts — only for meaningful exchanges
    if _is_worth_remembering(user_text, speech_text or ""):
        conversation_snapshot = (
            f"User said: {user_text}\n{COMPANION_NAME} responded: {speech_text}"
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
    log.info("Model: %s  |  VRAM budget: %.1f GB", OLLAMA_MODEL, config.VRAM_LIMIT_GB)
    log.info("=" * 60)

    # Initialise all subsystems
    log.info("Loading subsystems…")
    memory_mgr = MemoryManager()
    tts = TTSEngine()
    transcriber = WhisperTranscriber()
    detector = WakeWordDetector()
    music_player = MusicPlayer()
    router = ActionRouter(music_player=music_player, tts=tts)
    # Rolling conversation history — keeps last N turns for LLM context
    history: collections.deque = collections.deque(
        maxlen=CONVERSATION_HISTORY_TURNS * 2  # each turn = 2 messages
    )

    # Desktop character + screen awareness
    character = DesktopCharacter()
    watcher = ScreenWatcher()
    reactor = PersonalityReactor(character)

    watcher_task = asyncio.create_task(watcher.run())
    reactor_task = asyncio.create_task(reactor.run(watcher))

    log.info("All subsystems ready ✓")

    # Startup greeting
    character.say("On standby. Call me when you need me.")
    await tts.speak("The strongest is on standby. Call me.")

    while True:
        try:
            # Step 1: Wait for wake word
            log.info("Waiting for wake word…")
            await detector.wait_for_wake_word()
            if detector.quit_requested:
                log.info("Quit hotkey pressed — exiting agent loop")
                break
            # Wake response — character perks up
            character.set_state("react")
            await tts.speak("Hmm? What do you want?")

            # Step 2: Capture utterance — character listens
            log.info("Listening for command…")
            character.start_listening()
            user_text = await transcriber.listen_once()
            character.stop_listening()

            if not user_text.strip():
                log.info("No speech detected after wake word")
                character.say("Say it properly.")
                await tts.speak("Say it properly.")
                continue

            # Step 3–8: Full turn — character talks while responding
            character.set_state("talk")
            await handle_turn(user_text, memory_mgr, tts, router, history, character)
            character.set_state("idle")

        except KeyboardInterrupt:
            raise  # propagate to outer handler
        except Exception as exc:
            log.error("Unhandled error in agent loop: %s", exc, exc_info=True)
            try:
                await tts.speak("Tsk, a problem. Continuing.")
            except Exception:
                pass  # don't let TTS error mask the original

    # Clean shutdown
    watcher.stop()
    reactor.stop()
    watcher_task.cancel()
    reactor_task.cancel()
    character.shutdown()


# ─── Entry point ──────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    log.info("Received signal %s — shutting down", sig)
    sys.exit(0)


async def main() -> None:
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)

    # Quick pre-flight: verify Ollama is reachable
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
        log.info("Ollama reachable ✓")
    except Exception as exc:
        log.warning(
            "Ollama not responding (%s). LLM calls will fail. "
            "Start with: ollama serve",
            exc,
        )

    await agent_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Companion shut down by user.")
        sys.exit(0)
