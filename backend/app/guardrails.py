"""Guardrails: the rules that make an autonomous teammate safe in payments.

Every tool call goes through `check()` before it runs, whether the call came
from the rules engine or from an LLM. The model can propose anything; only
calls that pass these rules execute.
"""
from __future__ import annotations

import re
from typing import Optional

from .config import LEXICON, POLICY
from .text import inr

LIMIT = POLICY["auto_money_limit"]
RETRYABLE = set(POLICY["retryable_hold_reasons"])

MONEY_TOOLS = {"mark_payment_recovered", "retry_settlement", "reinitiate_refund"}
DEVICE_TOOLS = {"reconnect_device", "resend_missed_announcements", "set_announcement_language", "play_test_announcement"}
SOUNDBOX_LANGUAGES = set(LEXICON["announcement_languages"])


def verdict(allowed: bool, rule: str, reason: str, requires_approval: bool = False) -> dict:
    return {"allowed": allowed, "requires_approval": requires_approval, "rule": rule, "reason": reason}


LEVELS = POLICY["autonomy_levels"]


def autonomy(store) -> tuple[str, int]:
    """The ops-controlled autonomy level, read at call time: (level, autonomous money limit in ₹)."""
    level = store.setting("autonomy_level", POLICY["default_autonomy"])
    return level, LEVELS[level]["money_limit"]


def money_hold(store, amount: int, always_capped: bool = True) -> Optional[dict]:
    """Held-for-approval verdict if this amount is above what the current autonomy level allows, else None."""
    level, limit = autonomy(store)
    if level == "suggest":
        return verdict(False, "autonomy-level", "Autonomy level is 'Suggest only': every money action needs a human", True)
    if level != "standard" and amount > limit:
        return verdict(False, "autonomy-level", f"Autonomy level '{LEVELS[level]['label']}': {inr(amount)} is above its {inr(limit)} limit", True)
    if level == "standard" and always_capped and amount > LIMIT:
        return verdict(False, "money-limit", f"{inr(amount)} is above the {inr(LIMIT)} autonomous limit", True)
    return None


def check(name: str, args: dict, store, merchant_id: str, approved_by: Optional[str] = None) -> dict:
    """Return a verdict for one proposed tool call."""
    # 1. Scope: the agent may only touch records of the merchant it is serving.
    for key, table in (("txn_id", "transactions"), ("settlement_id", "settlements"), ("refund_id", "refunds")):
        if key in args:
            row = store.one(f"SELECT merchant_id FROM {table} WHERE id = ?", (str(args[key]),))
            if row is None:
                return verdict(False, "record-exists", f"{args[key]} does not exist")
            if row["merchant_id"] != merchant_id:
                return verdict(False, "merchant-scope", f"{args[key]} belongs to another merchant")

    if name == "mark_payment_recovered":
        txn = store.transaction(str(args.get("txn_id")))
        if txn["gateway_status"] == "SUCCESS" and not txn["recovered"]:
            return verdict(False, "state-check", f"{txn['id']} already succeeded; there is nothing to recover")
        if not txn.get("bank_checked") or txn.get("bank_status") != "SUCCESS":
            return verdict(False, "evidence-required", "Bank has not confirmed this payment; recheck status first")
        if not approved_by and (held := money_hold(store, txn["amount"])):
            return held
        return verdict(True, "money-limit", f"{inr(txn['amount'])} is within the {inr(autonomy(store)[1])} limit and bank-confirmed"
                       if not approved_by else f"Approved by {approved_by}")

    if name == "retry_settlement":
        s = store.settlement(str(args.get("settlement_id")))
        if s["status"] != "ON_HOLD":
            return verdict(False, "state-check", f"Settlement is {s['status']}, nothing to retry")
        if s["hold_reason"] not in RETRYABLE:
            return verdict(False, "retryable-only", f"Hold reason {s['hold_reason']} cannot be retried automatically")
        merchant = store.merchant(merchant_id)
        if not merchant["bank_verified"] or s["destination"] != merchant["bank"]:
            return verdict(False, "verified-destination", "Destination is not the merchant's verified bank account")
        # The merchant's own payout to their verified account: capped only below the standard level.
        if not approved_by and (held := money_hold(store, s["amount"], always_capped=False)):
            return held
        return verdict(True, "verified-destination", f"Same amount to the same verified account ({s['destination']}); retry is idempotent"
                       if not approved_by else f"Approved by {approved_by}")

    if name == "reinitiate_refund":
        r = store.refund(str(args.get("refund_id")))
        if r["status"] != "FAILED":
            return verdict(False, "state-check", f"Refund is {r['status']}, nothing to re-send")
        if not approved_by and (held := money_hold(store, r["amount"])):
            return held
        return verdict(True, "money-limit", f"{inr(r['amount'])} is within the {inr(autonomy(store)[1])} limit"
                       if not approved_by else f"Approved by {approved_by}")

    if name == "set_announcement_language":
        if str(args.get("language")) not in SOUNDBOX_LANGUAGES:
            return verdict(False, "value-range", f"{args.get('language')} is not a language the Soundbox supports")
        return verdict(True, "device-action", "Device setting the Soundbox supports remotely; no money involved")

    if name in DEVICE_TOOLS:
        return verdict(True, "device-action", "Remote device action; no money involved")

    return verdict(True, "read-or-low-risk", "Read-only or low-risk action")


# ---------------------------------------------------------------- privacy
_PHONE = re.compile(r"\b([6-9]\d)\d{6}(\d{2})\b")
_VPA = re.compile(r"\b([a-z0-9._-]{2})[a-z0-9._-]*@([a-z]+)\b", re.I)
_LONGNUM = re.compile(r"\b\d{8,15}(\d{4})\b")


def mask_pii(text: str) -> str:
    """Mask phone numbers, UPI IDs/emails and long account/card numbers before logging or sending to an LLM."""
    if not text:
        return text
    text = _PHONE.sub(lambda m: f"{m.group(1)}XXXXXX{m.group(2)}", text)
    text = _VPA.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    text = _LONGNUM.sub(lambda m: f"••{m.group(1)}", text)
    return text
