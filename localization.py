"""
localization.py — Informal text normalization for the AI companion.

Handles Mongolian-style shorthand typed in Latin characters (common when
Mongolian speakers text informally in English keyboards).

The expand() function should be applied to raw STT/chat input BEFORE it
reaches the LLM so the assistant understands the intent without needing explicit
translation.

Examples of Mongolian shorthand (Latin characters):
  bnu   → bainu   (hello / how are you — from "Байна уу?")
  bnu?  → bainu   (same with question mark)
  yu bna → yuu baina  (what's up / what is there)
  yahan bna → yahan baina  (how are you / how is it)
  bi bna → bi baina  (I am / I'm here)
  hm    → hmm     (thinking sound, same)
  uu    → uu      (yes / OK in Mongolian — leave as is, already understood)
  mgii  → mgui    (no / not — from "байхгүй")
  tgvl  → tegvel  (then / so — from "тэгвэл")
  tgj   → tegj    (doing that / like that)
  tgn   → tegnee  (right then / OK then)
  yum bna → yum baina  (what is it / what's there)
  yum gd → yum gej  (saying what / what do you mean)
  za    → za      (OK / alright — Mongolian affirmative)
  zaa   → zaa     (alright then)
  ee    → ee      (yes / uh-huh — Mongolian affirmative)
  nz    → naiz    (friend)
  hgm   → hugum   (guys / people)
  odoo  → odoo    (now)
  odo   → odoo    (now — shorthand)
  yav   → yav     (go)
  ir    → ir      (come)
  hn    → hun     (person / man)
  hd    → hund    (to someone)
"""

from __future__ import annotations

import re

# ─── Mongolian shorthand → expansion map ──────────────────────────────────────
# Keys: lowercase, no punctuation
# Values: expanded form (kept in informal latin transcription that the LLM
#         can contextualise — no Cyrillic needed)

_SHORTHANDS: dict[str, str] = {
    # Greetings / status
    "bnu":        "bainu (hello / how are you)",
    "bnu?":       "bainu (hello / how are you)",
    "sain bnu":   "sain bainu (are you well / hello)",
    "sain uu":    "sain uu (hello / are you well)",
    "yu bna":     "yuu baina (what's up / what's going on)",
    "yahan bna":  "yahan baina (how are you / how is it)",
    "bi bna":     "bi baina (I am here / I am doing this)",
    "yum bna":    "yum baina (what is it / what's there)",
    "yum gd":     "yum gej (saying what / meaning what)",
    "yum gdg":    "yum gedeg (what does it mean)",
    # Affirmatives / negatives
    "za":    "za (OK / alright)",
    "zaa":   "zaa (alright then / sure)",
    "ee":    "ee (yes / uh-huh)",
    "mgii":  "mgui (no / doesn't exist / not available)",
    "mgui":  "mgui (no / doesn't exist)",
    "ugu":   "ugui (no / none)",
    # Transitions
    "tgvl":  "tegvel (then / so / in that case)",
    "tgj":   "tegj (doing that / like that)",
    "tgn":   "tegnee (OK then / right then)",
    "tgd":   "tegeed (and then / after that)",
    "tdg":   "tedeg (those / those ones)",
    # People / social
    "nz":    "naiz (friend)",
    "nzuu":  "naizuu (my friend / hey friend)",
    "hgm":   "hugumees (from people / everyone)",
    "hn":    "hun (person)",
    "hd":    "hund (to someone)",
    # Time / action
    "odoo":  "odoo (now)",
    "odo":   "odoo (now)",
    "yav":   "yav (go)",
    "ir":    "ir (come)",
    "awna":  "avna (will take / will get)",
    "ogno":  "ugno (will give)",
    # Misc
    "haha":  "haha",  # keep as is
    "lol":   "lol",   # keep as is
    "bb":    "bye bye",
    "ttyl":  "talk to you later",
}

# Build a regex that matches whole words (case-insensitive)
# Sort by length descending so longer phrases match first
_sorted_keys = sorted(_SHORTHANDS.keys(), key=len, reverse=True)
_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _sorted_keys) + r")\b",
    re.IGNORECASE,
)


def expand(text: str) -> str:
    """
    Expand Mongolian shorthand in *text*.

    Returns the original text with shorthands replaced by their expansions.
    Unrecognised words are left unchanged.

    Example:
        >>> expand("bnu gaki")
        'bainu (hello / how are you) gaki'
    """
    def _replace(m: re.Match) -> str:
        key = m.group(0).lower()
        return _SHORTHANDS.get(key, m.group(0))

    return _PATTERN.sub(_replace, text)


def has_mongolian_slang(text: str) -> bool:
    """Return True if the text contains any known Mongolian shorthand."""
    return bool(_PATTERN.search(text))
