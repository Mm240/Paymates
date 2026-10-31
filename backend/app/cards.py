"""Rich reply cards (generative UI): the result of a ticket at a glance.

A card is built from the outcome and its facts, never generated. The layout of
each card lives in shared/templates.json ("cards"), so the browser engine
builds exactly the same card. Only the live parts are added here in code:
Soundbox readings, Council votes, and the hisaab bars.

    {type, status: success|pending|held|info, title, amount, refs: [{label, value}],
     rows: [{label, value}], steps: [{label, state: done|current|todo}], bars, actions: [{label, action}]}
"""
from __future__ import annotations

from typing import Optional

from .config import TEMPLATES
from .store import Store

SPEC = TEMPLATES["cards"]
EMPTY = (None, "", "-")


def label(key: str, lang: str) -> str:
    t = SPEC["labels"].get(key)
    return (t.get(lang) or t["en"]) if t else key


def _value(src: str, facts: dict, lang: str):
    return label(src[1:], lang) if src.startswith("@") else facts.get(src)


def _quick(item: str, lang: str) -> dict:
    """'send:topic_refund' -> a chip that sends its own label; 'action:add_txn_id' -> a chip that runs a card action."""
    kind, key = item.split(":", 1)
    return {"label": label(key if kind == "send" else f"a_{key}", lang), **({"send": label(key, lang)} if kind == "send" else {"action": key})}


def build(outcome: str, facts: dict, lang: str, store: Optional[Store] = None, mid: Optional[str] = None,
          extra: Optional[dict] = None) -> dict:
    spec = SPEC["outcomes"].get(outcome) or SPEC["outcomes"]["_default"]
    extra = extra or {}
    pairs = lambda items: [{"label": label(k, lang), "value": v} for k, src in items  # noqa: E731
                           if (v := _value(src, facts, lang)) not in EMPTY]
    card = {
        "type": spec["type"], "lang": lang, "status": spec["status"], "status_label": label(f"s_{spec['status']}", lang),
        "title": label(spec["title"], lang),
        "subtitle": label(spec["subtitle"], lang) if spec.get("subtitle") else None,
        "amount": facts.get("amount") if spec.get("amount", True) and facts.get("amount") not in EMPTY else None,
        "refs": pairs(spec.get("refs", [])),
        "rows": pairs(spec.get("rows", [])),
        "steps": [{"label": label(k, lang), "state": s} for k, s in spec.get("steps", [])],
        "actions": [{"label": label(f"a_{a}", lang), "action": a} for a in spec.get("actions", [])][:2],
        "quick": [_quick(q, lang) for q in spec.get("quick", [])],
    }
    if extra.get("escalation_id"):
        card["refs"].append({"label": label("l_case", lang), "value": extra["escalation_id"]})
    if spec["type"] == "council_hold" and extra.get("votes"):
        card["rows"] += [{"label": v["seat"], "value": label(f"v_{v['vote']}", lang), "vote": v["vote"]} for v in extra["votes"]]
    if spec["type"] == "soundbox_status" and store and mid:
        sb = store.merchant(mid)["soundbox"]
        card["rows"] = [
            {"label": label("l_online", lang), "value": label("v_yes" if sb.get("online") else "v_no", lang)},
            {"label": label("l_signal", lang), "value": sb.get("signal")},
            {"label": label("l_battery", lang), "value": f"{sb.get('battery')}%" if sb.get("battery") is not None else None},
            {"label": label("l_volume", lang), "value": f"{sb.get('volume')}%"},
        ] + card["rows"]
        card["rows"] = [r for r in card["rows"] if r["value"] not in EMPTY]
    return card


def hisaab_card(lang: str, upi: int, card_amt: int, upi_text: str, card_text: str, fixed_items: list[str]) -> dict:
    card = build("HISAAB", {}, lang)
    card["bars"] = [{"label": label("h_upi", lang), "amount": upi_text, "value": upi, "kind": "upi"},
                    {"label": label("h_card", lang), "amount": card_text, "value": card_amt, "kind": "card"}]
    if fixed_items:
        card["rows"] = [{"label": label("l_fixed_today", lang), "value": item} for item in fixed_items]
        card["status"], card["status_label"] = "success", label("s_success", lang)
    return card
