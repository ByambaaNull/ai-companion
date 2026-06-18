"""
actions/drafts.py — Draft emails, letters, and messages on request.

One LLM call with a professional-writing system prompt; the finished draft
is copied to the clipboard so the user can paste it straight into Gmail/Docs.

Usage (via LLM actions):
    write_draft | email to professor asking for a deadline extension
    write_draft | cover letter for a software internship at Unitel
    write_draft | bagshiin email bichij ogooch, margaash chamga ochij chadahgui gej
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_DRAFT_SYSTEM = (
    "You are an expert writing assistant. Write the requested draft (email, "
    "letter, message, cover letter, etc.) ready to send:\n"
    "- Clean, professional, natural wording; appropriate greeting and sign-off "
    "when the format calls for it.\n"
    "- Use placeholders like [Name] or [Date] only where information is "
    "genuinely missing.\n"
    "- Match the language of the request: requests in English get English "
    "drafts; requests in Mongolian — including informal Latin-script Mongolian "
    "(e.g. 'bagshiin email bichij ogooch') — get a proper, polished Mongolian "
    "draft in Cyrillic unless the user asks otherwise.\n"
    "- Output ONLY the draft itself: no preamble, no commentary, no markdown "
    "fences."
)


async def write_draft(param: str) -> str:
    """Draft a text from a description; result also lands on the clipboard."""
    try:
        description = param.strip()
        if not description:
            return ("Tell me what to write, e.g. write_draft | email to "
                    "professor asking for a deadline extension")

        from main import get_llm_response  # lazy — avoids circular import
        draft = await get_llm_response(
            f"Write this draft: {description}", _DRAFT_SYSTEM)
        if not draft:
            return "Couldn't generate the draft right now — try again in a moment."

        clip_note = ""
        try:
            import pyperclip
            pyperclip.copy(draft)
            clip_note = "\n\n(Copied to clipboard — ready to paste.)"
        except Exception as exc:
            log.warning("Couldn't copy draft to clipboard: %s", exc)

        return draft + clip_note
    except Exception as exc:
        log.error("write_draft failed: %s", exc, exc_info=True)
        return f"Couldn't write that draft: {exc}"
