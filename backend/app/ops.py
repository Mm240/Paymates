"""Operations: outcome metrics, the human console, and the ticket simulator."""
from __future__ import annotations

import random
import statistics
from typing import Awaitable, Callable, Optional

from .config import LEXICON, POLICY, TEMPLATES
from .store import Store, now_ms
from .text import inr, render
from .tools import ToolRunner

Emit = Callable[[dict], Awaitable[None]]


# ====================================================================== metrics
def metrics(store: Store) -> dict:
    ts = list(reversed(store.tickets(limit=100000)))      # oldest first
    total = len(ts)
    count = lambda s: sum(1 for t in ts if t["status"] == s)  # noqa: E731
    ai = count("RESOLVED_AI")
    durations = [t["duration_ms"] for t in ts if t["status"] == "RESOLVED_AI" and t["duration_ms"] is not None]
    by_intent: dict[str, dict] = {}
    for t in ts:
        b = by_intent.setdefault(t["intent"] or "UNKNOWN", {"total": 0, "ai": 0})
        b["total"] += 1
        b["ai"] += t["status"] == "RESOLVED_AI"
    manual = POLICY["manual_minutes_per_ticket"]
    return {
        "total": total,
        "resolved_ai": ai,
        "escalated_open": count("ESCALATED"),
        "resolved_human": count("RESOLVED_HUMAN"),
        "human_followup": count("HUMAN_FOLLOWUP"),
        "needs_info": count("NEEDS_INFO"),
        "ai_resolution_rate": round(ai / total, 3) if total else 0,
        "median_ai_ms": int(statistics.median(durations)) if durations else None,
        "manual_minutes_baseline": manual,
        "recovered_value": sum(t["recovered_amount"] or 0 for t in ts),
        "hours_saved": round(ai * manual / 60, 1),
        "proactive_fixed": sum(1 for t in ts if t["source"] == "proactive" and t["status"] == "RESOLVED_AI"),
        "by_intent": by_intent,
        "series": [{"i": i + 1, "status": t["status"], "intent": t["intent"], "source": t["source"]} for i, t in enumerate(ts)],
        "auto_money_limit": POLICY["auto_money_limit"],
    }


# ====================================================================== human console
async def _noop(_: dict) -> None:
    return None


async def decide(store: Store, esc_id: str, approve: bool, specialist: str, note: str = "") -> dict:
    esc = store.escalation(esc_id)
    if esc is None:
        raise KeyError(esc_id)
    if esc["status"] != "OPEN":
        raise ValueError(f"{esc_id} is already {esc['status']}")
    ticket = store.ticket(esc["ticket_id"])
    lang = ticket["lang"] or "en"
    mid = esc["merchant_id"]
    proposed = esc["proposed_action"]
    recovered = 0
    if approve:
        ref = esc_id
        tool = proposed["tool"] if proposed else "none"
        amount = None
        if proposed:
            runner = ToolRunner(store, mid, ticket["id"], _noop, actor=f"human:{specialist}")
            res = await runner.run(proposed["tool"], proposed["args"], approved_by=specialist)
            if res.get("blocked") or res.get("error"):
                raise ValueError(f"Approved action could not run: {res.get('reason') or res.get('error')}")
            ref = res.get("new_reference") or res.get("new_utr") or res.get("txn_id") or esc_id
            recovered = runner.recovered
            if proposed["tool"] == "reinitiate_refund":
                amount = store.refund(proposed["args"]["refund_id"])["amount"]
            elif proposed["tool"] == "mark_payment_recovered":
                amount = store.transaction(proposed["args"]["txn_id"])["amount"]
            elif proposed["tool"] == "retry_settlement":
                amount = store.settlement(proposed["args"]["settlement_id"])["amount"]
        done = TEMPLATES["action_done"].get(tool, TEMPLATES["action_done"]["none"])[lang].format(amount=inr(amount))
        if note:
            done = f"{done} {note}"
        text = render("APPROVED_FOLLOWUP", lang, {"specialist": specialist, "action_done": done, "ref": ref, "ticket_id": ticket["id"]})
        store.update("escalations", esc_id, {"status": "APPROVED", "decided_at": now_ms(), "decided_by": specialist, "note": note})
        store.update("tickets", ticket["id"], {"status": "RESOLVED_HUMAN", "resolved_at": now_ms(),
                                               "recovered_amount": (ticket["recovered_amount"] or 0) + recovered})
    else:
        text = render("REJECTED_FOLLOWUP", lang, {"specialist": specialist, "note": note or ""}).replace("  ", " ")
        store.update("escalations", esc_id, {"status": "REJECTED", "decided_at": now_ms(), "decided_by": specialist, "note": note})
        store.update("tickets", ticket["id"], {"status": "HUMAN_FOLLOWUP"})
    store.audit(ticket["id"], f"human:{specialist}", "approve" if approve else "reject", {"escalation": esc_id, "note": note})
    store.add_message(mid, "assistant", text, ticket["id"])
    return {"escalation": store.escalation(esc_id), "ticket": store.ticket(ticket["id"]),
            "followup": {"merchant_id": mid, "ticket_id": ticket["id"], "text": text, "from": specialist}}


# ====================================================================== simulator
SIM_WEIGHTS = {"FAILED_PAYMENT": 5, "SETTLEMENT_DELAY": 4, "REFUND_ISSUE": 4, "SOUNDBOX": 3, "KYC": 2,
               "GENERAL": 3, "HUMAN_REQUEST": 1, "UNCLEAR": 1}


def _setup_case(store: Store, rng: random.Random, intent: str, mid: str) -> dict:
    """Create the simulated records a scenario needs; return template fields for the message."""
    fields: dict = {}
    t = now_ms()
    if intent == "FAILED_PAYMENT":
        amount = rng.choice([180, 450, 799, 1250, 2300, 3999, 6200])
        tid = store.next_id("sim_txn", "T885")
        bank_ok = rng.random() < 0.65
        store.insert("transactions", {"id": tid, "merchant_id": mid, "amount": amount, "method": "UPI", "payer": "customer@okhdfc",
                                      "created_at": t - rng.randint(20, 600) * 60000, "gateway_status": "FAILED",
                                      "bank_debited": True, "bank_status": "SUCCESS" if bank_ok else "FAILED"})
        fields["amount"] = inr(amount)
    elif intent == "SETTLEMENT_DELAY":
        m = store.merchant(mid)
        reason = "KYC_PENDING" if m["kyc_status"] != "VERIFIED" else "BANK_TIMEOUT"
        sid = store.next_id("sim_stl", "S2611")
        store.insert("settlements", {"id": sid, "merchant_id": mid, "amount": rng.randint(4000, 60000), "status": "ON_HOLD",
                                     "hold_reason": reason, "expected_at": t - rng.randint(30, 300) * 60000, "settled_at": None,
                                     "utr": None, "destination": m["bank"]})
    elif intent == "REFUND_ISSUE":
        amount = rng.choice([450, 1200, 2600, 7800, 9999])
        rid = store.next_id("sim_ref", "R8")
        status = "FAILED" if rng.random() < 0.7 else "PROCESSING"
        store.insert("refunds", {"id": rid, "merchant_id": mid, "txn_id": None, "amount": amount, "status": status,
                                 "reason": "BANK_REJECTED_TECHNICAL" if status == "FAILED" else None,
                                 "initiated_at": t - rng.randint(1000, 5000) * 60000, "arn": None,
                                 "eta_at": t + 1440 * 60000 if status == "PROCESSING" else None})
        fields["amount"] = inr(amount)
    elif intent == "SOUNDBOX":
        m = store.merchant(mid)
        sb = dict(m["soundbox"])
        issue = rng.choice(["volume", "signal", "both", "missed", "offline", "fault"])
        sb.update(volume=0 if issue in ("volume", "both") else 70,
                  signal="weak" if issue in ("signal", "both") else "good",
                  network_fallback=False if issue in ("signal", "both") else True,
                  online=issue != "offline", last_ping_minutes=rng.randint(400, 900) if issue == "offline" else 1,
                  missed_announcements=rng.randint(1, 4) if issue in ("missed", "signal") else 0,
                  fault="speaker" if issue == "fault" else None)
        store.update("merchants", mid, {"soundbox": sb})
    return fields


SIM_MERCHANTS = [
    ("M2001", "Sharma Tea Stall", "Pawan Sharma", "Jaipur", "hinglish", "VERIFIED"),
    ("M2002", "Lakshmi Textiles", "Lakshmi Rao", "Surat", "en", "VERIFIED"),
    ("M2003", "Patel Hardware", "Hitesh Patel", "Ahmedabad", "en", "PENDING_DOCS"),
    ("M2004", "Fresh Fruits Co", "Sneha Kulkarni", "Pune", "en", "VERIFIED"),
    ("M2005", "Noor Bakery", "Imran Noor", "Hyderabad", "hinglish", "VERIFIED"),
    ("M2006", "Singh Auto Parts", "Gurpreet Singh", "Ludhiana", "hi", "PENDING_DOCS"),
]


def _ensure_sim_merchants(store: Store) -> list[str]:
    """Simulated traffic uses its own merchants so the scripted demo merchants stay untouched."""
    for i, (mid, name, owner, city, lang, kyc) in enumerate(SIM_MERCHANTS):
        if store.merchant(mid):
            continue
        bank = ["SBI ••1182", "HDFC Bank ••6620", "PNB ••3091", "ICICI Bank ••7745", "SBI ••2208", "PNB ••5514"][i]
        store.insert("merchants", {
            "id": mid, "name": name, "owner": owner, "city": city, "category": "Retail", "lang": lang, "phone": "9000000000",
            "plan": "Soundbox 4G + QR", "settlement_cycle": "T+1", "kyc_status": kyc,
            "kyc_missing": ["GST certificate"] if kyc != "VERIFIED" else [], "bank": bank, "bank_verified": True,
            "soundbox": {"id": f"SB-5{i}210", "model": "Soundbox 4G", "online": True, "volume": 70, "signal": "good",
                         "language": "Hindi", "network_fallback": True, "fault": None, "battery": 70, "last_ping_minutes": 1,
                         "missed_announcements": 0}})
    return [m[0] for m in SIM_MERCHANTS]


async def simulate(store: Store, n: int, emit: Emit, handle, seed: Optional[int] = None) -> dict:
    rng = random.Random(seed if seed is not None else now_ms())
    intents = [k for k, w in SIM_WEIGHTS.items() for _ in range(w)]
    merchants = _ensure_sim_merchants(store)
    for i in range(n):
        intent = rng.choice(intents)
        mid = rng.choice(merchants)
        fields = _setup_case(store, rng, intent, mid)
        msg = rng.choice(LEXICON["simulation"][intent])["text"].format(**fields) if fields else \
            rng.choice(LEXICON["simulation"][intent])["text"].replace("{amount}", "")
        ticket = await handle(store, mid, msg, _noop, source="simulated", pacing=0.0)
        await emit({"type": "sim_progress", "i": i + 1, "n": n, "ticket": ticket, "metrics": metrics(store)})
    return metrics(store)
