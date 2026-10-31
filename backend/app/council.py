"""Model Council: no single model can move money.

Guardrails decide whether a money movement is *allowed by policy*. The council
decides whether it is *safe right now*. Before any money tool runs without a
human's approval, it goes to two kinds of independent reviewers:

  * Auditors (code, always on). Each re-reads the ledger on its own:
      ledger    - re-verifies the evidence straight from the records
      replay    - replays the audit log to catch a double payout
      velocity  - caps how much the agent may move for one merchant per day
    Any auditor can veto.

  * Model jurors (one per configured provider, from different labs). Each sees
    the proposed action and the evidence and votes APPROVE or HOLD. A model
    that times out abstains. Approval needs a majority of the votes cast.

Reviewers run in parallel. A HOLD sends the action to a human with the
council's reasons attached, exactly like a money-limit hold.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from .config import LLM_TIMEOUT, POLICY, PROVIDERS, ModelSpec, mode, model_for, provider_available
from .llm import chat, parse_json
from .store import Store, now_ms
from .text import inr

DAILY_CAP = POLICY.get("council_daily_cap", 20000)
DAILY_CAP_KYC_PENDING = POLICY.get("council_daily_cap_kyc_pending", 2000)
MAX_JURORS = 5
DAY_MS = 24 * 60 * 60 * 1000
CAPPED = {"mark_payment_recovered", "reinitiate_refund"}    # money leaving the company's books; settlement retries are the merchant's own funds


def jurors() -> list[ModelSpec]:
    """One fast model per configured provider, so the jury spans different labs."""
    if mode() == "offline":
        return []
    triage = model_for("triage")
    out = []
    for name, p in PROVIDERS.items():
        if provider_available(name):
            # Reuse the light triage model on its provider: one less model to rate-limit on free tiers.
            if triage and triage.provider == name:
                out.append(triage)
                continue
            key = os.getenv(p["key_env"]) if p.get("key_env") else None
            out.append(ModelSpec(name, p["fast"], p["kind"], p["base"], key))
    return out[:MAX_JURORS]


def amount_of(store: Store, name: str, args: dict) -> int:
    if name == "mark_payment_recovered":
        row = store.transaction(str(args.get("txn_id")))
    elif name == "retry_settlement":
        row = store.settlement(str(args.get("settlement_id")))
    elif name == "reinitiate_refund":
        row = store.refund(str(args.get("refund_id")))
    else:
        row = None
    return int(row["amount"]) if row else 0


def record_key(args: dict) -> Optional[str]:
    return args.get("txn_id") or args.get("settlement_id") or args.get("refund_id")


def _vote(seat: str, who: str, kind: str, vote: str, reason: str, ms: int = 0) -> dict:
    return {"seat": seat, "who": who, "kind": kind, "vote": vote, "reason": reason, "ms": ms}


# ---------------------------------------------------------------- auditors
def audit_ledger(store: Store, name: str, args: dict) -> dict:
    if name == "mark_payment_recovered":
        t = store.transaction(str(args["txn_id"]))
        ok = t["bank_checked"] and t["bank_status"] == "SUCCESS" and t["bank_debited"]
        why = "Bank says SUCCESS and the customer was debited" if ok else "Ledger does not show a confirmed customer debit"
    elif name == "retry_settlement":
        s = store.settlement(str(args["settlement_id"]))
        m = store.merchant(s["merchant_id"])
        ok = s["status"] == "ON_HOLD" and s["destination"] == m["bank"] and bool(m["bank_verified"])
        why = f"Payout still on hold, same verified account {s['destination']}" if ok else "Payout state or destination changed"
    else:
        r = store.refund(str(args["refund_id"]))
        ok = r["status"] == "FAILED"
        why = f"Refund bounced ({r['reason']}), customer has not been paid" if ok else f"Refund is {r['status']}"
    return _vote("Ledger auditor", "ledger", "auditor", "APPROVE" if ok else "HOLD", why)


def _prior_moves(store: Store, merchant_id: str) -> list[dict]:
    rows = store.all(
        "SELECT a.ts, a.action, a.detail FROM audit a JOIN tickets t ON t.id = a.ticket_id "
        "WHERE t.merchant_id = ? AND a.action IN ('mark_payment_recovered', 'retry_settlement', 'reinitiate_refund')",
        (merchant_id,))
    return [r for r in rows if isinstance(r["detail"], dict) and r["detail"].get("allowed")]


def audit_replay(prior: list[dict], name: str, args: dict) -> dict:
    key = record_key(args)
    dup = [r for r in prior if r["action"] == name and record_key(r["detail"].get("args") or {}) == key]
    if dup:
        return _vote("Replay auditor", "replay", "auditor", "HOLD", f"{key} was already actioned; a second payout would double-pay")
    return _vote("Replay auditor", "replay", "auditor", "APPROVE", f"Audit log replayed: {key} has never been paid out")


def audit_velocity(store: Store, merchant_id: str, prior: list[dict], name: str, args: dict) -> dict:
    """Risk-tiered daily cap: merchants with incomplete KYC get a much smaller autonomous allowance."""
    kyc_ok = store.merchant(merchant_id)["kyc_status"] == "VERIFIED"
    cap = DAILY_CAP if kyc_ok else DAILY_CAP_KYC_PENDING
    tier = "daily cap" if kyc_ok else "cap while KYC is pending"
    if name not in CAPPED:
        return _vote("Velocity auditor", "velocity", "auditor", "APPROVE",
                     "Merchant's own payout to their verified account; not new money, so no cap applies")
    since = now_ms() - DAY_MS
    moved = sum(amount_of(store, r["action"], r["detail"].get("args") or {}) for r in prior if r["ts"] >= since and r["action"] in CAPPED)
    total = moved + amount_of(store, name, args)
    if total > cap:
        return _vote("Velocity auditor", "velocity", "auditor", "HOLD",
                     f"{inr(total)} in 24h would pass the {inr(cap)} {tier}")
    return _vote("Velocity auditor", "velocity", "auditor", "APPROVE", f"{inr(total)} of the {inr(cap)} {tier} used after this")


# ---------------------------------------------------------------- model jurors
JUROR_PROMPT = (
    "You are one juror on a payments safety council at a payments company in India. An autonomous support agent wants to move "
    "money. Decide independently whether the EVIDENCE fully supports the ACTION. Vote HOLD if anything is missing, "
    "inconsistent, or suspicious; otherwise APPROVE. Reply with only JSON: "
    '{"vote": "APPROVE" | "HOLD", "reason": "<= 18 words"}'
)


async def ask_juror(spec: ModelSpec, case: dict) -> dict:
    t0 = time.perf_counter()
    try:
        out = await asyncio.wait_for(
            chat(spec, [{"role": "system", "content": JUROR_PROMPT}, {"role": "user", "content": json.dumps(case, default=str)}],
                 json_mode=True, temperature=0, max_tokens=120),
            timeout=LLM_TIMEOUT)
        data = parse_json(out["text"])
        vote = "HOLD" if str(data.get("vote", "")).upper().startswith("HOLD") else "APPROVE"
        reason = str(data.get("reason") or "").strip()[:160] or "No reason given"
    except Exception as exc:  # timeout, bad key, unparseable reply: abstain, never block the demo on a flaky provider
        vote, reason = "ABSTAIN", f"Abstained ({type(exc).__name__})"
    return _vote(spec.provider.title(), spec.label, "model", vote, reason, int((time.perf_counter() - t0) * 1000))


def evidence(store: Store, name: str, args: dict) -> dict:
    if name == "mark_payment_recovered":
        t = store.transaction(str(args["txn_id"]))
        return {k: t[k] for k in ("id", "amount", "gateway_status", "bank_status", "bank_checked", "bank_debited", "recovered")}
    if name == "retry_settlement":
        s = store.settlement(str(args["settlement_id"]))
        m = store.merchant(s["merchant_id"])
        return {**{k: s[k] for k in ("id", "amount", "status", "hold_reason", "destination")},
                "merchant_verified_bank": m["bank"], "bank_verified": bool(m["bank_verified"])}
    r = store.refund(str(args["refund_id"]))
    return {k: r[k] for k in ("id", "amount", "status", "reason", "txn_id")}


# ---------------------------------------------------------------- session
async def review(store: Store, merchant_id: str, name: str, args: dict, guard_reason: str) -> dict:
    t0 = time.perf_counter()
    prior = _prior_moves(store, merchant_id)
    votes = [audit_ledger(store, name, args), audit_replay(prior, name, args), audit_velocity(store, merchant_id, prior, name, args)]
    panel = jurors()
    if panel:
        case = {"action": name, "args": args, "amount_inr": amount_of(store, name, args), "evidence": evidence(store, name, args),
                "policy": {"autonomous_limit_inr": POLICY["auto_money_limit"], "guardrail_verdict": guard_reason}}
        votes += await asyncio.gather(*(ask_juror(s, case) for s in panel))

    auditor_veto = [v for v in votes if v["kind"] == "auditor" and v["vote"] == "HOLD"]
    cast = [v for v in votes if v["kind"] == "model" and v["vote"] != "ABSTAIN"]
    approvals = sum(1 for v in cast if v["vote"] == "APPROVE")
    majority = not cast or approvals * 2 > len(cast)
    approved = not auditor_veto and majority
    if auditor_veto:
        reason = auditor_veto[0]["reason"]
    elif not majority:
        reason = f"Model jury voted to hold ({len(cast) - approvals} of {len(cast)})"
    else:
        reason = f"{sum(1 for v in votes if v['vote'] == 'APPROVE')} of {len(votes)} seats approved"
    return {"approved": approved, "reason": reason, "votes": votes, "amount": amount_of(store, name, args),
            "ms": int((time.perf_counter() - t0) * 1000), "jurors": len(panel)}
