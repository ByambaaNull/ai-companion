"""
actions/clipboard_ai.py — Run an AI instruction over the clipboard contents.

The result REPLACES the clipboard (ready to paste) and is also returned.

Usage (via LLM actions):
    clipboard_ai | summarize
    clipboard_ai | translate to Mongolian
    clipboard_ai | fix                  ← grammar/spelling
    clipboard_ai | reply                ← draft a reply to the copied message
    clipboard_ai | explain
    clipboard_ai | rewrite this as a formal complaint     ← free-form
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_MAX_INPUT_CHARS = 8000

_SYSTEM = (
    "You transform text exactly as instructed. Output ONLY the transformed "
    "text — no preamble, no quotes around it, no explanations (unless the "
    "instruction itself asks for an explanation)."
)

_PRESETS: dict[str, str] = {
    "summarize": "Summarize the following text concisely: a 1-2 sentence "
                 "summary, then key points as short bullets if warranted.",
    "summarise": "Summarize the following text concisely: a 1-2 sentence "
                 "summary, then key points as short bullets if warranted.",
    "fix":       "Fix all grammar, spelling, and punctuation mistakes in the "
                 "following text. Keep the original meaning, tone, and "
                 "language. Output only the corrected text.",
    "reply":     "Draft a clear, polite reply to the following message. Match "
                 "its language and an appropriate level of formality.",
    "explain":   "Explain the following text in simple, plain terms a "
                 "non-expert can understand.",
}


def _build_instruction(param: str) -> str:
    instruction = param.strip()
    if not instruction:
        return _PRESETS["summarize"]
    key = instruction.lower()
    if key in _PRESETS:
        return _PRESETS[key]
    if key.startswith("translate"):
        return (f"{instruction}. Translate the following text. Output only "
                "the translation.")
    return instruction  # free-form


async def clipboard_ai(param: str) -> str:
    """Apply an AI instruction to the clipboard text; result goes back on it."""
    try:
        try:
            import pyperclip
        except ImportError:
            return "pip install pyperclip to enable clipboard actions."

        try:
            text = pyperclip.paste() or ""
        except Exception as exc:
            return f"Couldn't read the clipboard: {exc}"

        if not text.strip():
            return "Your clipboard is empty — copy some text first, then ask again."

        truncated = len(text) > _MAX_INPUT_CHARS
        text = text[:_MAX_INPUT_CHARS]

        instruction = _build_instruction(param)
        prompt = f"Instruction: {instruction}\n\nText:\n{text}"
        if truncated:
            prompt += "\n\n[Note: text truncated.]"

        from main import get_llm_response  # lazy — avoids circular import
        result = await get_llm_response(prompt, _SYSTEM)
        if not result:
            return "Got an empty response — try again in a moment."

        try:
            pyperclip.copy(result)
            note = "\n\n(Result copied to clipboard — ready to paste.)"
        except Exception as exc:
            log.warning("Couldn't write clipboard: %s", exc)
            note = "\n\n(Couldn't write it back to the clipboard, though.)"
        return result + note
    except Exception as exc:
        log.error("clipboard_ai failed: %s", exc, exc_info=True)
        return f"Clipboard AI failed: {exc}"
