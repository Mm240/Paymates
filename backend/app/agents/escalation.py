"""Escalation agent: hand a case to a human with a briefing, never a cold transfer.

The briefing is built from facts (what the merchant said, every check the
agent ran, what it proposes). A model, when available, adds a two-sentence
summary on top; the facts are never generated.
"""
from __future__ import annotations

import json

from ..config import POLICY, model_for
from ..llm import chat
from ..text import inr

LIMIT = POLICY["auto_money_limit"]


def describe_call(c: dict) -> str:
    n, a, r, g = c["name"], c["args"], c["result"], c["guard"]
    if r.get("blocked"):
        return f"Held: {n} {json.dumps(a)} — {g['reason']}"
    if n == "get_transaction" and "txn_id" in r:
        return f"Payment {r['txn_id']} · {r['amount_display']} · gateway {r['gateway_status']} · customer {'debited' if r['customer_debited'] else 'not debited'}"
    if n == "list_transactions":
        return f"Looked up {len(r.get('transactions', []))} recent payment(s)"
    if n == "recheck_payment_status":
        return f"Bank recheck of {r.get('txn_id')}: {r.get('bank_final_status')}"
    if n == "list_settlements":
        return "Settlements: " + "; ".join(f"{s['settlement_id']} {s['status']}" for s in r.get("settlements", [])[:3])
    if n in ("list_refunds",):
        return "Refunds: " + "; ".join(f"{x['refund_id']} {x['amount_display']} {x['status']}" for x in r.get("refunds", [])[:3])
    if n == "get_refund":
        return f"Refund {r.get('refund_id')} · {r.get('amount_display')} · {r.get('status')}"
    if n == "run_soundbox_diagnostics":
        return f"Soundbox {r.get('device_id')} diagnostics: {', '.join(r.get('issues') or ['no issues'])}"
    if n == "reconnect_device":
        return f"Reconnected Soundbox {r.get('device_id')}" + (" on the backup network" if r.get("switched_to_backup_network") else "")
    if n == "resend_missed_announcements":
        return f"Re-announced {r.get('replayed')} missed payment(s)"
    if n == "set_announcement_language":
        return f"Soundbox language set to {r.get('language')}"
    if n == "play_test_announcement":
        return "Played a test announcement" + ("" if r.get("audible") else " (volume 0, not audible)")
    if n == "get_kyc_status":
        return f"KYC {r.get('kyc_status')}" + (f", missing {', '.join(r['missing_documents'])}" if r.get("missing_documents") else "")
    if n == "search_help_center":
        top = (r.get("results") or [{}])[0]
        return f"Help center: {top.get('id', 'no match')} {top.get('title', '')}".strip()
    if n == "escalate_to_human":
        return f"Agent asked for a human: {a.get('reason')}"
    return f"{n}: done"


def risk_flags(tri: dict, act: dict, store, merchant_id: str) -> list[str]:
    flags = []
    p = act.get("proposed_action")
    if p:
        amt = None
        if p["tool"] == "reinitiate_refund":
            amt = store.refund(p["args"]["refund_id"])["amount"]
        elif p["tool"] == "mark_payment_recovered":
            amt = store.transaction(p["args"]["txn_id"])["amount"]
        if amt and amt > LIMIT:
            flags.append(f"Moves {inr(amt)}, above the {inr(LIMIT)} autonomous limit")
    if (act.get("escalation_reason") or "").startswith("Council held it"):
        flags.append(act["escalation_reason"])
    if tri["confidence"] < POLICY["confidence_threshold"]:
        flags.append(f"Low triage confidence ({int(tri['confidence'] * 100)}%)")
    if tri["intent"] == "HUMAN_REQUEST":
        flags.append("Merchant asked for a person")
    past = store.past_tickets(merchant_id, 5)
    if len([t for t in past if t["intent"] == tri["intent"]]) >= 2:
        flags.append("Repeat issue for this merchant")
    return flags


async def build_briefing(store, merchant: dict, ticket_id: str, text: str, tri: dict, act: dict, calls: list[dict]) -> tuple[dict, str]:
    checks = [describe_call(c) for c in calls]
    p = act.get("proposed_action")
    headline = {
        "REFUND_NEEDS_APPROVAL": f"Re-send failed refund {act['facts'].get('refund_id')} ({act['facts'].get('amount')}) — needs approval",
        "MONEY_NEEDS_APPROVAL": f"Money movement of {act['facts'].get('amount')} needs approval",
        "COUNCIL_HOLD": f"Model Council held a {act['facts'].get('amount')} money movement",
        "HUMAN_HANDOFF": "Merchant asked to speak to a person",
    }.get(act["outcome"], "Case needs a specialist's judgement")
    briefing = {
        "headline": headline,
        "merchant": {"id": merchant["id"], "name": merchant["name"], "owner": merchant["owner"], "city": merchant["city"],
                     "plan": merchant["plan"], "language": tri["language"]},
        "said": text,
        "intent": tri["intent"],
        "confidence": tri["confidence"],
        "checks": checks,
        "proposed": p,
        "risk": risk_flags(tri, act, store, merchant["id"]),
        "reason": act.get("escalation_reason") or act.get("summary"),
        "summary": f"{merchant['owner']} ({merchant['name']}, {merchant['city']}) wrote about {tri['intent'].replace('_', ' ').lower()}. "
                   f"{act.get('summary', '')}.",
    }
    spec = model_for("escalation")
    if not spec:
        return briefing, "template"
    try:
        out = await chat(spec, [
            {"role": "system", "content": "You write handover notes for human payment-support specialists. Two sentences, plain English, "
                                          "facts only from the input, end with what decision is needed."},
            {"role": "user", "content": json.dumps({k: briefing[k] for k in ("merchant", "said", "intent", "checks", "proposed", "risk", "reason")},
                                                   ensure_ascii=False, default=str)},
        ], max_tokens=180)
        if out["text"].strip():
            briefing["summary"] = out["text"].strip()
        return briefing, spec.label
    except Exception:
        return briefing, f"template (fallback from {spec.label})"
