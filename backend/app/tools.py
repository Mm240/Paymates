"""Mock Paytm merchant APIs, exposed to the agents as tools.

In production these would call Paytm's internal services. Here they read and
write the simulated SQLite data. Every call runs through `ToolRunner`, which
applies guardrails, writes the audit log and streams the call to the UI.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional

from . import council
from .config import LEXICON
from .guardrails import MONEY_TOOLS, check, mask_pii
from .rag import index
from .store import Store, now_ms
from .text import inr

SOUNDBOX_LANGUAGES = list(LEXICON["announcement_languages"])
OFFLINE_LONG_MIN = 6 * 60      # silent this long -> treat as a device that needs a technician


# ---------------------------------------------------------------- tool implementations
def _txn_view(t: dict) -> dict:
    return {"txn_id": t["id"], "utr": t.get("utr"), "amount": t["amount"], "amount_display": inr(t["amount"]), "method": t["method"],
            "payer": mask_pii(t["payer"]), "gateway_status": t["gateway_status"],
            "customer_debited": t["bank_debited"], "recovered": t["recovered"],
            "minutes_ago": int((now_ms() - t["created_at"]) / 60000)}


def get_merchant_profile(s: Store, mid: str, a: dict) -> dict:
    m = s.merchant(mid)
    return {"merchant_id": m["id"], "name": m["name"], "owner": m["owner"], "city": m["city"], "plan": m["plan"],
            "settlement_cycle": m["settlement_cycle"], "kyc_status": m["kyc_status"], "bank": m["bank"],
            "preferred_language": m["lang"]}


def list_transactions(s: Store, mid: str, a: dict) -> dict:
    status = a.get("status")
    rows = s.transactions(mid, status=status.upper() if status else None, limit=int(a.get("limit", 6)))
    return {"transactions": [_txn_view(t) for t in rows]}


def get_transaction(s: Store, mid: str, a: dict) -> dict:
    t = s.transaction(str(a["txn_id"]))
    return _txn_view(t) if t else {"error": "not found"}


def find_transaction(s: Store, mid: str, a: dict) -> dict:
    """Look a payment up by its 12-digit UPI UTR or by transaction ID. Only this merchant's payments are visible."""
    t = None
    if a.get("utr"):
        t = s.transaction_by_utr(mid, str(a["utr"]))
    elif a.get("txn_id"):
        t = s.transaction(str(a["txn_id"]))
        t = t if t and t["merchant_id"] == mid else None
    return _txn_view(t) if t else {"error": "not found"}


def recheck_payment_status(s: Store, mid: str, a: dict) -> dict:
    """Ask the customer's bank / UPI network for the final status of a payment."""
    t = s.transaction(str(a["txn_id"]))
    s.update("transactions", t["id"], {"bank_checked": True})
    return {"txn_id": t["id"], "bank_final_status": t["bank_status"], "customer_debited": t["bank_debited"],
            "note": "Bank confirms the debit succeeded (late confirmation)" if t["bank_status"] == "SUCCESS" and t["gateway_status"] == "FAILED"
            else ("Bank confirms failure; auto-reversal to customer within T+1" if t["bank_debited"] else "No debit happened")}


def mark_payment_recovered(s: Store, mid: str, a: dict) -> dict:
    t = s.transaction(str(a["txn_id"]))
    if t["recovered"]:
        return {"txn_id": t["id"], "already_recovered": True}
    s.update("transactions", t["id"], {"gateway_status": "SUCCESS", "recovered": True})
    return {"txn_id": t["id"], "new_status": "SUCCESS", "credited_to_next_settlement": t["amount"],
            "amount_display": inr(t["amount"]), "recovered": True}


def list_settlements(s: Store, mid: str, a: dict) -> dict:
    return {"settlements": [
        {"settlement_id": x["id"], "amount": x["amount"], "amount_display": inr(x["amount"]), "status": x["status"],
         "hold_reason": x["hold_reason"], "utr": x["utr"], "destination": x["destination"],
         "overdue_minutes": max(0, int((now_ms() - x["expected_at"]) / 60000)) if x["status"] != "SETTLED" else 0}
        for x in s.settlements(mid, limit=int(a.get("limit", 5)))
    ]}


def retry_settlement(s: Store, mid: str, a: dict) -> dict:
    x = s.settlement(str(a["settlement_id"]))
    code = next((c for k, c in (("SBI", "SBIN"), ("HDFC", "HDFC"), ("PNB", "PUNB")) if x["destination"].startswith(k)), "UTRN")
    utr = f"{code}5261{s.next_id('utr', '')}04"
    s.update("settlements", x["id"], {"status": "PROCESSING", "hold_reason": None, "utr": utr,
                                       "expected_at": now_ms() + 2 * 3600_000})
    return {"settlement_id": x["id"], "new_status": "PROCESSING", "new_utr": utr, "eta_minutes": 120,
            "amount_display": inr(x["amount"])}


def list_refunds(s: Store, mid: str, a: dict) -> dict:
    return {"refunds": [_refund_view(r) for r in s.refunds(mid, limit=int(a.get("limit", 5)))]}


def _refund_view(r: dict) -> dict:
    return {"refund_id": r["id"], "txn_id": r["txn_id"], "amount": r["amount"], "amount_display": inr(r["amount"]),
            "status": r["status"], "failure_reason": r["reason"], "arn": r["arn"],
            "eta_minutes": max(0, int((r["eta_at"] - now_ms()) / 60000)) if r.get("eta_at") else None}


def get_refund(s: Store, mid: str, a: dict) -> dict:
    r = s.refund(str(a["refund_id"]))
    return _refund_view(r) if r else {"error": "not found"}


def reinitiate_refund(s: Store, mid: str, a: dict) -> dict:
    r = s.refund(str(a["refund_id"]))
    ref = s.next_id("ref", "RF")
    s.update("refunds", r["id"], {"status": "PROCESSING", "reason": None, "eta_at": now_ms() + 24 * 3600_000})
    return {"refund_id": r["id"], "new_status": "PROCESSING", "new_reference": ref, "amount_display": inr(r["amount"]),
            "destination": "customer's original payment source"}


def get_kyc_status(s: Store, mid: str, a: dict) -> dict:
    m = s.merchant(mid)
    return {"kyc_status": m["kyc_status"], "missing_documents": m["kyc_missing"]}


def run_soundbox_diagnostics(s: Store, mid: str, a: dict) -> dict:
    sb = s.merchant(mid)["soundbox"]
    issues = []
    if sb.get("fault"):
        issues.append("hardware_fault")
    if not sb.get("online"):
        issues.append("offline_long" if sb.get("last_ping_minutes", 0) > OFFLINE_LONG_MIN else "offline")
    if sb.get("volume", 0) == 0:
        issues.append("volume_zero")
    if sb.get("signal") == "weak" and not sb.get("network_fallback"):
        issues.append("weak_signal_no_fallback")
    if sb.get("missed_announcements", 0) > 0:
        issues.append("missed_announcements")
    return {"device_id": sb["id"], "model": sb["model"], "online": sb["online"], "volume": sb["volume"],
            "signal": sb["signal"], "network_fallback": sb["network_fallback"], "battery": sb.get("battery"),
            "last_ping_minutes": sb.get("last_ping_minutes"), "missed_announcements": sb.get("missed_announcements", 0),
            "language": sb.get("language"), "fault": sb.get("fault"), "issues": issues}


# Remote device actions: only what a connected Soundbox really allows. Volume is a physical button.
def _save_sb(s: Store, mid: str, sb: dict) -> None:
    s.update("merchants", mid, {"soundbox": sb})


def reconnect_device(s: Store, mid: str, a: dict) -> dict:
    sb = dict(s.merchant(mid)["soundbox"])
    switched = sb["signal"] == "weak" and not sb["network_fallback"]
    if switched:
        sb.update(network_fallback=True, signal="good (backup network)")
    sb.update(online=True, last_ping_minutes=0)
    _save_sb(s, mid, sb)
    return {"device_id": sb["id"], "online": True, "signal": sb["signal"], "switched_to_backup_network": switched}


def resend_missed_announcements(s: Store, mid: str, a: dict) -> dict:
    sb = dict(s.merchant(mid)["soundbox"])
    n = sb.get("missed_announcements", 0)
    sb["missed_announcements"] = 0
    _save_sb(s, mid, sb)
    return {"device_id": sb["id"], "replayed": n}


def set_announcement_language(s: Store, mid: str, a: dict) -> dict:
    sb = dict(s.merchant(mid)["soundbox"])
    sb["language"] = str(a["language"])
    _save_sb(s, mid, sb)
    return {"device_id": sb["id"], "language": sb["language"], "test_announcement": "played"}


def play_test_announcement(s: Store, mid: str, a: dict) -> dict:
    sb = s.merchant(mid)["soundbox"]
    return {"device_id": sb["id"], "played": True, "audible": sb["volume"] > 0, "volume": sb["volume"], "language": sb.get("language")}


def raise_ticket(s: Store, mid: str, a: dict, ticket_id: Optional[str] = None) -> dict:
    ref = s.next_id("ops", "OPS-")
    s.insert("ops_tickets", {"id": ref, "merchant_id": mid, "category": a.get("category", "general"),
                             "priority": a.get("priority", "P3"), "summary": a.get("summary", ""),
                             "created_at": now_ms(), "parent_ticket": ticket_id})
    return {"ticket_ref": ref, "priority": a.get("priority", "P3"), "queue": a.get("category", "general")}


def search_help_center(s: Store, mid: str, a: dict) -> dict:
    return {"results": [{"id": d["id"], "title": d["title"], "score": d["score"], "snippet": d["snippet"]}
                        for d in index.search(str(a.get("query", "")), k=3)]}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


# name -> (function, kind, description, json schema)
TOOLS: dict[str, tuple[Callable, str, str, dict]] = {
    "get_merchant_profile": (get_merchant_profile, "read", "Get the merchant's profile: plan, KYC status, bank, settlement cycle.", _schema({})),
    "list_transactions": (list_transactions, "read", "List the merchant's recent payments. Optional status filter: SUCCESS or FAILED.",
                          _schema({"status": {"type": "string", "enum": ["SUCCESS", "FAILED"]}, "limit": {"type": "integer"}})),
    "get_transaction": (get_transaction, "read", "Get one payment by transaction ID.", _schema({"txn_id": {"type": "string"}}, ["txn_id"])),
    "find_transaction": (find_transaction, "read", "Find a payment by its 12-digit UPI UTR/RRN or by transaction ID (starts with T). Returns an error if there is no such payment on this merchant's account.",
                         _schema({"utr": {"type": "string"}, "txn_id": {"type": "string"}})),
    "recheck_payment_status": (recheck_payment_status, "check", "Ask the customer's bank / UPI network for the final status of a payment that shows FAILED. Required before marking it recovered.",
                               _schema({"txn_id": {"type": "string"}}, ["txn_id"])),
    "mark_payment_recovered": (mark_payment_recovered, "write", "Mark a bank-confirmed payment as successful and credit it to the merchant's next settlement. Moves money; guarded.",
                               _schema({"txn_id": {"type": "string"}}, ["txn_id"])),
    "list_settlements": (list_settlements, "read", "List recent settlements (payouts to the merchant's bank) with status, hold reason and UTR.", _schema({"limit": {"type": "integer"}})),
    "retry_settlement": (retry_settlement, "write", "Re-send a settlement that is ON_HOLD for a retryable reason (BANK_TIMEOUT) to the merchant's verified bank account. Moves money; guarded.",
                         _schema({"settlement_id": {"type": "string"}}, ["settlement_id"])),
    "list_refunds": (list_refunds, "read", "List refunds the merchant issued to customers, with status and failure reason.", _schema({"limit": {"type": "integer"}})),
    "get_refund": (get_refund, "read", "Get one refund by refund ID.", _schema({"refund_id": {"type": "string"}}, ["refund_id"])),
    "reinitiate_refund": (reinitiate_refund, "write", "Re-send a FAILED refund to the customer's original payment source. Moves money; amounts above the limit need human approval.",
                          _schema({"refund_id": {"type": "string"}}, ["refund_id"])),
    "get_kyc_status": (get_kyc_status, "read", "Get KYC status and any missing documents.", _schema({})),
    "run_soundbox_diagnostics": (run_soundbox_diagnostics, "check", "Run remote diagnostics on the merchant's Soundbox: online, volume, signal, battery, last ping, missed announcements, faults.", _schema({})),
    "reconnect_device": (reconnect_device, "write", "Re-register the Soundbox on the network; moves it to the backup network if the signal is weak.", _schema({})),
    "resend_missed_announcements": (resend_missed_announcements, "write", "Re-play payment announcements the Soundbox missed. Only useful when the volume is above 0.", _schema({})),
    "set_announcement_language": (set_announcement_language, "write", "Change the language the Soundbox announces payments in.",
                                  _schema({"language": {"type": "string", "enum": SOUNDBOX_LANGUAGES}}, ["language"])),
    "play_test_announcement": (play_test_announcement, "write", "Play a test payment announcement on the Soundbox.", _schema({})),
    "raise_ticket": (raise_ticket, "write", "Open an operations ticket for tracking or follow-up by a team (e.g. settlements, kyc, devices).",
                     _schema({"category": {"type": "string"}, "priority": {"type": "string", "enum": ["P1", "P2", "P3"]}, "summary": {"type": "string"}}, ["category", "summary"])),
    "search_help_center": (search_help_center, "read", "Search the merchant help center for policies and how-to answers.",
                           _schema({"query": {"type": "string"}}, ["query"])),
    "escalate_to_human": (None, "handoff", "Hand the case to a human specialist with a reason. Use when unsure, when the merchant asks for a person, or when a needed action was held for approval.",
                          _schema({"reason": {"type": "string"}}, ["reason"])),
}


def tool_specs(names: Optional[list[str]] = None) -> list[dict]:
    return [{"name": n, "description": d, "parameters": p} for n, (_, _, d, p) in TOOLS.items() if names is None or n in names]


Emit = Callable[[dict], Awaitable[None]]


class ToolRunner:
    """Executes tool calls for one ticket: guardrails -> run -> audit -> stream."""

    def __init__(self, store: Store, merchant_id: str, ticket_id: str, emit: Emit, actor: str = "action-agent"):
        self.store, self.mid, self.ticket_id, self.emit, self.actor = store, merchant_id, ticket_id, emit, actor
        self.calls: list[dict] = []
        self.blocked: list[dict] = []          # calls held for human approval
        self.handoff: Optional[str] = None     # reason if the agent escalated
        self.recovered = 0
        self.sessions: list[dict] = []         # Model Council reviews of money moves

    async def run(self, name: str, args: dict, approved_by: Optional[str] = None) -> dict:
        args = {k: v for k, v in (args or {}).items() if v is not None}
        if name not in TOOLS:
            return {"error": f"unknown tool {name}"}
        fn, kind, _, _ = TOOLS[name]
        started = time.perf_counter()
        session = None

        if name == "escalate_to_human":
            self.handoff = str(args.get("reason") or "Agent requested a human")
            result: dict[str, Any] = {"handed_off": True}
            guard = {"allowed": True, "rule": "handoff", "reason": "Human handoff requested"}
        else:
            guard = check(name, args, self.store, self.mid, approved_by)
            if guard["allowed"] and name in MONEY_TOOLS and not approved_by:
                # Policy allows it; now the council must agree it is safe. A human approval overrides.
                await self.emit({"type": "step", "agent": "council", "status": "running",
                                 "title": f"Council reviewing {name.replace('_', ' ')}"})
                session = await council.review(self.store, self.mid, name, args, guard["reason"])
                self.sessions.append({"tool": name, "args": args, **session})
                self.store.audit(self.ticket_id, "model-council", "review",
                                 {"tool": name, "args": args, "approved": session["approved"], "reason": session["reason"],
                                  "votes": [f"{v['who']}:{v['vote']}" for v in session["votes"]]})
                if not session["approved"]:
                    guard = {"allowed": False, "requires_approval": True, "rule": "model-council",
                             "reason": f"Council held it: {session['reason']}"}
            if not guard["allowed"]:
                result = {"blocked": True, "requires_approval": guard["requires_approval"], "reason": guard["reason"]}
                if guard["requires_approval"]:
                    self.blocked.append({"tool": name, "args": args, "reason": guard["reason"]})
            else:
                try:
                    if name == "raise_ticket":
                        result = fn(self.store, self.mid, args, self.ticket_id)
                    else:
                        result = fn(self.store, self.mid, args)
                except Exception as exc:  # bad args from a model, missing record...
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                if name == "mark_payment_recovered" and result.get("recovered"):
                    self.recovered += int(result.get("credited_to_next_settlement", 0))

        ms = int((time.perf_counter() - started) * 1000)
        record = {"name": name, "kind": kind, "args": args, "result": result, "guard": guard, "ms": ms,
                  "money": name in MONEY_TOOLS, "council": session}
        self.calls.append(record)
        self.store.audit(self.ticket_id, approved_by or self.actor, name,
                         {"args": args, "allowed": guard["allowed"], "rule": guard["rule"], "reason": guard["reason"]})
        await self.emit({"type": "tool", **record})
        return result
