"""Triage agent: what is the merchant asking, how sure are we, how urgent is it?

LLM path: a fast model classifies intent and extracts IDs as JSON.
Rules path: weighted keyword matching in English, Hinglish and Hindi.
The language is always detected by rules (script + Hinglish markers),
because that decision must never be wrong.
"""
from __future__ import annotations

import json

from ..config import LEXICON, model_for
from ..llm import chat, parse_json
from ..rag import index
from ..text import detect_language, extract_entities, keyword_hits

INTENTS = {
    "FAILED_PAYMENT": "A customer's payment failed, is stuck, or money was deducted without success.",
    "SETTLEMENT_DELAY": "The merchant's payout/settlement to their bank is late, missing or on hold.",
    "REFUND_ISSUE": "A refund the merchant sent to a customer has not arrived, failed, or needs status.",
    "SOUNDBOX": "The Soundbox device is silent, not announcing, or needs settings changed.",
    "KYC": "KYC status, verification or which documents are needed.",
    "HUMAN_REQUEST": "The merchant explicitly asks for a human, a call, or a manager.",
    "GENERAL": "A how-to or policy question answerable from the help center.",
    "UNKNOWN": "Unclear or out of scope.",
}
MONEY = {"FAILED_PAYMENT", "SETTLEMENT_DELAY", "REFUND_ISSUE"}


def rules_triage(text: str) -> dict:
    weights = LEXICON.get("intent_weights", {})
    scores, hits = {}, {}
    for intent, kws in LEXICON["intents"].items():
        h = keyword_hits(text, kws)
        hits[intent] = h
        scores[intent] = len(h) * weights.get(intent, 1.0)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (top, s1), (_, s2) = ranked[0], ranked[1]
    if top != "GENERAL":
        # GENERAL words ("what", "how", "when") frame a question; only another specific issue competes.
        s2 = max((v for k, v in scores.items() if k not in (top, "GENERAL")), default=0)
    entities = extract_entities(text)

    if s1 == 0:
        docs = index.search(text, 1)
        if docs and docs[0]["score"] >= 0.22:
            top, conf, why = "GENERAL", 0.62, f"No intent keywords; help-center match {docs[0]['id']} ({docs[0]['score']})"
        else:
            top, conf, why = "UNKNOWN", 0.3, "No intent keywords and no strong help-center match"
    else:
        conf = min(0.97, 0.5 + 0.17 * s1)
        if s2 == s1:
            conf -= 0.25
        elif s2 > 0:
            conf -= 0.08
        why = "Matched: " + ", ".join(hits[top][:4])

    # An explicit record ID is strong evidence of what the case is about.
    if entities.get("refund_id"):
        top, conf = "REFUND_ISSUE", max(conf, 0.9)
    elif entities.get("settlement_id"):
        top, conf = "SETTLEMENT_DELAY", max(conf, 0.9)
    elif entities.get("txn_id") and top in ("UNKNOWN", "GENERAL"):
        top, conf = "FAILED_PAYMENT", max(conf, 0.75)

    return {
        "intent": top,
        "confidence": round(max(0.05, conf), 2),
        "entities": entities,
        "urgency": "high" if top in MONEY else ("medium" if top in ("SOUNDBOX", "KYC", "HUMAN_REQUEST") else "low"),
        "language": detect_language(text),
        "reasoning": why,
    }


SYSTEM = (
    "You are the triage agent of PayMates, an AI support teammate for Indian payment merchants. "
    "Classify the merchant's message. Messages may be English, Hindi (Devanagari) or Hinglish (Hindi in Latin script). "
    "Reply with ONLY a JSON object: {\"intent\": one of " + json.dumps(list(INTENTS)) + ", "
    "\"confidence\": number 0-1, \"entities\": {\"txn_id\"?, \"settlement_id\"?, \"refund_id\"?, \"amount\"? (integer rupees)}, "
    "\"urgency\": \"high\"|\"medium\"|\"low\", \"reasoning\": short phrase}.\n"
    "Intent meanings:\n" + "\n".join(f"- {k}: {v}" for k, v in INTENTS.items())
)


async def triage(text: str) -> tuple[dict, str]:
    """Returns (result, model_label)."""
    rules = rules_triage(text)
    spec = model_for("triage")
    if not spec:
        return rules, "rules-engine"
    try:
        out = await chat(spec, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
                         json_mode=True, max_tokens=300)
        data = parse_json(out["text"])
        intent = str(data.get("intent", "")).upper()
        if intent not in INTENTS:
            raise ValueError(f"invalid intent {intent!r}")
        ents = {**rules["entities"], **{
            k: v for k, v in (data.get("entities") or {}).items()
            if v not in (None, "", 0) and not (k == "amount" and rules["entities"].get("amount") is not None)
        }}
        if "amount" in ents:
            try:
                ents["amount"] = int(str(ents["amount"]).replace(",", "").replace("₹", ""))
            except ValueError:
                ents.pop("amount")
        return {
            "intent": intent,
            "confidence": round(float(data.get("confidence", 0.7)), 2),
            "entities": ents,
            "urgency": data.get("urgency") or rules["urgency"],
            "language": rules["language"],
            "reasoning": str(data.get("reasoning", ""))[:160],
        }, spec.label
    except Exception as exc:  # fall back, never fail the ticket
        rules["reasoning"] += f" (LLM unavailable: {str(exc)[:80]})"
        return rules, f"rules-engine (fallback from {spec.label})"
