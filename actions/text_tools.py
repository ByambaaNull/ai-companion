"""
text_tools.py — writing helpers powered by the user's own LLM keys (no Grammarly /
DeepL / paid summarizer subscription):

    fix_grammar, rewrite, translate, summarize (raw text, a web page, or a YouTube
    link via its captions).

All calls are synchronous (meant to run on a background thread) and fall over
across whatever providers are configured, like main.stream_llm does.
"""

from __future__ import annotations

import html
import logging
import re

import httpx

import config

log = logging.getLogger(__name__)


def _chat(system: str, user: str, max_tokens: int = 700) -> dict:
    """One-shot, non-streaming chat across configured providers. {ok, text/error}."""
    providers = config.LLM_PROVIDERS
    if not providers:
        return {"ok": False, "error": "No API key set — add one in Settings → API keys."}
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    last = ""
    for prov in providers:
        try:
            resp = httpx.post(
                prov["url"],
                json={"model": prov["model"], "messages": messages,
                      "temperature": 0.3, "max_tokens": max_tokens, "stream": False},
                headers={"Authorization": f"Bearer {prov['key']}",
                         "Content-Type": "application/json"},
                timeout=45.0,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            if text:
                return {"ok": True, "text": text}
        except Exception as exc:
            last = str(exc)
            log.warning("text_tools via %s failed: %s — trying next", prov["name"], exc)
            continue
    return {"ok": False, "error": last or "All providers failed."}


def fix_grammar(text: str) -> dict:
    return _chat(
        "You are a careful copy editor. Fix spelling, grammar and punctuation. "
        "Preserve the author's meaning, tone and formatting. Return ONLY the "
        "corrected text, nothing else.", text)


def rewrite(text: str, style: str = "clearer and more concise") -> dict:
    return _chat(
        f"Rewrite the user's text to be {style}. Keep the original meaning. "
        f"Return ONLY the rewritten text.", text)


def translate(text: str, target_lang: str = "English") -> dict:
    return _chat(
        f"Translate the user's text into {target_lang}. Keep names and formatting. "
        f"Return ONLY the translation.", text)


def summarize(text: str) -> dict:
    if len(text) > 12000:
        text = text[:12000]
    return _chat(
        "Summarize the user's text into a few clear bullet points, then one "
        "1-sentence takeaway. Be faithful and concise.", text, max_tokens=500)


# ─── Web page / YouTube summarization ────────────────────────────────────────

def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</(p|div|h[1-6]|li)>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def summarize_url(url: str) -> dict:
    """Fetch a web page and summarize its readable text."""
    if not re.match(r"^https?://", url, re.I):
        return {"ok": False, "error": "Enter a valid http(s) link."}
    try:
        r = httpx.get(url, timeout=30.0, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = _strip_html(r.text)
        if len(text) < 80:
            return {"ok": False, "error": "Couldn't extract readable text from that page."}
        return summarize(text)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _vtt_to_text(vtt: str) -> str:
    out: list[str] = []
    for line in vtt.splitlines():
        line = line.strip()
        if (not line or line.upper().startswith("WEBVTT") or "-->" in line
                or line.isdigit()):
            continue
        line = re.sub(r"<[^>]+>", "", line)  # inline timing tags
        if line and (not out or out[-1] != line):
            out.append(line)
    return " ".join(out)


def summarize_youtube(url: str) -> dict:
    """Summarize a YouTube video from its captions (auto or uploaded). No download."""
    try:
        import yt_dlp
    except Exception:
        return {"ok": False, "error": "yt-dlp not installed."}
    try:
        opts = {"skip_download": True, "quiet": True, "no_warnings": True,
                "writesubtitles": True, "writeautomaticsub": True,
                "subtitleslangs": ["en", "en-US", "en-orig"]}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        title = info.get("title", "")
        subs = {**(info.get("subtitles") or {}), **(info.get("automatic_captions") or {})}
        track = None
        for lang in ("en", "en-US", "en-orig"):
            if lang in subs and subs[lang]:
                track = subs[lang]
                break
        if not track:
            return {"ok": False, "error": "No captions available for this video."}
        vurl = None
        for fmt in track:
            if fmt.get("ext") in ("vtt", "srv1", "json3", "ttml"):
                vurl = fmt.get("url")
                if fmt.get("ext") == "vtt":
                    break
        if not vurl:
            vurl = track[-1].get("url")
        cap = httpx.get(vurl, timeout=30.0).text
        text = _vtt_to_text(cap)
        if len(text) < 80:
            return {"ok": False, "error": "Captions were empty."}
        res = summarize(f"Video title: {title}\n\nTranscript:\n{text}")
        if res.get("ok"):
            res["title"] = title
        return res
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
