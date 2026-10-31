"""Action agent: decide what to do and do it, through guarded tools.

Two strategies, same tools, same guardrails:

* LLM: a strong tool-calling model plans and calls tools in a loop, steered
  by the domain playbook for the intent.
* Playbook (rules): deterministic runbooks per intent. Used when no model is
  configured, and as the automatic fallback if the model errors mid-case.

Either way the result is a structured outcome the responder turns into a
reply, plus any action that was held for human approval.
"""
from __future__ import annotations

import json
from typing import Optional

from ..config import LEXICON, POLICY, TEMPLATES, model_for
from ..llm import chat, parse_json
from ..rag import index
from ..store import Store
from ..text import ago, inr, phrase, small_talk_kind, tokens
from ..tools import ToolRunner, tool_specs

LIMIT = POLICY["auto_money_limit"]


def result(status: str, outcome: str, facts: dict, summary: str, proposed: Optional[dict] = None,
           reason: Optional[str] = None) -> dict:
    if outcome.endswith("_NEEDS_APPROVAL") and (reason or "").startswith("Council held it"):
        outcome = "COUNCIL_HOLD"        # within the money limit, but the Model Council said wait
    elif outcome.endswith("_NEEDS_APPROVAL") and (reason or "").startswith("Autonomy level"):
        outcome = "AUTONOMY_HOLD"       # ops turned autonomy down; a human approves money moves for now
    return {"status": status, "outcome": outcome, "facts": facts, "summary": summary,
            "proposed_action": proposed, "escalation_reason": reason}


def reason_text(code: Optional[str], lang: str) -> str:
    key = f"reason_{code}"
    if code and key in TEMPLATES["phrases"]:
        return phrase(key, lang)
    return code or ""


# ====================================================================== playbooks
def ask(outcome: str, base: dict, why: str, **facts) -> dict:
    """Not enough to go on: ask the merchant for the reference instead of acting on a guess."""
    return result("NEEDS_INFO", outcome, {**base, **facts}, why)


async def pb_failed_payment(run: ToolRunner, store: Store, tri: dict, base: dict) -> dict:
    ents, lang = tri["entities"], tri["language"]
    ref = ents.get("utr") or ents.get("txn_id")
    if ref:
        r = await run.run("find_transaction", {"utr": ents["utr"]} if ents.get("utr") else {"txn_id": ents["txn_id"]})
        if "txn_id" not in r:
            return ask("ID_NOT_FOUND", base, f"{ref} is not on this merchant's account; asked them to re-check it", ref=ref)
        txn = r
    else:
        r = await run.run("list_transactions", {"status": "FAILED", "limit": 20})
        cands = [t for t in r.get("transactions", []) if not t["recovered"]]
        if ents.get("amount") is not None:
            cands = [t for t in cands if t["amount"] == ents["amount"]]
        debited = [t for t in cands if t["customer_debited"]]
        if not cands:
            return ask("PAYMENT_NOT_FOUND", base, "No failed payment matches what the merchant said; asked for the UTR or transaction ID")
        # With no ID or amount, act only if the merchant says money was taken AND exactly one payment fits.
        # A bare "a payment failed" is not enough to move money.
        if ents.get("amount") is None and (ents.get("other") or not ents.get("debited") or len(debited) > 1 or (not debited and len(cands) > 1)):
            return ask("PAYMENT_NOT_FOUND", base, "Not enough to pick one payment safely; asked for the UTR or transaction ID")
        txn = sorted(debited or cands, key=lambda t: t["minutes_ago"])[0]

    facts = {**base, "txn_id": txn["txn_id"], "amount": inr(txn["amount"]), "amount_raw": txn["amount"]}
    if txn.get("recovered"):
        return result("RESOLVED_AI", "PAYMENT_RECOVERED", facts, f"{txn['txn_id']} was already recovered")
    if txn["gateway_status"] == "SUCCESS":
        return result("RESOLVED_AI", "PAYMENT_ALREADY_OK", facts, f"{txn['txn_id']} succeeded normally; nothing to recover")
    if not txn["customer_debited"]:
        return result("RESOLVED_AI", "PAYMENT_NOT_DEBITED", facts, f"{txn['txn_id']} failed before debit; no money moved")

    chk = await run.run("recheck_payment_status", {"txn_id": txn["txn_id"]})
    if chk.get("bank_final_status") == "SUCCESS":
        res = await run.run("mark_payment_recovered", {"txn_id": txn["txn_id"]})
        if res.get("blocked") and res.get("requires_approval"):
            return result("ESCALATE", "MONEY_NEEDS_APPROVAL", facts,
                          f"Bank confirmed {txn['txn_id']} ({inr(txn['amount'])}); crediting it needs approval",
                          {"tool": "mark_payment_recovered", "args": {"txn_id": txn["txn_id"]},
                           "label": f"Mark {txn['txn_id']} successful and credit {inr(txn['amount'])} to the merchant"},
                          res.get("reason"))
        return result("RESOLVED_AI", "PAYMENT_RECOVERED", facts,
                      f"Bank confirmed late success; recovered {inr(txn['amount'])} for the merchant")
    return result("RESOLVED_AI", "PAYMENT_AUTO_REVERSAL", facts, f"Bank confirmed failure; {inr(txn['amount'])} auto-reverses to customer")


async def pb_settlement(run: ToolRunner, store: Store, tri: dict, base: dict) -> dict:
    lang = tri["language"]
    ents = tri["entities"]
    r = await run.run("list_settlements", {"limit": 20})
    ss = r.get("settlements", [])
    sid, utr = ents.get("settlement_id"), ents.get("utr")
    if sid or utr:
        target = next((s for s in ss if s["settlement_id"] == sid or (utr and s["utr"] == utr)), None)
        if target is None:
            return ask("ID_NOT_FOUND", base, f"{sid or utr} is not a settlement on this account; asked them to re-check it", ref=sid or utr)
    elif ents.get("amount") is not None:
        target = next((s for s in ss if s["amount"] == ents["amount"]), None)
        if target is None:
            return ask("SETTLEMENT_NOT_FOUND", base, f"No settlement of {inr(ents['amount'])}; asked for the UTR or transaction ID")
    elif ents.get("other") and ss:
        return ask("SETTLEMENT_NOT_FOUND", base, "Merchant means a different settlement; asked which one")
    else:
        target = next((s for s in ss if s["status"] == "ON_HOLD"), None) \
            or next((s for s in ss if s["status"] == "PROCESSING"), None) or (ss[0] if ss else None)
    if target is None:
        doc = index.search("settlement timeline", 1)[0]
        return result("RESOLVED_AI", "FAQ_ANSWER", {**base, "answer": doc["short"][lang]}, "No settlements yet; explained timelines")

    row = store.settlement(target["settlement_id"])
    facts = {**base, "settlement_id": row["id"], "amount": inr(row["amount"]), "bank": row["destination"],
             "bank_name": row["destination"].split(" ••")[0], "utr": row["utr"] or "-"}
    if row["status"] == "SETTLED":
        facts["when"] = ago(row["settled_at"], lang)
        return result("RESOLVED_AI", "SETTLEMENT_ON_TIME", facts, f"{row['id']} already settled, UTR {row['utr']}")
    if row["status"] == "PROCESSING":
        return result("RESOLVED_AI", "SETTLEMENT_PROCESSING", facts, f"{row['id']} is processing, UTR {row['utr']}")

    if row["hold_reason"] in POLICY["retryable_hold_reasons"]:
        res = await run.run("retry_settlement", {"settlement_id": row["id"]})
        if res.get("blocked") and res.get("requires_approval"):
            return result("ESCALATE", "MONEY_NEEDS_APPROVAL", facts, f"Retry of {row['id']} ({inr(row['amount'])}) held for approval",
                          {"tool": "retry_settlement", "args": {"settlement_id": row["id"]},
                           "label": f"Re-send settlement {row['id']} of {inr(row['amount'])} to {row['destination']}"}, res.get("reason"))
        if res.get("blocked"):
            return result("ESCALATE", "LOW_CONFIDENCE", facts, f"Retry of {row['id']} was blocked: {res.get('reason')}",
                          None, res.get("reason"))
        t = await run.run("raise_ticket", {"category": "settlements", "priority": "P2",
                                           "summary": f"Track re-sent settlement {row['id']} ({inr(row['amount'])}), UTR {res['new_utr']}"})
        facts.update(utr=res["new_utr"], ticket_ref=t["ticket_ref"])
        return result("RESOLVED_AI", "SETTLEMENT_RETRIED", facts,
                      f"Re-sent {row['id']} ({inr(row['amount'])}) after bank timeout; new UTR {res['new_utr']}")
    if row["hold_reason"] == "KYC_PENDING":
        k = await run.run("get_kyc_status", {})
        t = await run.run("raise_ticket", {"category": "kyc", "priority": "P2",
                                           "summary": f"Fast-track KYC review; settlement {row['id']} ({inr(row['amount'])}) on hold"})
        facts.update(docs=", ".join(k.get("missing_documents") or ["-"]), ticket_ref=t["ticket_ref"])
        return result("RESOLVED_AI", "SETTLEMENT_KYC_HOLD", facts, f"{row['id']} held for KYC; guided merchant and opened {t['ticket_ref']}")
    return result("ESCALATE", "LOW_CONFIDENCE", facts, f"{row['id']} on hold for {row['hold_reason']}; needs manual review",
                  None, f"Unusual hold reason {row['hold_reason']}")


async def pb_refund(run: ToolRunner, store: Store, tri: dict, base: dict) -> dict:
    ents, lang = tri["entities"], tri["language"]
    rf = None
    if ents.get("refund_id"):
        r = await run.run("get_refund", {"refund_id": ents["refund_id"]})
        if r.get("blocked") or r.get("error"):
            return ask("ID_NOT_FOUND", base, f"{ents['refund_id']} is not a refund on this account; asked them to re-check it", ref=ents["refund_id"])
        rf = r
    else:
        r = await run.run("list_refunds", {"limit": 20})
        cands = r.get("refunds", [])
        ref = ents.get("utr") or ents.get("txn_id")
        if ref:
            t = await run.run("find_transaction", {"utr": ents["utr"]} if ents.get("utr") else {"txn_id": ents["txn_id"]})
            if "txn_id" not in t:
                return ask("ID_NOT_FOUND", base, f"{ref} is not on this merchant's account; asked them to re-check it", ref=ref)
            cands = [x for x in cands if x["txn_id"] == t["txn_id"]]
        elif ents.get("amount") is not None:
            cands = [x for x in cands if x["amount"] == ents["amount"]]
        if not cands or (not ref and ents.get("amount") is None and ents.get("other")):
            return ask("REFUND_NOT_FOUND", base, "No refund matches what the merchant said; asked for the refund ID, UTR or transaction ID")
        failed = [x for x in cands if x["status"] == "FAILED"]
        if len(failed) != 1 and len(cands) > 1 and ents.get("amount") is None and not ref:
            return ask("REFUND_NOT_FOUND", base, f"{len(failed) or len(cands)} refunds could match; asked which one")
        order = {"FAILED": 0, "PROCESSING": 1, "PROCESSED": 2}
        rf = sorted(cands, key=lambda x: order.get(x["status"], 3))[0]

    row = store.refund(rf["refund_id"])
    facts = {**base, "refund_id": row["id"], "amount": inr(row["amount"]), "amount_raw": row["amount"]}
    if row["status"] == "PROCESSED":
        facts["arn"] = row["arn"] or "-"
        return result("RESOLVED_AI", "REFUND_DONE", facts, f"{row['id']} already processed, ARN {row['arn']}")
    if row["status"] == "PROCESSING":
        facts["eta"] = phrase("eta_day", lang)
        return result("RESOLVED_AI", "REFUND_IN_PROGRESS", facts, f"{row['id']} processing within SLA")

    facts["reason"] = reason_text(row["reason"], lang)
    res = await run.run("reinitiate_refund", {"refund_id": row["id"]})
    if res.get("blocked") and res.get("requires_approval"):
        return result("ESCALATE", "REFUND_NEEDS_APPROVAL", facts,
                      f"{row['id']} ({inr(row['amount'])}) failed; re-send held for approval (above {inr(LIMIT)})",
                      {"tool": "reinitiate_refund", "args": {"refund_id": row["id"]},
                       "label": f"Re-send refund {row['id']} of {inr(row['amount'])} to the customer's original payment source"},
                      res.get("reason"))
    if res.get("blocked"):
        return result("ESCALATE", "LOW_CONFIDENCE", facts, f"Re-send of {row['id']} blocked: {res.get('reason')}", None, res.get("reason"))
    facts["ref"] = res["new_reference"]
    return result("RESOLVED_AI", "REFUND_REINITIATED", facts, f"Re-sent {row['id']} ({inr(row['amount'])}), ref {res['new_reference']}")


def target_language(text: str) -> Optional[str]:
    low = text.lower()
    for name, spec in LEXICON["announcement_languages"].items():
        if any(m in low for m in spec["match"]):
            return name
    return None


async def pb_soundbox(run: ToolRunner, store: Store, tri: dict, base: dict, text: str) -> dict:
    """Do remotely only what a connected Soundbox allows; guide the merchant through anything physical."""
    lang = tri["language"]
    toks = tokens(text)
    if {"language", "bhasha", "bhaasha"} & set(toks) or "भाषा" in text:
        target = target_language(text)
        if target:
            r = await run.run("set_announcement_language", {"language": target})
            if not r.get("blocked") and not r.get("error"):
                shown = LEXICON["announcement_languages"][target]["hi"] if lang == "hi" else target
                return result("RESOLVED_AI", "SOUNDBOX_LANGUAGE_SET", {**base, "device_id": r["device_id"], "language": shown},
                              f"Set {r['device_id']} announcements to {target} and played a test")
        r = await run.run("search_help_center", {"query": "soundbox announcement language"})
        doc = next(d for d in index.docs if d["id"] == (r["results"][0]["id"] if r["results"] else "KB-106"))
        return result("RESOLVED_AI", "FAQ_ANSWER", {**base, "answer": doc["short"][lang]}, "Explained how to change announcement language")

    d = await run.run("run_soundbox_diagnostics", {})
    facts = {**base, "device_id": d["device_id"], "volume": d["volume"]}
    issues = d.get("issues", [])
    if "hardware_fault" in issues or "offline_long" in issues:
        problem = phrase("problem_fault", lang) if "hardware_fault" in issues else \
            phrase("problem_offline", lang, hours=round((d.get("last_ping_minutes") or 0) / 60))
        t = await run.run("raise_ticket", {"category": "devices", "priority": "P2", "summary": f"Replace Soundbox {d['device_id']}: {', '.join(issues)}"})
        facts.update(ticket_ref=t["ticket_ref"], problem=problem)
        return result("RESOLVED_AI", "SOUNDBOX_REPLACEMENT", facts, f"Booked replacement for {d['device_id']}")

    joiner = phrase("and", lang)
    issue_txt, fix_txt = [], []
    if "offline" in issues or "weak_signal_no_fallback" in issues:
        r = await run.run("reconnect_device", {})
        if "offline" in issues:
            issue_txt.append(phrase("issue_offline", lang))
        if "weak_signal_no_fallback" in issues:
            issue_txt.append(phrase("issue_signal", lang))
        fix_txt.append(phrase("fix_backup_network" if r.get("switched_to_backup_network") else "fix_reconnect", lang))
    if "volume_zero" in issues:
        # Volume is a physical button. Missed announcements wait until the merchant can hear them.
        note = phrase("fixed_note", lang, fixes=joiner.join(fix_txt)) if fix_txt else ""
        return result("RESOLVED_AI", "SOUNDBOX_VOLUME_GUIDE", {**facts, "fixed_note": note},
                      f"{d['device_id']} muted; guided merchant to press + 3 times" + (f"; {', '.join(issues)}" if fix_txt else ""))
    if "missed_announcements" in issues:
        r = await run.run("resend_missed_announcements", {})
        issue_txt.append(phrase("issue_missed", lang, n=d["missed_announcements"]))
        fix_txt.append(phrase("fix_resend", lang, n=r.get("replayed", d["missed_announcements"])))
    if fix_txt:
        await run.run("play_test_announcement", {})
        facts.update(issues=joiner.join(issue_txt), fixes=joiner.join(fix_txt))
        return result("RESOLVED_AI", "SOUNDBOX_FIXED", facts, f"Fixed {d['device_id']} remotely: {', '.join(issues)}")
    return result("RESOLVED_AI", "SOUNDBOX_OK", facts, f"{d['device_id']} healthy; gave next steps")


def pb_small_talk(tri: dict, base: dict, text: str) -> dict:
    kind = small_talk_kind(text) or tri["entities"].get("small_talk") or "greet"
    return result("RESOLVED_AI", "SMALL_TALK_THANKS" if kind == "thanks" else "SMALL_TALK_GREET", base, "Small talk; answered with quick options")


async def pb_kyc(run: ToolRunner, store: Store, tri: dict, base: dict) -> dict:
    lang = tri["language"]
    k = await run.run("get_kyc_status", {})
    if k["kyc_status"] != "VERIFIED":
        return result("RESOLVED_AI", "KYC_GUIDANCE", {**base, "docs": ", ".join(k["missing_documents"] or ["-"])},
                      f"KYC {k['kyc_status']}; listed missing documents")
    doc = next(d for d in index.docs if d["id"] == "KB-104")
    return result("RESOLVED_AI", "KYC_VERIFIED", {**base, "answer": doc["short"][lang]}, "KYC verified; shared requirements")


async def pb_general(run: ToolRunner, store: Store, tri: dict, base: dict, text: str) -> dict:
    lang = tri["language"]
    r = await run.run("search_help_center", {"query": text})
    hits = r.get("results", [])
    if hits and hits[0]["score"] >= 0.15:
        doc = next(d for d in index.docs if d["id"] == hits[0]["id"])
        return result("RESOLVED_AI", "FAQ_ANSWER", {**base, "answer": doc["short"][lang], "source": doc["id"]},
                      f"Answered from help center {doc['id']}")
    return result("ESCALATE", "LOW_CONFIDENCE", base, "No confident answer in help center", None, "No confident help-center answer")


async def run_playbook(run: ToolRunner, store: Store, tri: dict, base: dict, text: str) -> dict:
    intent = tri["intent"]
    if intent == "FAILED_PAYMENT":
        return await pb_failed_payment(run, store, tri, base)
    if intent == "SETTLEMENT_DELAY":
        return await pb_settlement(run, store, tri, base)
    if intent == "REFUND_ISSUE":
        return await pb_refund(run, store, tri, base)
    if intent == "SOUNDBOX":
        return await pb_soundbox(run, store, tri, base, text)
    if intent == "KYC":
        return await pb_kyc(run, store, tri, base)
    if intent == "SMALL_TALK":
        return pb_small_talk(tri, base, text)
    return await pb_general(run, store, tri, base, text)


# ====================================================================== LLM strategy
PLAYBOOK_HINTS = {
    "FAILED_PAYMENT": "Find the payment with find_transaction (UTR or transaction ID) if given, else list_transactions(FAILED) and filter by amount. "
                      "If no ID is given and more than one payment could match, or the ID is not found, do NOT pick one: finish with outcome needs_info. "
                      "If the payment already succeeded, there is nothing to recover. If debited, call recheck_payment_status. If the bank says SUCCESS, "
                      "call mark_payment_recovered. If FAILED, explain auto-reversal.",
    "SETTLEMENT_DELAY": "list_settlements. If ON_HOLD with BANK_TIMEOUT, call retry_settlement then raise_ticket (category settlements, P2). "
                        "If KYC_PENDING, call get_kyc_status and raise_ticket (category kyc). If SETTLED, give the UTR.",
    "REFUND_ISSUE": "Find the refund by ID or amount. If several could match and none is clearly the failed one, or the ID is not found, do NOT pick one: "
                    "finish with outcome needs_info. PROCESSED -> give ARN. PROCESSING -> give ETA. FAILED -> call reinitiate_refund (held for approval if above limit).",
    "SOUNDBOX": "run_soundbox_diagnostics. Weak signal or offline -> reconnect_device. Missed announcements -> resend_missed_announcements, "
                "then play_test_announcement. Volume 0 cannot be changed remotely: tell the merchant to press + 3 times. "
                "Language change -> set_announcement_language. Hardware fault or offline for hours -> raise_ticket (devices) for a replacement.",
    "KYC": "get_kyc_status and list any missing documents.",
    "GENERAL": "search_help_center and answer from the best result.",
    "UNKNOWN": "Investigate briefly; if still unclear, escalate_to_human.",
}

ACTION_SYSTEM = (
    "You are the action agent of PayMates, an autonomous customer-service teammate for Paytm-style merchants in India. "
    "You resolve the merchant's issue by calling tools, not by guessing.\n"
    "Rules:\n"
    "1. Verify before acting: read the record, check its state, then act.\n"
    f"2. Money movements are guarded. Anything above Rs {LIMIT} will be held for human approval; that is expected, not an error.\n"
    "3. Never invent IDs, amounts or statuses. Use only what tools return.\n"
    "4. If a tool returns blocked, do not try to work around it.\n"
    "5. If the case is unclear after checking, call escalate_to_human with a clear reason.\n"
    "6. Use as few tool calls as needed (usually 2-4).\n"
    "When finished, reply with ONLY a JSON object: "
    "{\"outcome\": \"resolved\" | \"escalate\" | \"needs_info\", \"summary\": one sentence describing what you found and did}."
)


async def run_llm(spec, run: ToolRunner, store: Store, tri: dict, ctx: dict, text: str, base: dict) -> dict:
    brief = {
        "merchant": ctx["merchant"],
        "message": text,
        "triage": {k: tri[k] for k in ("intent", "confidence", "entities", "urgency")},
        "relevant_records": ctx["records"],
        "past_tickets": [p["summary"] for p in ctx["past_tickets"]],
        "help_center": [{"id": d["id"], "title": d["title"], "snippet": d["snippet"]} for d in ctx["kb"]],
        "suggested_playbook": PLAYBOOK_HINTS.get(tri["intent"], PLAYBOOK_HINTS["UNKNOWN"]),
    }
    messages = [{"role": "system", "content": ACTION_SYSTEM},
                {"role": "user", "content": json.dumps(brief, ensure_ascii=False, default=str)}]
    tools = tool_specs()
    final: dict = {}
    for _ in range(7):
        out = await chat(spec, messages, tools=tools, max_tokens=700, agent="action")
        if not out["tool_calls"]:
            try:
                final = parse_json(out["text"])
            except Exception:
                final = {"outcome": "resolved", "summary": out["text"][:240]}
            break
        messages.append({"role": "assistant", "content": out["text"], "tool_calls": out["tool_calls"]})
        for call in out["tool_calls"]:
            res = await run.run(call["name"], call["args"])
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(res, ensure_ascii=False, default=str)})
    summary = str(final.get("summary") or "Handled by the action agent")

    # Guardrail outcomes win over whatever the model concluded.
    if run.blocked:
        b = run.blocked[0]
        return infer_blocked(store, b, base, tri["language"], summary)
    if run.handoff:
        return result("ESCALATE", "LOW_CONFIDENCE", base, summary, None, run.handoff)
    inferred = infer_outcome(store, run.calls, base, tri["language"], summary)
    if inferred:
        return inferred
    if final.get("outcome") == "escalate":
        return result("ESCALATE", "LOW_CONFIDENCE", base, summary, None, summary)
    if final.get("outcome") == "needs_info":
        ask_outcome = {"FAILED_PAYMENT": "PAYMENT_NOT_FOUND", "REFUND_ISSUE": "REFUND_NOT_FOUND", "SETTLEMENT_DELAY": "SETTLEMENT_NOT_FOUND"}.get(tri["intent"])
        return result("NEEDS_INFO", ask_outcome or "GENERIC_RESOLVED", {**base, "summary": summary}, summary)
    return result("RESOLVED_AI", "GENERIC_RESOLVED", {**base, "summary": summary}, summary)


def infer_blocked(store: Store, b: dict, base: dict, lang: str, summary: str) -> dict:
    tool, args = b["tool"], b["args"]
    if tool == "reinitiate_refund":
        row = store.refund(args["refund_id"])
        facts = {**base, "refund_id": row["id"], "amount": inr(row["amount"]), "reason": reason_text(row["reason"], lang)}
        return result("ESCALATE", "REFUND_NEEDS_APPROVAL", facts, summary,
                      {"tool": tool, "args": args, "label": f"Re-send refund {row['id']} of {inr(row['amount'])} to the customer's original payment source"},
                      b["reason"])
    amount = None
    if tool == "mark_payment_recovered":
        amount = store.transaction(args["txn_id"])["amount"]
    elif tool == "retry_settlement":
        amount = store.settlement(args["settlement_id"])["amount"]
    return result("ESCALATE", "MONEY_NEEDS_APPROVAL", {**base, "amount": inr(amount)}, summary,
                  {"tool": tool, "args": args, "label": f"Run {tool} {json.dumps(args)}"}, b["reason"])


def infer_outcome(store: Store, calls: list[dict], base: dict, lang: str, summary: str) -> Optional[dict]:
    """Map what the tools actually did onto a reply template, so replies stay grounded in facts."""
    ok = [c for c in calls if c["guard"]["allowed"] and not c["result"].get("error")]
    by = {c["name"]: c for c in ok}
    if "mark_payment_recovered" in by:
        t = store.transaction(by["mark_payment_recovered"]["args"]["txn_id"])
        return result("RESOLVED_AI", "PAYMENT_RECOVERED", {**base, "txn_id": t["id"], "amount": inr(t["amount"])}, summary)
    if "retry_settlement" in by:
        s = store.settlement(by["retry_settlement"]["args"]["settlement_id"])
        ref = by.get("raise_ticket", {}).get("result", {}).get("ticket_ref", "-")
        return result("RESOLVED_AI", "SETTLEMENT_RETRIED", {**base, "settlement_id": s["id"], "amount": inr(s["amount"]),
                                                            "bank": s["destination"], "bank_name": s["destination"].split(" ••")[0], "utr": s["utr"], "ticket_ref": ref}, summary)
    if "reinitiate_refund" in by:
        r = store.refund(by["reinitiate_refund"]["args"]["refund_id"])
        return result("RESOLVED_AI", "REFUND_REINITIATED", {**base, "refund_id": r["id"], "amount": inr(r["amount"]),
                                                           "reason": "-", "ref": by["reinitiate_refund"]["result"].get("new_reference")}, summary)
    if "set_announcement_language" in by:
        res = by["set_announcement_language"]["result"]
        return result("RESOLVED_AI", "SOUNDBOX_LANGUAGE_SET", {**base, "device_id": res.get("device_id"), "language": res.get("language")}, summary)
    if {"reconnect_device", "resend_missed_announcements"} & set(by):
        return result("RESOLVED_AI", "GENERIC_RESOLVED", {**base, "summary": summary}, summary)
    if "recheck_payment_status" in by:
        chk = by["recheck_payment_status"]["result"]
        if chk.get("bank_final_status") == "FAILED" and chk.get("customer_debited"):
            t = store.transaction(chk["txn_id"])
            return result("RESOLVED_AI", "PAYMENT_AUTO_REVERSAL", {**base, "txn_id": t["id"], "amount": inr(t["amount"])}, summary)
    return None


async def act(store: Store, run: ToolRunner, tri: dict, ctx: dict, text: str, base: dict) -> tuple[dict, str]:
    """Returns (action_result, strategy_label)."""
    spec = model_for("action")
    if spec:
        try:
            return await run_llm(spec, run, store, tri, ctx, text, base), spec.label
        except Exception as exc:
            note = str(exc)[:120]
            # If the model already acted, report what the tools actually did; re-running the
            # playbook would pick the next record and describe the wrong outcome.
            summary = f"Model stopped mid-case ({note}); outcome taken from the tool calls it made"
            done = infer_blocked(store, run.blocked[0], base, tri["language"], summary) if run.blocked \
                else infer_outcome(store, run.calls, base, tri["language"], summary)
            if done:
                done["fallback_note"] = f"LLM error after acting; outcome from tool results: {note}"
                return done, f"{spec.label} (stopped early: outcome from tool results)"
            res = await run_playbook(run, store, tri, base, text)
            res["fallback_note"] = f"LLM error, finished with playbook: {note}"
            return res, f"playbook (fallback from {spec.label})"
    return await run_playbook(run, store, tri, base, text), "playbook-engine"
