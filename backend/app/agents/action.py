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

from ..config import POLICY, TEMPLATES, model_for
from ..llm import chat, parse_json
from ..rag import index
from ..store import Store
from ..text import ago, inr, phrase, tokens
from ..tools import ToolRunner, tool_specs

LIMIT = POLICY["auto_money_limit"]


def result(status: str, outcome: str, facts: dict, summary: str, proposed: Optional[dict] = None,
           reason: Optional[str] = None) -> dict:
    if outcome.endswith("_NEEDS_APPROVAL") and (reason or "").startswith("Council held it"):
        outcome = "COUNCIL_HOLD"        # within the money limit, but the Model Council said wait
    return {"status": status, "outcome": outcome, "facts": facts, "summary": summary,
            "proposed_action": proposed, "escalation_reason": reason}


def reason_text(code: Optional[str], lang: str) -> str:
    key = f"reason_{code}"
    if code and key in TEMPLATES["phrases"]:
        return phrase(key, lang)
    return code or ""


# ====================================================================== playbooks
async def pb_failed_payment(run: ToolRunner, store: Store, tri: dict, base: dict) -> dict:
    ents, lang = tri["entities"], tri["language"]
    txn = None
    if ents.get("txn_id"):
        r = await run.run("get_transaction", {"txn_id": ents["txn_id"]})
        if not r.get("blocked") and not r.get("error"):
            txn = r
    if txn is None:
        r = await run.run("list_transactions", {"status": "FAILED"})
        cands = r.get("transactions", [])
        if ents.get("amount"):
            cands = [t for t in cands if t["amount"] == ents["amount"]] or cands
        cands.sort(key=lambda t: (not t["customer_debited"], t["recovered"], t["minutes_ago"]))
        txn = cands[0] if cands else None
    if txn is None:
        return result("NEEDS_INFO", "PAYMENT_NOT_FOUND", base, "No matching failed payment found; asked merchant for details")

    facts = {**base, "txn_id": txn["txn_id"], "amount": inr(txn["amount"]), "amount_raw": txn["amount"]}
    if txn.get("recovered"):
        return result("RESOLVED_AI", "PAYMENT_RECOVERED", facts, f"{txn['txn_id']} was already recovered")
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
    r = await run.run("list_settlements", {})
    ss = r.get("settlements", [])
    sid = tri["entities"].get("settlement_id")
    target = next((s for s in ss if s["settlement_id"] == sid), None) if sid else None
    target = target or next((s for s in ss if s["status"] == "ON_HOLD"), None) \
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
        if not r.get("blocked") and not r.get("error"):
            rf = r
    if rf is None:
        r = await run.run("list_refunds", {})
        cands = r.get("refunds", [])
        if ents.get("amount"):
            cands = [x for x in cands if x["amount"] == ents["amount"]] or cands
        order = {"FAILED": 0, "PROCESSING": 1, "PROCESSED": 2}
        cands.sort(key=lambda x: order.get(x["status"], 3))
        rf = cands[0] if cands else None
    if rf is None:
        return result("NEEDS_INFO", "REFUND_NOT_FOUND", base, "No matching refund found; asked for details")

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


async def pb_soundbox(run: ToolRunner, store: Store, tri: dict, base: dict, text: str) -> dict:
    lang = tri["language"]
    toks = tokens(text)
    if {"language", "bhasha"} & set(toks) or "भाषा" in text:
        r = await run.run("search_help_center", {"query": "soundbox announcement language"})
        doc = next(d for d in index.docs if d["id"] == (r["results"][0]["id"] if r["results"] else "KB-106"))
        return result("RESOLVED_AI", "FAQ_ANSWER", {**base, "answer": doc["short"][lang]}, "Explained how to change announcement language")

    d = await run.run("run_soundbox_diagnostics", {})
    facts = {**base, "device_id": d["device_id"], "volume": d["volume"]}
    issues = d.get("issues", [])
    if "hardware_fault" in issues or "offline" in issues:
        t = await run.run("raise_ticket", {"category": "devices", "priority": "P2", "summary": f"Replace Soundbox {d['device_id']}: {', '.join(issues)}"})
        facts["ticket_ref"] = t["ticket_ref"]
        return result("RESOLVED_AI", "SOUNDBOX_REPLACEMENT", facts, f"Booked replacement for {d['device_id']}")
    fix_args, issue_txt, fix_txt = {}, [], []
    if "volume_zero" in issues:
        fix_args["volume"] = 80
        issue_txt.append(phrase("issue_volume", lang))
        fix_txt.append(phrase("fix_volume", lang))
    if "weak_signal_no_fallback" in issues:
        fix_args["network_fallback"] = True
        issue_txt.append(phrase("issue_signal", lang))
        fix_txt.append(phrase("fix_fallback", lang))
    if fix_args:
        await run.run("update_soundbox_settings", fix_args)
        joiner = phrase("and", lang)
        facts.update(issues=joiner.join(issue_txt), fixes=joiner.join(fix_txt))
        return result("RESOLVED_AI", "SOUNDBOX_FIXED", facts, f"Fixed {d['device_id']} remotely: {', '.join(issues)}")
    return result("RESOLVED_AI", "SOUNDBOX_OK", facts, f"{d['device_id']} healthy; gave next steps")


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
    return await pb_general(run, store, tri, base, text)


# ====================================================================== LLM strategy
PLAYBOOK_HINTS = {
    "FAILED_PAYMENT": "Find the failed payment (by ID, else amount, else most recent where the customer was debited). If debited, "
                      "call recheck_payment_status. If the bank says SUCCESS, call mark_payment_recovered. If FAILED, explain auto-reversal.",
    "SETTLEMENT_DELAY": "list_settlements. If ON_HOLD with BANK_TIMEOUT, call retry_settlement then raise_ticket (category settlements, P2). "
                        "If KYC_PENDING, call get_kyc_status and raise_ticket (category kyc). If SETTLED, give the UTR.",
    "REFUND_ISSUE": "Find the refund. PROCESSED -> give ARN. PROCESSING -> give ETA. FAILED -> call reinitiate_refund (held for approval if above limit).",
    "SOUNDBOX": "run_soundbox_diagnostics. Fix volume_zero (set 80) and weak_signal_no_fallback (network_fallback true) with update_soundbox_settings. "
                "Hardware fault or offline -> raise_ticket (devices).",
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
        out = await chat(spec, messages, tools=tools, max_tokens=700)
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
        return result("NEEDS_INFO", "GENERIC_RESOLVED", {**base, "summary": summary}, summary)
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
    if "update_soundbox_settings" in by:
        res = by["update_soundbox_settings"]["result"]
        return result("RESOLVED_AI", "GENERIC_RESOLVED", {**base, "summary": summary, "device_id": res.get("device_id")}, summary)
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
            # Tools are idempotent, so the playbook can safely pick up where the model stopped.
            res = await run_playbook(run, store, tri, base, text)
            res["fallback_note"] = f"LLM error, finished with playbook: {str(exc)[:120]}"
            return res, f"playbook (fallback from {spec.label})"
    return await run_playbook(run, store, tri, base, text), "playbook-engine"
