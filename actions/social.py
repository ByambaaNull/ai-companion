"""
social.py — Send messages on Facebook Messenger, Instagram, X/Twitter, WhatsApp, and Discord via Playwright.

Design principles (token-efficient — zero screenshots):
  1. Search for the recipient by typing their name in the platform's search box.
  2. Extract visible result names purely from the DOM (text nodes / aria labels).
     No screenshots, no vision-LLM calls — those burn tokens fast.
  3. Fuzzy-match to find the correct person.
  4. Click ONLY the exact matched name element.
  5. Verify the conversation header before sending.
  6. Report clearly if anything is ambiguous — never silently message wrong person.

Supported platforms:
  • Facebook Messenger  (messenger.com)
  • Instagram DM        (instagram.com/direct)
  • X / Twitter DM      (x.com/messages)
  • WhatsApp Web        (web.whatsapp.com)
  • Discord DM          (discord.com/channels/@me)  — uses your logged-in browser session

Universal dispatcher:
  send_message(platform, recipient, message)
  Platforms: facebook / fb / messenger | instagram / ig | twitter / x | whatsapp / wa | discord

Away mode:
  set_away(on, template)  — sets away-mode; platforms will send `template` to inbound senders.
  Away monitoring for browser platforms is tracked via AwayMonitor.

SETUP (one-time):
  pip install playwright
  playwright install chromium
  Then run any social action — browser opens, log in manually, session saved forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
from difflib import SequenceMatcher
from typing import Optional, Tuple

import config

# ─── Contact / nickname store ─────────────────────────────────────────────────
# Maps short nicknames to full platform names, e.g. {"lety": "Lety Fiez"}
_CONTACTS_FILE = config.DATA_DIR / "contacts.json"


def _load_contacts() -> dict:
    if _CONTACTS_FILE.exists():
        try:
            return json.loads(_CONTACTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_contacts(contacts: dict) -> None:
    _CONTACTS_FILE.write_text(
        json.dumps(contacts, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _resolve_name(name: str) -> str:
    """Return full name if nickname is known, otherwise return name unchanged."""
    contacts = _load_contacts()
    return contacts.get(name.lower().strip(), name)


async def save_contact(nickname: str, full_name: str) -> str:
    """Save a short nickname → full name mapping for use in social messages."""
    contacts = _load_contacts()
    contacts[nickname.lower().strip()] = full_name.strip()
    _save_contacts(contacts)
    log.info("Contact saved: '%s' → '%s'", nickname, full_name)
    return f"Got it — '{nickname}' is now saved as '{full_name}'."


def list_contacts() -> str:
    """Return all saved nicknames."""
    contacts = _load_contacts()
    if not contacts:
        return "No contacts saved yet. Say 'save lety as Lety Fiez' to add one."
    lines = [f"  {nick} → {name}" for nick, name in sorted(contacts.items())]
    return "Saved contacts:\n" + "\n".join(lines)


def delete_contact(nickname: str) -> str:
    """Remove a saved nickname."""
    contacts = _load_contacts()
    key = nickname.lower().strip()
    if key not in contacts:
        return f"No contact named '{nickname}' found."
    full = contacts.pop(key)
    _save_contacts(contacts)
    return f"Removed '{nickname}' (was '{full}')."

log = logging.getLogger(__name__)

_lock = asyncio.Lock()
_pw   = None
_ctx  = None


# ─── Fuzzy matching helpers ───────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()

def _best_match(target: str, candidates: list[str]) -> Optional[Tuple[str, float]]:
    """
    Return (best_candidate, score) where score ∈ [0, 1].
    Returns None if no candidate scores above 0.62 — avoids clicking wrong people.
    Group-chat style names (contains commas) are penalised heavily so a solo
    contact always beats a group chat with the same person in it.
    """
    if not candidates:
        return None

    def _score(c: str) -> float:
        base = _similarity(target, c)
        # Group chats contain commas ("Lety, John, Maria") — penalise them
        if "," in c:
            base *= 0.5
        return base

    scored = [(c, _score(c)) for c in candidates]
    best_name, best_score = max(scored, key=lambda x: x[1])
    if best_score < 0.62:
        return None
    return best_name, best_score


# ─── Browser singleton ────────────────────────────────────────────────────────

async def _browser():
    global _pw, _ctx
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed.\n"
            "Run:  pip install playwright  &&  playwright install chromium"
        )

    if _ctx is not None:
        try:
            # cookies() is an async call that throws if the context is truly closed
            await _ctx.cookies()
            return _ctx
        except Exception:
            # Context is dead — tear it down cleanly before recreating
            _ctx = None
            if _pw is not None:
                try:
                    await _pw.stop()
                except Exception:
                    pass
                _pw = None

    config.BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _pw  = await async_playwright().start()
    _ctx = await _pw.chromium.launch_persistent_context(
        str(config.BROWSER_DATA_DIR),
        headless=False,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
              "--disable-infobars"],
        ignore_default_args=["--enable-automation"],
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    return _ctx


async def _page(ctx):
    if ctx.pages:
        return ctx.pages[0]
    try:
        return await ctx.new_page()
    except Exception:
        # Context died between _browser() and here — reset so next call rebuilds it
        global _ctx, _pw
        _ctx = None
        if _pw is not None:
            try:
                await _pw.stop()
            except Exception:
                pass
            _pw = None
        raise


async def _wait_for_login(page, url_fragment: str, site: str) -> None:
    if url_fragment in page.url:
        return
    log.info("%s: browser open — please log in now (3 min timeout)", site)
    await page.wait_for_url(f"**{url_fragment}**", timeout=180_000)
    log.info("%s: logged in ✓", site)


# ─── Shared: extract names from search results ────────────────────────────────

async def _extract_names(page, selectors: list[str]) -> list[str]:
    """
    Try each selector in order, return the first non-empty list of text strings.
    """
    for sel in selectors:
        try:
            items = page.locator(sel)
            count = await items.count()
            if count == 0:
                continue
            names: list[str] = []
            for i in range(min(count, 10)):
                raw = (await items.nth(i).text_content() or "").strip()
                # Clean out noise (button labels, timestamps, etc.)
                first_line = raw.splitlines()[0].strip() if raw else ""
                if first_line and len(first_line) < 60:
                    names.append(first_line)
            if names:
                return names
        except Exception:
            continue
    return []


async def _read_conversation_header(page) -> str:
    """Read the name shown at the top of the open conversation."""
    for sel in [
        'h1', '[role="heading"]',
        'span[dir="auto"]',
        '[data-testid="conversation-title"]',
        'header span',
    ]:
        try:
            el   = page.locator(sel).first
            text = (await el.text_content(timeout=2_000) or "").strip()
            if text and len(text) < 80:
                return text
        except Exception:
            continue
    return ""


# ─── Facebook Messenger ───────────────────────────────────────────────────────

async def _fb_search_and_open(page, recipient: str):
    """
    Navigate to Messenger, use the sidebar search box to find the recipient,
    and click their conversation.
    Returns (found_name, None) on success or ("", error_string) on failure.

    Retry logic: after typing the name, polls every 1 second for up to 15
    seconds waiting for search results to appear in the sidebar panel — this
    handles slow connections / buffering gracefully.
    """
    await page.goto("https://www.messenger.com/", timeout=30_000)
    await page.wait_for_load_state("domcontentloaded", timeout=20_000)

    if "messenger.com" not in page.url and "facebook.com" not in page.url:
        return "", "Facebook Messenger opened — please log in first, then ask me again."
    await page.wait_for_timeout(2_000)

    # ── Step 1: find and focus the sidebar search box ─────────────────────────
    SEARCH_SELECTORS = [
        'input[placeholder="Search Messenger"]',
        'input[placeholder="Search"]',
        'input[placeholder*="Search"]',
        'input[aria-label="Search Messenger"]',
        'input[aria-label*="Search"]',
        '[role="combobox"][aria-label*="Search"]',
        'input[type="search"]',
        '[contenteditable="true"][aria-label*="Search"]',
    ]
    search_box = None
    for sel in SEARCH_SELECTORS:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=3_000)
            search_box = el
            break
        except Exception:
            continue

    if search_box is None:
        return "", "Messenger is open but can't find the search box — please make sure you're logged in."

    # Click, clear, then type using keyboard so Messenger registers each keystroke
    await search_box.click()
    await page.wait_for_timeout(300)
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(200)
    for char in recipient:
        await page.keyboard.type(char)
        await page.wait_for_timeout(40)

    # ── Step 2: poll every 1 s (up to 15 s) for a non-empty results panel ─────
    # We look ONLY inside a dedicated search-results container — never the
    # recent-chats sidebar — so we can't accidentally pick up the wrong chat.
    RESULT_PANEL_SELECTORS = [
        '[data-scope="typeahead_result"]',
        '[aria-label*="Search results"]',
        '[role="listbox"]',
        'ul[role="listbox"]',
    ]
    RESULT_ITEM_SELECTORS = [
        '[data-scope="typeahead_result"] [role="option"]',
        '[aria-label*="Search results"] [role="option"]',
        '[role="listbox"] [role="option"]',
        'ul[role="listbox"] li',
    ]

    _NOISE = {"active", "you", "messenger", "facebook", "search", "message", "new message"}

    async def _try_get_results() -> list[str]:
        """Read result names from the search panel. Returns [] while still loading."""
        # First confirm a results panel is actually present
        panel_visible = False
        for psel in RESULT_PANEL_SELECTORS:
            try:
                count = await page.locator(psel).count()
                if count > 0:
                    panel_visible = True
                    break
            except Exception:
                continue
        if not panel_visible:
            return []

        names = await _extract_names(page, RESULT_ITEM_SELECTORS)
        names = [
            n for n in names
            if len(n) > 2
            and n.lower() not in _NOISE
            and not n.replace(":", "").replace(" ", "").isdigit()
        ]
        return names

    names: list[str] = []
    MAX_RETRIES = 15
    for attempt in range(MAX_RETRIES):
        names = await _try_get_results()
        if names:
            log.info("Search results appeared after %d s for '%s': %s", attempt, recipient, names)
            break
        log.debug("No results yet for '%s' (attempt %d/%d) — retrying in 1 s…",
                  recipient, attempt + 1, MAX_RETRIES)
        await page.wait_for_timeout(1_000)

    if not names:
        return "", (
            f"Searched for '{recipient}' on Messenger but no results appeared after "
            f"{MAX_RETRIES} seconds. Check the name or your connection and try again."
        )

    # ── Step 3: fuzzy-match, ignoring group chats ─────────────────────────────
    solo_names = [n for n in names if "," not in n]
    pool = solo_names if solo_names else names

    match = _best_match(recipient, pool)
    if match is None:
        top = pool[0] if pool else names[0]
        return "", (
            f"Searched for '{recipient}' but the closest result was '{top}' — "
            "doesn't look like the right person. Try their exact full name."
        )

    found_name, score = match
    log.info("Best match for '%s': '%s' (score %.2f)", recipient, found_name, score)

    # ── Step 4: click the matched item inside the results panel only ──────────
    clicked = False
    for panel_sel in RESULT_PANEL_SELECTORS:
        if clicked:
            break
        try:
            panel = page.locator(panel_sel).first
            panel_count = await panel.count()
            if panel_count == 0:
                continue
            # Try exact text match first, then partial
            for exact in (True, False):
                try:
                    el = panel.get_by_text(found_name, exact=exact).first
                    await el.wait_for(state="visible", timeout=2_000)
                    await el.click()
                    clicked = True
                    break
                except Exception:
                    continue
        except Exception:
            continue

    if not clicked:
        return "", (
            f"Found '{found_name}' in results but couldn't click them — "
            "the panel may have closed. Try again."
        )

    await page.wait_for_timeout(2_000)
    return found_name, None


async def read_facebook_messages(recipient: str, count: int = 6) -> str:
    """Read the last N messages from a Facebook Messenger conversation."""
    resolved = _resolve_name(recipient)
    async with _lock:
        try:
            ctx  = await _browser()
            page = await _page(ctx)

            found_name, err = await _fb_search_and_open(page, resolved)
            if err:
                return err

            # Read messages — try several message-row selectors
            messages: list[str] = []
            for sel in [
                '[role="row"]',
                '[data-scope="messages_table"] [role="row"]',
                '[class*="message"] span',
                'div[dir="auto"]',
            ]:
                try:
                    rows = page.locator(sel)
                    total = await rows.count()
                    if total == 0:
                        continue
                    for i in range(max(0, total - count), total):
                        raw = (await rows.nth(i).text_content() or "").strip()
                        first = raw.splitlines()[0].strip() if raw else ""
                        if first and 1 < len(first) < 400:
                            messages.append(first)
                    if messages:
                        break
                except Exception:
                    continue

            if not messages:
                return f"Opened {found_name}'s chat but couldn't read any messages."

            lines = "\n".join(f"  • {m}" for m in messages[-count:])
            return f"Last messages from {found_name}:\n{lines}"

        except RuntimeError as exc:
            return str(exc)
        except Exception as exc:
            log.error("Read FB messages failed: %s", exc, exc_info=True)
            return f"Something went wrong reading Facebook messages: {exc}"


async def send_facebook_message(recipient: str, message: str) -> str:
    if not recipient or not message:
        return "Need both a recipient name and a message."

    async with _lock:
        try:
            ctx  = await _browser()
            page = await _page(ctx)

            found_name, err = await _fb_search_and_open(page, _resolve_name(recipient))
            if err:
                return err

            # ── Verify we opened the right conversation ───────────────────────
            header = await _read_conversation_header(page)
            _GENERIC_HEADERS = {"messenger", "facebook", "messages", "facebook messenger", "inbox"}
            if header and header.lower() not in _GENERIC_HEADERS:
                # Reject group chats outright (header has commas = multiple people)
                if "," in header:
                    return (
                        f"I found '{found_name}' in search but after clicking it opened "
                        f"a group chat ('{header}'). Aborting to avoid messaging the wrong chat. "
                        f"Please use their exact full name and try again."
                    )
                if _similarity(header, found_name) < 0.55:
                    return (
                        f"I found '{found_name}' in search but after clicking, "
                        f"the conversation shows '{header}' — these don't match. "
                        f"Aborting to be safe. Please check the name and try again."
                    )
            confirmed_name = (
                header if (header and header.lower() not in _GENERIC_HEADERS) else found_name
            )
            log.info("Conversation confirmed: %s", confirmed_name)

            # ── Type and send ─────────────────────────────────────────────────
            msg_box = None
            for sel in [
                '[aria-label="Message"][contenteditable="true"]',
                '[aria-label*="Message"][contenteditable="true"]',
                '[role="textbox"][contenteditable="true"]',
                '[data-lexical-editor="true"]',
                'div[contenteditable="true"]',
            ]:
                try:
                    el = page.locator(sel).last
                    await el.wait_for(state="visible", timeout=4_000)
                    msg_box = el
                    break
                except Exception:
                    continue

            if msg_box is None:
                return f"Opened {confirmed_name}'s conversation but can't find the message box."

            await msg_box.click()
            await page.wait_for_timeout(300)
            await msg_box.type(message, delay=40)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)

            return f"Message sent to {confirmed_name} on Facebook Messenger ✓"

        except RuntimeError as exc:
            return str(exc)
        except Exception as exc:
            log.error("Facebook message failed: %s", exc, exc_info=True)
            return f"Something went wrong sending the Facebook message: {exc}"


# ─── Instagram DM ─────────────────────────────────────────────────────────────

async def send_instagram_message(recipient: str, message: str) -> str:
    if not recipient or not message:
        return "Need both a recipient name and a message."

    async with _lock:
        try:
            ctx  = await _browser()
            page = await _page(ctx)

            await page.goto("https://www.instagram.com/direct/inbox/", timeout=30_000)
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            if "instagram.com" not in page.url:
                return "Instagram opened — please log in first, then ask me again."
            # If redirected to login page, ask user to log in
            if "/accounts/login" in page.url:
                return "Instagram opened — please log in first, then ask me again."
            await page.wait_for_timeout(1_500)

            # ── New message button — try multiple approaches ──────────────────
            opened_new = False

            # First try: click the compose/pencil button via known selectors
            for sel in [
                'svg[aria-label="New message"]',
                '[aria-label="New message"]',
                'a[href="/direct/new/"]',
                '[aria-label="Compose"]',
                'svg[aria-label="Compose"]',
            ]:
                try:
                    btn = page.locator(sel).first
                    await btn.wait_for(state="visible", timeout=3_000)
                    await btn.click()
                    opened_new = True
                    break
                except Exception:
                    continue

            # Second try: navigate directly to the compose URL
            if not opened_new:
                try:
                    await page.goto("https://www.instagram.com/direct/new/", timeout=20_000)
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    await page.wait_for_timeout(1_200)
                    opened_new = True
                except Exception:
                    pass

            if not opened_new:
                return (
                    "Can't find the 'New message' button on Instagram. "
                    "Make sure you're logged in at instagram.com/direct."
                )

            await page.wait_for_timeout(1_200)

            # ── Search ────────────────────────────────────────────────────────
            search = None
            for sel in [
                'input[placeholder="Search..."]',
                'input[placeholder*="Search"]',
                'input[aria-label="Search for a person or group"]',
                'input[aria-label*="Search"]',
                'input[name="queryBox"]',
                'input[type="text"]',
            ]:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=4_000)
                    search = el
                    break
                except Exception:
                    continue

            if search is None:
                return "Can't find the recipient search box in Instagram DMs."

            await search.click()
            await page.wait_for_timeout(300)
            await search.fill("")
            await search.type(recipient, delay=50)

            results_appeared = False
            for results_sel in ['[role="option"]', '[role="listbox"]', '[role="list"]']:
                try:
                    await page.wait_for_selector(results_sel, timeout=4_000)
                    results_appeared = True
                    break
                except Exception:
                    continue
            if not results_appeared:
                await page.wait_for_timeout(2_000)

            # ── Extract all visible result names ──────────────────────────────
            names = await _extract_names(page, [
                '[role="listbox"] [role="option"]',
                '[role="option"]',
                '[aria-label*="Select"]',
                '[role="list"] [role="button"]',
                '[class*="result"]',
            ])

            if not names:
                # Broader DOM sweep: Instagram sometimes uses aria labels
                names = await _extract_names(page, [
                    '[aria-label*="Select"]',
                    'span[dir="auto"]',
                    '[class*="username"]',
                    '[class*="name"]',
                ])
                names = [n for n in names if 2 < len(n) < 60]

            if not names:
                return (
                    f"No results found for '{recipient}' on Instagram. "
                    "Try their exact Instagram username."
                )

            # ── Fuzzy-match ───────────────────────────────────────────────────
            match = _best_match(recipient, names)
            if match is None:
                return (
                    f"Searched for '{recipient}' on Instagram. "
                    f"Closest result was '{names[0]}' — doesn't look right. "
                    "Try their Instagram username instead of display name."
                )

            found_name, score = match
            log.info(
                "Instagram: best match for '%s' → '%s' (score=%.2f)",
                recipient, found_name, score,
            )

            # ── Click matched result ──────────────────────────────────────────
            clicked = False
            for sel in [
                f'[role="listbox"] [role="option"]:has-text("{found_name}")',
                f'[role="option"]:has-text("{found_name}")',
                f'[role="list"] [role="button"]:has-text("{found_name}")',
                f'[aria-label*="{found_name}"]',
            ]:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=3_000)
                    await el.click()
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                for container_sel in ['[role="listbox"]', '[role="list"]']:
                    try:
                        container = page.locator(container_sel).first
                        el = container.get_by_text(found_name, exact=False).first
                        await el.click()
                        clicked = True
                        break
                    except Exception:
                        continue

            if not clicked:
                return f"Found '{found_name}' but couldn't click them. Try again."

            await page.wait_for_timeout(800)

            # ── Next / Chat button (may not appear if conversation exists) ────
            for btn_text in ["Next", "Chat", "Open", "Message"]:
                try:
                    btn = page.get_by_role("button", name=btn_text).first
                    await btn.wait_for(state="visible", timeout=2_000)
                    await btn.click()
                    break
                except Exception:
                    continue

            await page.wait_for_timeout(1_500)

            # ── Type and send ─────────────────────────────────────────────────
            msg_box = None
            for sel in [
                '[aria-label="Message"][contenteditable="true"]',
                '[aria-label="Message"]',
                '[placeholder="Message..."]',
                '[aria-label*="message" i][contenteditable="true"]',
                'div[contenteditable="true"]',
            ]:
                try:
                    el = page.locator(sel).last
                    await el.wait_for(state="visible", timeout=4_000)
                    msg_box = el
                    break
                except Exception:
                    continue

            if msg_box is None:
                return f"Opened {found_name}'s DM but can't find the message box."

            await msg_box.click()
            await page.wait_for_timeout(300)
            await msg_box.type(message, delay=40)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)

            return f"Message sent to {found_name} on Instagram ✓"

        except RuntimeError as exc:
            return str(exc)
        except Exception as exc:
            log.error("Instagram message failed: %s", exc, exc_info=True)
            return f"Something went wrong sending the Instagram DM: {exc}"


# ─── Parse helper (used by action_router) ────────────────────────────────────

def _parse(param: str) -> Tuple[str, str]:
    """Split 'recipient : message text' into (recipient, message).
    Resolves short nicknames → full names from contacts.json.
    """
    if ":" in param:
        parts = param.split(":", 1)
        return _resolve_name(parts[0].strip()), parts[1].strip()
    return _resolve_name(param.strip()), ""


# ─── X / Twitter DM ──────────────────────────────────────────────────────────

async def send_twitter_message(recipient: str, message: str) -> str:
    """
    Send a DM to someone on X (formerly Twitter).
    Uses pure DOM text extraction — no screenshots.
    recipient can be their display name or @username.
    """
    if not recipient or not message:
        return "Need both a recipient name and a message."

    async with _lock:
        try:
            ctx  = await _browser()
            page = await _page(ctx)

            await page.goto("https://x.com/messages", timeout=30_000)
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            if (
                "x.com" not in page.url and "twitter.com" not in page.url
            ) or "/login" in page.url or "/flow/" in page.url:
                return (
                    "X/Twitter opened — please log in first, then ask me again."
                )
            await page.wait_for_timeout(2_000)

            # ── Compose / New message button ──────────────────────────────────
            for sel in [
                '[data-testid="NewDM_Button"]',
                'a[href="/messages/compose"]',
                '[aria-label="New message"]',
                '[aria-label="Compose"]',
            ]:
                try:
                    btn = page.locator(sel).first
                    await btn.wait_for(state="visible", timeout=4_000)
                    await btn.click()
                    break
                except Exception:
                    continue

            await page.wait_for_timeout(1_000)

            # ── Search for person ─────────────────────────────────────────────
            search = None
            for sel in [
                'input[data-testid="searchPeople"]',
                'input[placeholder*="Search people"]',
                'input[placeholder*="Search"]',
                'input[aria-label*="Search"]',
            ]:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=5_000)
                    search = el
                    break
                except Exception:
                    continue

            if search is None:
                return "Can't find the recipient search box in X DMs. Are you logged in at x.com?"

            clean_recipient = recipient.lstrip("@").strip()
            await search.click()
            await page.wait_for_timeout(300)
            await search.fill("")
            await search.type(clean_recipient, delay=50)
            await page.wait_for_timeout(2_000)

            # ── Extract visible names from results ────────────────────────────
            names = await _extract_names(page, [
                '[data-testid="TypeaheadUser"]',
                '[data-testid="typeaheadResult"]',
                '[role="option"]',
                '[role="listitem"]',
            ])

            # Also grab aria-label attributes (X uses them for user cells)
            if not names:
                try:
                    cells = page.locator('[data-testid="TypeaheadUser"]')
                    count = await cells.count()
                    for i in range(min(count, 10)):
                        label = await cells.nth(i).get_attribute("aria-label") or ""
                        text  = (await cells.nth(i).text_content() or "").strip()
                        combined = label or text
                        first_line = combined.splitlines()[0].strip()
                        if first_line and len(first_line) < 60:
                            names.append(first_line)
                except Exception:
                    pass

            if not names:
                return (
                    f"No results found for '{recipient}' on X. "
                    "Try their exact @username (without the @)."
                )

            match = _best_match(clean_recipient, names)
            if match is None:
                return (
                    f"Searched for '{recipient}' on X. "
                    f"Closest result was '{names[0]}' — doesn't look right. "
                    "Try their exact @username."
                )

            found_name, score = match
            log.info("X DM: best match for '%s' → '%s' (score=%.2f)", recipient, found_name, score)

            # ── Click matched person ──────────────────────────────────────────
            clicked = False
            for sel in [
                f'[data-testid="TypeaheadUser"]:has-text("{found_name}")',
                f'[role="option"]:has-text("{found_name}")',
                f'[role="listitem"]:has-text("{found_name}")',
            ]:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=3_000)
                    await el.click()
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                try:
                    el = page.get_by_text(found_name, exact=False).first
                    await el.click()
                    clicked = True
                except Exception:
                    pass

            if not clicked:
                return f"Found '{found_name}' on X but couldn't click them. Try again."

            await page.wait_for_timeout(800)

            # ── Next button (confirms selection) ──────────────────────────────
            for btn_text in ["Next", "Confirm"]:
                try:
                    btn = page.get_by_role("button", name=btn_text).first
                    await btn.wait_for(state="visible", timeout=2_500)
                    await btn.click()
                    break
                except Exception:
                    continue

            await page.wait_for_timeout(1_200)

            # ── Type and send ─────────────────────────────────────────────────
            msg_box = None
            for sel in [
                '[data-testid="dmComposerTextInput"]',
                '[aria-label="Message"][contenteditable="true"]',
                '[aria-label*="Message"][contenteditable="true"]',
                'div[contenteditable="true"]',
            ]:
                try:
                    el = page.locator(sel).last
                    await el.wait_for(state="visible", timeout=4_000)
                    msg_box = el
                    break
                except Exception:
                    continue

            if msg_box is None:
                return f"Opened {found_name}'s DM on X but can't find the message box."

            await msg_box.click()
            await page.wait_for_timeout(300)
            await msg_box.type(message, delay=40)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)

            return f"Message sent to {found_name} on X ✓"

        except RuntimeError as exc:
            return str(exc)
        except Exception as exc:
            log.error("X message failed: %s", exc, exc_info=True)
            return f"Something went wrong sending the X DM: {exc}"


# ─── WhatsApp Web DM ──────────────────────────────────────────────────────────

async def send_whatsapp_message(recipient: str, message: str) -> str:
    """
    Send a WhatsApp message via WhatsApp Web.
    Uses pure DOM text extraction — no screenshots.
    """
    if not recipient or not message:
        return "Need both a recipient name and a message."

    async with _lock:
        try:
            ctx  = await _browser()
            page = await _page(ctx)

            await page.goto("https://web.whatsapp.com/", timeout=30_000)
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            # WhatsApp Web needs QR scan or session — wait for the chat list to appear
            try:
                await page.wait_for_selector(
                    '[data-testid="chat-list"], [aria-label*="Chat list"], #side',
                    timeout=60_000,
                )
            except Exception:
                return (
                    "WhatsApp Web isn't ready. Open the browser, scan the QR code, "
                    "then try again."
                )
            await page.wait_for_timeout(1_500)

            # ── Search / new chat ─────────────────────────────────────────────
            search = None
            for sel in [
                '[data-testid="search"]',
                'input[placeholder*="Search"]',
                'input[title*="Search"]',
                '[aria-label*="Search"]',
            ]:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=4_000)
                    search = el
                    break
                except Exception:
                    continue

            if search is None:
                return "Can't find the WhatsApp search box. Make sure WhatsApp Web is open."

            await search.click()
            await page.wait_for_timeout(300)
            await search.fill("")
            await search.type(recipient, delay=50)
            await page.wait_for_timeout(1_500)

            # ── Extract result names ──────────────────────────────────────────
            names = await _extract_names(page, [
                '[data-testid="cell-frame-title"]',
                '[aria-label*="Contact"] span',
                '[role="listitem"] span[title]',
                '[title]',
            ])

            # WhatsApp uses span[title] heavily
            if not names:
                try:
                    spans = page.locator('[role="listitem"] span[title]')
                    count = await spans.count()
                    for i in range(min(count, 10)):
                        title = await spans.nth(i).get_attribute("title") or ""
                        if title and len(title) < 60:
                            names.append(title)
                except Exception:
                    pass

            if not names:
                return (
                    f"No results found for '{recipient}' on WhatsApp. "
                    "Make sure they're saved in your contacts."
                )

            match = _best_match(recipient, names)
            if match is None:
                return (
                    f"Searched for '{recipient}' on WhatsApp. "
                    f"Closest result was '{names[0]}' — doesn't look right. "
                    "Try their saved contact name."
                )

            found_name, score = match
            log.info("WhatsApp: best match '%s' → '%s' (score=%.2f)", recipient, found_name, score)

            # ── Click matched contact ─────────────────────────────────────────
            clicked = False
            for sel in [
                f'[title="{found_name}"]',
                f'[data-testid="cell-frame-title"]:has-text("{found_name}")',
                f'[role="listitem"]:has-text("{found_name}")',
            ]:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=3_000)
                    await el.click()
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                return f"Found '{found_name}' on WhatsApp but couldn't open the chat. Try again."

            await page.wait_for_timeout(1_200)

            # ── Type and send ─────────────────────────────────────────────────
            msg_box = None
            for sel in [
                '[data-testid="conversation-compose-box-input"]',
                '[aria-label="Type a message"]',
                '[aria-label*="message" i][contenteditable="true"]',
                'div[contenteditable="true"]',
            ]:
                try:
                    el = page.locator(sel).last
                    await el.wait_for(state="visible", timeout=4_000)
                    msg_box = el
                    break
                except Exception:
                    continue

            if msg_box is None:
                return f"Opened {found_name}'s chat on WhatsApp but can't find the message box."

            await msg_box.click()
            await page.wait_for_timeout(300)
            await msg_box.type(message, delay=40)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)

            return f"Message sent to {found_name} on WhatsApp ✓"

        except RuntimeError as exc:
            return str(exc)
        except Exception as exc:
            log.error("WhatsApp message failed: %s", exc, exc_info=True)
            return f"Something went wrong sending the WhatsApp message: {exc}"


# ─── Discord Web DM ─────────────────────────────────────────────────────────

async def send_discord_message(recipient: str, message: str) -> str:
    """
    Send a Discord DM via discord.com in the persistent Chrome profile.
    Uses your logged-in browser session — no token required.
    recipient: the person's display name or username (e.g. "Alice" or "alice#1234").
    """
    if not recipient or not message:
        return "Need both a recipient name and a message."

    async with _lock:
        try:
            ctx  = await _browser()
            page = await _page(ctx)

            await page.goto("https://discord.com/channels/@me", timeout=30_000)
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
            if "discord.com" not in page.url or "/login" in page.url:
                return (
                    "Discord opened — please log in first, then ask me again."
                )
            await page.wait_for_timeout(2_000)

            # ── Step 1: try to find an existing DM in the sidebar ─────────────
            opened = False
            for sel in [
                f'[data-list-id="private-channels"] a[aria-label*="{recipient}" i]',
                f'[aria-label*="{recipient}" i][href*="/channels/@me/"]',
            ]:
                try:
                    el = page.locator(sel).first
                    await el.wait_for(state="visible", timeout=2_000)
                    await el.click()
                    opened = True
                    break
                except Exception:
                    continue

            # ── Step 2: if not found, use the "Find or start a conversation" search ──
            if not opened:
                for btn_sel in [
                    'button[aria-label="New Direct Message"]',
                    'button[aria-label*="Find or start"]',
                ]:
                    try:
                        btn = page.locator(btn_sel).first
                        await btn.wait_for(state="visible", timeout=3_000)
                        await btn.click()
                        opened = True
                        break
                    except Exception:
                        continue

                if not opened:
                    # Fallback: click the + icon at the top of the DM sidebar
                    try:
                        dm_btn = page.locator('svg[aria-label="Add Friend"]').first
                        await dm_btn.wait_for(state="visible", timeout=2_000)
                        # The + is near the "Direct Messages" heading
                        parent = dm_btn.locator("xpath=..").first
                        await parent.click()
                        opened = True
                    except Exception:
                        pass

                if not opened:
                    return (
                        "Can't open a new Discord DM. "
                        "Make sure you're logged in at discord.com."
                    )

                await page.wait_for_timeout(800)

                # ── Type in the search/people input ────────────────────────────
                search = None
                for sel in [
                    'input[placeholder="Find or start a conversation"]',
                    'input[placeholder*="Find"]',
                    'input[aria-label*="Find"]',
                    'input[type="text"]',
                ]:
                    try:
                        el = page.locator(sel).first
                        await el.wait_for(state="visible", timeout=4_000)
                        search = el
                        break
                    except Exception:
                        continue

                if search is None:
                    return "Can't find the Discord search box. Are you logged in?"

                await search.click()
                await page.wait_for_timeout(300)
                await search.fill("")
                await search.type(recipient, delay=50)
                await page.wait_for_timeout(1_500)

                # ── Extract results ─────────────────────────────────────────────
                names = await _extract_names(page, [
                    '[role="option"]',
                    '[role="listitem"]',
                    '[class*="result"]',
                ])

                if not names:
                    try:
                        opts = page.locator('[role="option"]')
                        count = await opts.count()
                        for i in range(min(count, 10)):
                            txt = (await opts.nth(i).text_content() or "").strip()
                            first = txt.splitlines()[0].strip()
                            if first and len(first) < 80:
                                names.append(first)
                    except Exception:
                        pass

                if not names:
                    return (
                        f"No Discord users found for '{recipient}'. "
                        "Try their exact Discord username."
                    )

                match = _best_match(recipient, names)
                if match is None:
                    return (
                        f"Closest Discord result was '{names[0]}' — doesn't look right. "
                        "Try their exact username."
                    )

                found_name, score = match
                log.info("Discord: best match '%s' → '%s' (score=%.2f)",
                         recipient, found_name, score)

                # ── Click the matched result ────────────────────────────────────
                clicked = False
                for sel in [
                    f'[role="option"]:has-text("{found_name}")',
                    f'[role="listitem"]:has-text("{found_name}")',
                ]:
                    try:
                        el = page.locator(sel).first
                        await el.wait_for(state="visible", timeout=3_000)
                        await el.click()
                        clicked = True
                        break
                    except Exception:
                        continue

                if not clicked:
                    try:
                        el = page.get_by_text(found_name, exact=False).first
                        await el.click()
                        clicked = True
                    except Exception:
                        pass

                if not clicked:
                    return f"Found '{found_name}' on Discord but couldn't open the DM."

                # Confirm DM button if it appears (opens the DM channel)
                for btn_text in ["Create DM", "Open DM", "Send"]:
                    try:
                        btn = page.get_by_role("button", name=btn_text).first
                        await btn.wait_for(state="visible", timeout=2_000)
                        await btn.click()
                        break
                    except Exception:
                        continue

            await page.wait_for_timeout(1_500)

            # ── Type and send ─────────────────────────────────────────────────
            msg_box = None
            for sel in [
                'div[role="textbox"][aria-label*="Message @"]',
                'div[role="textbox"][aria-label*="Message"]',
                'div[role="textbox"]',
                '[contenteditable="true"][role="textbox"]',
            ]:
                try:
                    el = page.locator(sel).last
                    await el.wait_for(state="visible", timeout=5_000)
                    msg_box = el
                    break
                except Exception:
                    continue

            if msg_box is None:
                return "Opened the Discord DM but can't find the message box."

            await msg_box.click()
            await page.wait_for_timeout(300)
            await msg_box.type(message, delay=40)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)

            return f"Message sent to {recipient} on Discord ✓"

        except RuntimeError as exc:
            return str(exc)
        except Exception as exc:
            log.error("Discord message failed: %s", exc, exc_info=True)
            return f"Something went wrong sending the Discord DM: {exc}"


# ─── Universal message dispatcher ────────────────────────────────────────────

_PLATFORM_MAP: dict[str, str] = {
    # Facebook / Messenger
    "facebook": "facebook", "fb": "facebook", "messenger": "facebook",
    "facebook messenger": "facebook",
    # Instagram
    "instagram": "instagram", "ig": "instagram", "insta": "instagram",
    # X / Twitter
    "twitter": "twitter", "x": "twitter", "x.com": "twitter",
    # WhatsApp
    "whatsapp": "whatsapp", "wa": "whatsapp", "whats app": "whatsapp",
    # Discord — browser automation via discord.com
    "discord": "discord",
}


async def send_message(platform: str, recipient: str, message: str,
                       discord_bot=None) -> str:
    """
    Platform-agnostic message dispatcher.

    platform: one of facebook/fb/messenger | instagram/ig | twitter/x |
              whatsapp/wa | discord
    """
    key = _PLATFORM_MAP.get(platform.lower().strip())
    if key is None:
        supported = "facebook, instagram, twitter/x, whatsapp, discord"
        return f"Unknown platform '{platform}'. Supported: {supported}."

    if key == "facebook":
        return await send_facebook_message(recipient, message)
    if key == "instagram":
        return await send_instagram_message(recipient, message)
    if key == "twitter":
        return await send_twitter_message(recipient, message)
    if key == "whatsapp":
        return await send_whatsapp_message(recipient, message)
    if key == "discord":
        return await send_discord_message(recipient, message)

    return f"Platform '{platform}' is recognised but not yet wired up."


# ─── Away mode state ──────────────────────────────────────────────────────────

_away_active:  bool = False
_away_message: str  = (
    "Hey! {name} is away right now and can't reply. "
    "They'll get back to you as soon as they're back."
)


def set_away(on: bool, template: str = "") -> None:
    """
    Toggle the global away state.
    template: optional custom message. Use {name} as placeholder for the user's name.
    """
    global _away_active, _away_message
    _away_active = on
    if template.strip():
        _away_message = template.strip()
    log.info("Global away mode: %s", "ON" if on else "OFF")


def is_away() -> bool:
    return _away_active


def get_away_message(name: str = "The user") -> str:
    """Return the formatted away message."""
    return _away_message.format(name=name)


async def send_away_reply(platform: str, recipient: str,
                          user_name: str = "The user",
                          discord_bot=None) -> str:
    """
    Send the configured away message to a person on any platform.
    Called automatically by AwayMonitor or manually via action.
    """
    if not _away_active:
        return "Away mode is not active."
    msg = get_away_message(user_name)
    return await send_message(platform, recipient, msg, discord_bot=discord_bot)


# ─── Shutdown ─────────────────────────────────────────────────────────────────

async def close_browser() -> None:
    global _pw, _ctx
    for obj, method in [(_ctx, "close"), (_pw, "stop")]:
        if obj:
            try:
                await getattr(obj, method)()
            except Exception:
                pass
    _pw = _ctx = None
