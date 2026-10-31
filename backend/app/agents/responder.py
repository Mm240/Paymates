"""Responder agent: tell the merchant what happened, in their language.

The template reply is always built first from verified facts. A model may
then rewrite it to sound more natural, but the rewrite is only used if
every amount and reference ID from the facts survives unchanged (a simple
grounding check). Otherwise the template is sent.
"""
from __future__ import annotations

import re

from ..config import model_for
from ..llm import chat
from ..text import render

LANG_NAMES = {"en": "English", "hi": "Hindi in Devanagari script", "hinglish": "Hinglish (Hindi written in Latin script, casual and warm)"}
CRITICAL = re.compile(r"₹[\d,]+|\b[A-Z]{1,4}-?\d{4,}\b|\b[A-Z]{4}\d{8,}\b")


def grounded(draft: str, candidate: str) -> bool:
    return all(token in candidate for token in set(CRITICAL.findall(draft)))


# Fixed wording: asking for an ID, small talk and clarifying questions are sent exactly as written.
STATIC_REPLIES = {"FAQ_ANSWER", "PAYMENT_NOT_FOUND", "REFUND_NOT_FOUND", "SETTLEMENT_NOT_FOUND", "ID_NOT_FOUND", "SMALL_TALK_GREET",
                  "SMALL_TALK_THANKS", "CLARIFY"}


async def respond(outcome: str, facts: dict, lang: str, text: str) -> tuple[str, str, bool]:
    """Returns (reply, model_label, grounded_ok)."""
    draft = render(outcome, lang, facts)
    spec = model_for("responder")
    if not spec or outcome in STATIC_REPLIES:
        return draft, "templates", True
    try:
        out = await chat(spec, [
            {"role": "system", "content": (
                "You are PayMates, a friendly support teammate for small Indian merchants. Rewrite the DRAFT reply so it sounds "
                f"natural and warm in {LANG_NAMES.get(lang, 'English')}. Keep it under 70 words. Keep every amount, ID and reference "
                "EXACTLY as written. Do not add facts, promises or links. Reply with only the message text.")},
            {"role": "user", "content": f"Merchant wrote: {text}\n\nDRAFT: {draft}"},
        ], temperature=0.4, max_tokens=260, agent="responder")
        candidate = out["text"].strip().strip('"')
        if candidate and grounded(draft, candidate):
            return candidate, spec.label, True
        return draft, f"templates (model reply failed grounding check: {spec.label})", False
    except Exception:
        return draft, f"templates (fallback from {spec.label})", True
