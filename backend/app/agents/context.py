"""Context agent: gather everything relevant BEFORE anyone replies.

Retrieval is plain code on purpose: SQL lookups for the merchant's live
records plus vector search over the help center. It is fast, exact and
auditable, and it means the merchant never has to "explain the issue again".
"""
from __future__ import annotations

from ..guardrails import mask_pii
from ..rag import index
from ..store import Store
from ..text import inr

HINTS = {
    "FAILED_PAYMENT": "payment failed money deducted bank",
    "SETTLEMENT_DELAY": "settlement timeline hold bank",
    "REFUND_ISSUE": "refund failed timeline",
    "SOUNDBOX": "soundbox not announcing volume network",
    "KYC": "kyc documents",
    "HUMAN_REQUEST": "human specialist",
}


def gather(store: Store, merchant_id: str, triage: dict, text: str) -> dict:
    m = store.merchant(merchant_id)
    intent = triage["intent"]
    records: dict = {}
    highlights: list[str] = []

    if intent == "FAILED_PAYMENT":
        failed = store.transactions(merchant_id, status="FAILED", limit=5)
        records["failed_payments"] = [
            {"txn_id": t["id"], "amount": t["amount"], "debited": t["bank_debited"], "payer": mask_pii(t["payer"])} for t in failed]
        highlights.append(f"{len(failed)} failed payment(s) in recent history")
        for t in failed[:3]:
            highlights.append(f"{t['id']} · {inr(t['amount'])} · customer {'debited' if t['bank_debited'] else 'not debited'}")
    elif intent == "SETTLEMENT_DELAY":
        ss = store.settlements(merchant_id, limit=4)
        records["settlements"] = [{"id": s["id"], "amount": s["amount"], "status": s["status"], "hold": s["hold_reason"]} for s in ss]
        for s in ss[:3]:
            highlights.append(f"{s['id']} · {inr(s['amount'])} · {s['status']}" + (f" ({s['hold_reason']})" if s["hold_reason"] else ""))
    elif intent == "REFUND_ISSUE":
        rs = store.refunds(merchant_id, limit=4)
        records["refunds"] = [{"id": r["id"], "amount": r["amount"], "status": r["status"]} for r in rs]
        for r in rs[:3]:
            highlights.append(f"{r['id']} · {inr(r['amount'])} · {r['status']}")
    elif intent == "SOUNDBOX":
        sb = m["soundbox"]
        records["soundbox"] = sb
        highlights.append(f"{sb['id']} ({sb['model']}) · {'online' if sb['online'] else 'offline'}")
    elif intent == "KYC":
        records["kyc"] = {"status": m["kyc_status"], "missing": m["kyc_missing"]}
        highlights.append(f"KYC {m['kyc_status']}" + (f" · missing {len(m['kyc_missing'])} doc(s)" if m["kyc_missing"] else ""))

    past = store.past_tickets(merchant_id, 3)
    if past:
        highlights.append(f"{len(past)} past ticket(s); last: {past[0]['summary'][:70]}")
    convo = store.conversation(merchant_id, 6)
    docs = index.search(text + " " + HINTS.get(intent, ""), 3)

    return {
        "merchant": {"id": m["id"], "name": m["name"], "owner": m["owner"], "city": m["city"], "plan": m["plan"],
                     "lang": m["lang"], "kyc": m["kyc_status"], "bank": m["bank"]},
        "records": records,
        "past_tickets": past,
        "conversation": convo,
        "kb": [{"id": d["id"], "title": d["title"], "score": d["score"], "snippet": d["snippet"], "short": d["short"]} for d in docs],
        "highlights": highlights,
    }
