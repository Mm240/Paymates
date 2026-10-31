"""Language detection, tokenising, keyword matching and Indian-style formatting."""
from __future__ import annotations

import re
import time
from typing import Optional

from .config import LEXICON, TEMPLATES

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
TOKEN = re.compile(r"[a-z0-9]+|[ऀ-ॿ]+")


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def detect_language(text: str) -> str:
    """'hi' for Devanagari, 'hinglish' for Romanised Hindi, else 'en'."""
    if DEVANAGARI.search(text):
        return "hi"
    toks = tokens(text)
    markers = set(LEXICON["hinglish_markers"])
    strong = set(LEXICON["strong_hinglish"])
    hits = [t for t in toks if t in markers]
    if len(hits) >= 2 or any(t in strong for t in hits):
        return "hinglish"
    return "en"


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    """Match keywords: phrases/Devanagari by substring, short words exactly, long words by prefix."""
    low = text.lower()
    toks = tokens(text)
    found = []
    for kw in keywords:
        if " " in kw or not kw.isascii():
            if kw in low:
                found.append(kw)
        elif len(kw) < 4:
            if kw in toks:
                found.append(kw)
        elif any(t.startswith(kw) for t in toks):
            found.append(kw)
    return found


def extract_entities(text: str) -> dict:
    ents: dict = {}
    if m := re.search(r"\bT\d{6,}\b", text, re.I):
        ents["txn_id"] = m.group(0).upper()
    if m := re.search(r"\bS\d{6,}\b", text, re.I):
        ents["settlement_id"] = m.group(0).upper()
    if m := re.search(r"\bR\d{4,}\b", text, re.I):
        ents["refund_id"] = m.group(0).upper()
    if m := re.search(r"(?:₹|rs\.?|inr|rupees?)\s?([\d,]+)", text, re.I) or \
            re.search(r"\b([\d,]{2,})\s?(?:rs\.?|rupees?|rupay|rupaye|रुपये)\b", text, re.I):
        try:
            ents["amount"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    elif re.search(r"\brefund\b|रिफंड", text, re.I):
        if m := re.search(r"\b([\d,]{2,})\b", text):
            try:
                ents["amount"] = int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return ents


def inr(amount: Optional[float]) -> str:
    """₹ with Indian digit grouping: 124500 -> ₹1,24,500."""
    if amount is None:
        return "₹0"
    n = int(round(amount))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-" if n < 0 else "") + "₹" + s


def phrase(key: str, lang: str, **kw) -> str:
    return TEMPLATES["phrases"][key][lang].format(**kw)


def ago(ts_ms: Optional[int], lang: str) -> str:
    if not ts_ms:
        return ""
    mins = max(1, int((time.time() * 1000 - ts_ms) / 60000))
    if mins < 60:
        return phrase("minutes_ago", lang, n=mins)
    return phrase("hours_ago", lang, n=round(mins / 60))


class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(code: str, lang: str, facts: dict) -> str:
    tpl = TEMPLATES["replies"].get(code) or TEMPLATES["replies"]["LOW_CONFIDENCE"]
    text = tpl.get(lang) or tpl["en"]
    return text.format_map(SafeDict(facts))
