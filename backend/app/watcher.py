"""Watcher: the proactive check-in. Finds problems before the merchant complains.

    scan(merchant)  -> findings from the records
    checkin(...)    -> each finding runs through the normal orchestrator
                       (same agents, tools, guardrails, council), as a
                       ticket with source "proactive"

What it looks for:
  * failed payments where the customer was debited and the bank was never rechecked
  * settlements ON_HOLD
  * FAILED refunds
  * Soundbox issues from diagnostics

Idempotent: a record is skipped if a human already has it (open escalation),
if PayMates already worked on it in any earlier ticket (it appears in the
audit log), or if a proactive ticket already exists for it.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from .agents.orchestrator import handle_message
from .store import Store, now_ms
from .text import inr, render
from .tools import run_soundbox_diagnostics

Emit = Callable[[dict], Awaitable[None]]
DAY_MS = 24 * 60 * 60 * 1000


def _handled(store: Store, mid: str, subject: str) -> bool:
    if store.one("SELECT 1 FROM tickets WHERE merchant_id = ? AND source = 'proactive' AND subject = ?", (mid, subject)):
        return True
    if store.one("SELECT 1 FROM escalations WHERE merchant_id = ? AND status = 'OPEN' AND proposed_action LIKE ?", (mid, f"%{subject}%")):
        return True
    # PayMates already acted on or examined this record in an earlier ticket.
    return bool(store.one(
        "SELECT 1 FROM audit a JOIN tickets t ON t.id = a.ticket_id WHERE t.merchant_id = ? "
        "AND a.actor NOT IN ('merchant', 'watcher') AND a.detail LIKE ?", (mid, f"%{subject}%")))


def scan(store: Store, mid: str) -> list[dict]:
    found: list[dict] = []
    # Smallest first: low-risk fixes land before anything that may need a human.
    for t in store.all("SELECT * FROM transactions WHERE merchant_id = ? AND gateway_status = 'FAILED' AND bank_debited = 1 "
                       "AND bank_checked = 0 AND recovered = 0 ORDER BY amount ASC", (mid,)):
        found.append({"kind": "payment", "subject": t["id"], "intent": "FAILED_PAYMENT", "entities": {"txn_id": t["id"]},
                      "title": f"Found payment {t['id']} ({inr(t['amount'])}) failed but the customer was charged",
                      "intro": "PROACTIVE_PAYMENT", "facts": {"txn_id": t["id"], "amount": inr(t["amount"])}})
    for x in store.all("SELECT * FROM settlements WHERE merchant_id = ? AND status = 'ON_HOLD' ORDER BY expected_at", (mid,)):
        found.append({"kind": "settlement", "subject": x["id"], "intent": "SETTLEMENT_DELAY", "entities": {"settlement_id": x["id"]},
                      "title": f"Found settlement {x['id']} ({inr(x['amount'])}) on hold: {x['hold_reason']}",
                      "intro": "PROACTIVE_SETTLEMENT", "facts": {"settlement_id": x["id"], "amount": inr(x["amount"])}})
    for r in store.all("SELECT * FROM refunds WHERE merchant_id = ? AND status = 'FAILED' ORDER BY initiated_at", (mid,)):
        found.append({"kind": "refund", "subject": r["id"], "intent": "REFUND_ISSUE", "entities": {"refund_id": r["id"]},
                      "title": f"Found refund {r['id']} ({inr(r['amount'])}) bounced back",
                      "intro": "PROACTIVE_REFUND", "facts": {"refund_id": r["id"], "amount": inr(r["amount"])}})
    d = run_soundbox_diagnostics(store, mid, {})
    if d["issues"]:
        found.append({"kind": "soundbox", "subject": d["device_id"], "intent": "SOUNDBOX", "entities": {},
                      "title": f"Found Soundbox {d['device_id']} issue: {', '.join(d['issues'])}",
                      "intro": "PROACTIVE_SOUNDBOX", "facts": {"device_id": d["device_id"]}})
    out = []
    for f in found:
        if f["kind"] == "soundbox":
            # Device state can stay "wrong" after a fix that needs the merchant's hands; don't nag twice a day.
            recent = store.one("SELECT 1 FROM tickets WHERE merchant_id = ? AND intent = 'SOUNDBOX' AND source != 'history' "
                               "AND created_at >= ?", (mid, now_ms() - DAY_MS))
            if recent:
                continue
        elif _handled(store, mid, f["subject"]):
            continue
        f["text"] = f"[PayMates check-in] {f['title']}"
        out.append(f)
    return out


async def checkin(store: Store, mid: str, emit: Emit, pacing: Optional[float] = None) -> dict:
    merchant = store.merchant(mid)
    if merchant is None:
        raise ValueError(f"unknown merchant {mid}")
    findings = scan(store, mid)
    await emit({"type": "checkin_start", "merchant_id": mid,
                "findings": [{"kind": f["kind"], "subject": f["subject"], "title": f["title"]} for f in findings]})
    tickets = []
    for f in findings:
        tickets.append(await handle_message(store, mid, f["text"], emit, source="proactive", pacing=pacing, watch=f))
    message = None
    if not findings:
        await emit({"type": "step", "agent": "watcher", "status": "done", "title": "All clear: nothing needs fixing right now",
                    "model": "watcher: record scan", "ms": 0, "data": {}})
        message = render("CHECKIN_ALL_CLEAR", merchant["lang"], {"owner_first": merchant["owner"].split()[0]})
        store.add_message(mid, "assistant", message, None)
    store.audit(None, "watcher", "checkin", {"merchant": mid, "found": len(findings)})
    result = {"merchant_id": mid, "found": len(findings), "tickets": tickets, "message": message}
    await emit({"type": "checkin_done", **result})
    return result
