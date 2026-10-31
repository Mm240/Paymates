"""Daily hisaab: one plain summary of the merchant's day, in their language.

    UPI received today        -> already in the bank
    card / wallet today       -> arrives next working day
    what PayMates fixed today -> with amounts, from today's resolved tickets
"""
from __future__ import annotations

from .store import Store, now_ms
from .text import inr, phrase, render

DAY_MS = 24 * 60 * 60 * 1000
FIXED = ("PAYMENT_RECOVERED", "SETTLEMENT_RETRIED", "REFUND_REINITIATED", "SOUNDBOX_FIXED", "SOUNDBOX_LANGUAGE_SET")


def compute(store: Store, mid: str) -> dict:
    m = store.merchant(mid)
    lang = m["lang"]
    since = now_ms() - DAY_MS
    paid = store.all("SELECT * FROM transactions WHERE merchant_id = ? AND gateway_status = 'SUCCESS' AND created_at >= ?", (mid, since))
    upi = sum(t["amount"] for t in paid if t["method"] == "UPI" and not t["recovered"])
    card = sum(t["amount"] for t in paid if t["method"] in ("Card", "Wallet"))
    marks = ",".join("?" for _ in FIXED)
    done = store.all(f"SELECT * FROM tickets WHERE merchant_id = ? AND status = 'RESOLVED_AI' AND created_at >= ? "
                     f"AND outcome IN ({marks}) ORDER BY created_at", (mid, since, *FIXED))
    items = [phrase(f"hisaab_{t['outcome']}", lang, amount=(t["facts"] or {}).get("amount", "")) for t in done]
    fixed = phrase("hisaab_fixed", lang, items=phrase("list_sep", lang).join(items)) if items else phrase("hisaab_none", lang)
    text = render("HISAAB", lang, {"owner_first": m["owner"].split()[0], "upi": inr(upi), "card": inr(card), "fixed": fixed})
    return {"merchant_id": mid, "upi_received": upi, "card_pending": card, "fixed": [t["id"] for t in done], "text": text}


def send(store: Store, mid: str) -> dict:
    out = compute(store, mid)
    store.add_message(mid, "assistant", out["text"], None)
    store.audit(None, "paymates", "hisaab", {"merchant": mid, "upi": out["upi_received"], "card": out["card_pending"]})
    return out
