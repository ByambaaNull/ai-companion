"""
actions/email_assistant.py — Read-only IMAP inbox triage.

Works with Gmail App Passwords (or any IMAP provider). Credentials come from
settings under "email": imap_host / address / app_password / max_fetch.

Strictly READ-ONLY: messages are fetched with BODY.PEEK on a readonly mailbox
so nothing is marked seen, moved, or deleted.

Usage (via LLM actions):
    sweep_inbox |              ← full categorised report of unread mail
    email_summary |            ← short counts + top 3 important subjects
"""

from __future__ import annotations

import asyncio
import email
import email.header
import imaplib
import logging
import re

import settings

log = logging.getLogger(__name__)

CATEGORIES = [
    "Important",
    "Action needed",
    "Personal",
    "Newsletter",
    "Advertisement",
    "Spam",
]

_SETUP_HELP = (
    "Email isn't set up yet. Open settings → Email tab and enter your address "
    "and an App Password (for Gmail: Google Account → Security → 2-Step "
    "Verification → App Passwords), then enable email. I only ever READ mail — "
    "nothing gets deleted or moved."
)

_BODY_CHARS = 500  # chars of body text fed to the classifier per message


# ─── Credentials ─────────────────────────────────────────────────────────────

def _email_config() -> dict | None:
    """Return IMAP config dict, or None when disabled / incomplete."""
    if not settings.get("email.enabled", False):
        return None
    host = (settings.get("email.imap_host", "imap.gmail.com") or "").strip()
    address = (settings.get("email.address", "") or "").strip()
    password = (settings.get("email.app_password", "") or "").strip()
    if not (host and address and password):
        return None
    try:
        max_fetch = int(settings.get("email.max_fetch", 25))
    except (TypeError, ValueError):
        max_fetch = 25
    return {"host": host, "address": address, "password": password,
            "max_fetch": max(1, max_fetch)}


# ─── IMAP fetch (blocking — run in executor) ─────────────────────────────────

def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    try:
        for chunk, enc in email.header.decode_header(value):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
    except Exception:
        return str(value)
    return " ".join(p.strip() for p in parts if p).strip()


def _extract_body(msg: email.message.Message) -> str:
    """First ~_BODY_CHARS characters of the text body."""
    def _decode_part(part) -> str:
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                return ""
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        except Exception:
            return ""

    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
                    "attachment" not in str(part.get("Content-Disposition", "")):
                text = _decode_part(part)
                if text:
                    break
        if not text:  # fall back to HTML, crudely stripped
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    text = re.sub(r"<[^>]+>", " ", _decode_part(part))
                    break
    else:
        text = _decode_part(msg)
        if msg.get_content_type() == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)

    text = re.sub(r"\s+", " ", text).strip()
    return text[:_BODY_CHARS]


def _fetch_unseen(cfg: dict) -> list[dict]:
    """Fetch UNSEEN messages read-only. Returns list of header/body dicts."""
    conn = imaplib.IMAP4_SSL(cfg["host"])
    try:
        conn.login(cfg["address"], cfg["password"])
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return []
        ids = data[0].split()
        if not ids:
            return []
        ids = ids[-cfg["max_fetch"]:]  # newest N

        messages: list[dict] = []
        for msg_id in reversed(ids):  # newest first
            try:
                status, payload = conn.fetch(msg_id, "(BODY.PEEK[])")
                if status != "OK" or not payload or payload[0] is None:
                    continue
                raw = payload[0][1]
                msg = email.message_from_bytes(raw)
                messages.append({
                    "from":    _decode_header(msg.get("From")),
                    "subject": _decode_header(msg.get("Subject")) or "(no subject)",
                    "date":    _decode_header(msg.get("Date")),
                    "body":    _extract_body(msg),
                })
            except Exception as exc:
                log.warning("Skipping unparseable message %s: %s", msg_id, exc)
        return messages
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ─── LLM classification (one call for the whole batch) ──────────────────────

_CLASSIFY_SYSTEM = (
    "You are an email triage assistant. Classify each email into exactly one "
    "category from this list: " + ", ".join(CATEGORIES) + ". "
    "Reply with ONE line per email, in this exact format and nothing else:\n"
    "<number> | <category> | <one-line gist (max 15 words)>"
)


async def _classify(messages: list[dict]) -> list[dict]:
    """Add 'category' and 'gist' keys to each message via a single LLM call."""
    lines = []
    for i, m in enumerate(messages, 1):
        lines.append(
            f"{i}. From: {m['from']}\n   Subject: {m['subject']}\n"
            f"   Body: {m['body'] or '(empty)'}"
        )
    prompt = f"Classify these {len(messages)} emails:\n\n" + "\n\n".join(lines)

    from main import get_llm_response  # lazy — avoids circular import
    raw = await get_llm_response(prompt, _CLASSIFY_SYSTEM)

    # Default everything, then overlay whatever parsed cleanly.
    for m in messages:
        m["category"] = "Personal"
        m["gist"] = (m["body"][:80] + "…") if len(m["body"]) > 80 else m["body"]

    cat_lookup = {c.lower(): c for c in CATEGORIES}
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        num_match = re.search(r"\d+", parts[0])
        if not num_match:
            continue
        idx = int(num_match.group()) - 1
        if not (0 <= idx < len(messages)):
            continue
        category = cat_lookup.get(parts[1].lower())
        if category:
            messages[idx]["category"] = category
        if len(parts) >= 3 and parts[2]:
            messages[idx]["gist"] = parts[2]
    return messages


async def _collect() -> tuple[str | None, list[dict]]:
    """Fetch + classify. Returns (error_or_status_string, classified_messages)."""
    cfg = _email_config()
    if cfg is None:
        return _SETUP_HELP, []

    loop = asyncio.get_running_loop()
    try:
        messages = await loop.run_in_executor(None, _fetch_unseen, cfg)
    except imaplib.IMAP4.error as exc:
        return (f"IMAP login failed ({exc}). Double-check the address and App "
                "Password in settings → Email tab."), []
    except Exception as exc:
        log.error("Email fetch failed: %s", exc)
        return f"Couldn't reach the mail server: {exc}", []

    if not messages:
        return "Inbox clear — no unread messages.", []

    try:
        messages = await _classify(messages)
    except Exception as exc:
        log.error("Email classification failed: %s", exc)
        # Still show what we fetched, just uncategorised.
        for m in messages:
            m.setdefault("category", "Personal")
            m.setdefault("gist", m["subject"])
    return None, messages


# ─── Public actions ──────────────────────────────────────────────────────────

async def sweep_inbox(param: str = "") -> str:
    """Full read-only triage report of unread mail, grouped by category."""
    try:
        err, messages = await _collect()
        if err is not None:
            return err

        out: list[str] = [f"Unread mail: {len(messages)} message"
                          f"{'s' if len(messages) != 1 else ''}"]
        for category in CATEGORIES:
            group = [m for m in messages if m.get("category") == category]
            if not group:
                continue
            out.append(f"\n{category} ({len(group)}):")
            for m in group:
                out.append(f"  • {m['subject']} — {m['from']}")
                if m.get("gist"):
                    out.append(f"      {m['gist']}")
        out.append("\n(Read-only sweep — everything is still marked unread.)")
        return "\n".join(out)
    except Exception as exc:
        log.error("sweep_inbox failed: %s", exc, exc_info=True)
        return f"Email sweep failed: {exc}"


async def email_summary(param: str = "") -> str:
    """Short version: counts per category + top 3 important subjects."""
    try:
        err, messages = await _collect()
        if err is not None:
            return err

        counts = []
        for category in CATEGORIES:
            n = sum(1 for m in messages if m.get("category") == category)
            if n:
                counts.append(f"{category}: {n}")
        out = [f"{len(messages)} unread — " + ", ".join(counts) + "."]

        top = [m for m in messages
               if m.get("category") in ("Important", "Action needed")][:3]
        if top:
            out.append("Top of the pile:")
            for m in top:
                out.append(f"  • {m['subject']} — {m['from']}")
        return "\n".join(out)
    except Exception as exc:
        log.error("email_summary failed: %s", exc, exc_info=True)
        return f"Email summary failed: {exc}"
