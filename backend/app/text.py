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


_CUR = r"(?:₹|rs\.?|inr|rupees?|rupaye|rupay|रुपये|रुपए)"
_NUM = r"(\d{1,7}(?:,\d{2,3})*(?:\.\d+)?)"
_DEBITED = re.compile(r"deduct|debit|\bcut\b|\bkat\b|\bkata\b|kat (?:gay|gaye|gya)|money (?:gone|taken)|charged|कट|डेबिट", re.I)
_OTHER = re.compile(r"\b(?:another|different|some other|other (?:payment|txn|transaction|refund|settlement)|do+sr[ae]|dusr[aei]|koi aur|alag)\b"
                    r"|दूसरा|दूसरे|दूसरी|अलग|कोई और", re.I)


def extract_amount(text: str) -> Optional[int]:
    """₹1,250 · Rs 1250 · 1250 rupees · 1250 ka · 1.2k · 1250 रुपये. 12-digit UTRs and IDs are never amounts."""
    m = (re.search(rf"{_CUR}\s?{_NUM}\s?(k)?(?![\w])", text, re.I)
         or re.search(rf"(?<![\w.]){_NUM}\s?(k)?\s?(?:{_CUR}|ka|ke|ki|का|के|की)(?![\w])", text, re.I)
         or re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s?(k)(?![\w])", text, re.I))
    if not m and re.search(r"\brefund\b|रिफंड", text, re.I):
        m = re.search(r"(?<![\w.])(\d{2,7}(?:,\d{2,3})*)(?![\w])", text)     # "2000 refund not done"
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if m.lastindex and m.lastindex >= 2 and (m.group(2) or "").lower() == "k":
        value *= 1000
    return int(round(value))


def extract_entities(text: str) -> dict:
    """Pull IDs and the amount out of the RAW message. Call this before PII masking: the mask hides long numbers like a UTR."""
    ents: dict = {}
    if m := re.search(r"\bT\d{6,}\b", text, re.I):
        ents["txn_id"] = m.group(0).upper()
    if m := re.search(r"\bS\d{6,}\b", text, re.I):
        ents["settlement_id"] = m.group(0).upper()
    if m := re.search(r"\bR\d{4,}\b", text, re.I):
        ents["refund_id"] = m.group(0).upper()
    if m := re.search(r"(?<![\w.])\d{12}(?![\w])", text):
        ents["utr"] = m.group(0)
    amount = extract_amount(text)
    if amount is not None:
        ents["amount"] = amount
    if _OTHER.search(text):
        ents["other"] = True
    if _DEBITED.search(text):
        ents["debited"] = True      # the merchant says the customer's money was taken
    return ents


# ---------------------------------------------------------------- small talk
_GREET = {"hi", "hii", "hiii", "hello", "hey", "heyy", "hlo", "helo", "namaste", "namaskar", "hola", "good morning", "good afternoon",
          "good evening", "gm", "नमस्ते", "नमस्कार", "हेलो", "हैलो", "हाय"}
_THANKS = {"thanks", "thank you", "thankyou", "thx", "ty", "ok", "okay", "okk", "k", "kk", "thik hai", "theek hai", "thik", "theek", "shukriya",
           "dhanyavad", "dhanyawad", "got it", "fine", "cool", "great", "accha", "achha", "acha", "bye", "धन्यवाद", "शुक्रिया", "ठीक है", "ठीक",
           "अच्छा", "बढ़िया", "थैंक्यू", "थैंक यू"}
_FILLER = {"a", "lot", "so", "much", "very", "bhai", "ji", "sir", "madam", "didi", "boss", "team", "paymates", "there", "again", "bahut", "जी", "भाई", "बहुत"}


def small_talk_kind(text: str) -> Optional[str]:
    """'greet' | 'thanks' for a message that is only a greeting or an acknowledgement, else None."""
    t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF!?.,;:\"'()\[\]{}<>|/\\~*_-]+", " ", text.lower())
    if not t.strip():
        return "thanks" if re.search(r"[\U0001F44D\U0001F64F]", text) else None
    words = [w for w in t.split() if w not in _FILLER]
    phrase_ = " ".join(words)
    if phrase_ in _GREET or (words and all(w in _GREET for w in words)):
        return "greet"
    if phrase_ in _THANKS or (words and all(w in _THANKS or w in _GREET for w in words) and any(w in _THANKS for w in words)):
        return "thanks"
    return None


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
