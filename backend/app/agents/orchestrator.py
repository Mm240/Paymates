"""Orchestrator: runs one merchant message through the agent team.

    [watcher] -> triage -> context -> action (guarded tools) -> [council] -> [escalation] -> responder

A ticket can start from the merchant's message, or from the watcher (a
proactive check-in found a problem in the records). Watcher tickets skip
model triage, because the record already says what the issue is, and open
the reply with an outbound "I noticed..." in the merchant's language.

Every step is streamed as an event so the UI can show the agents working
live. Failures in any model fall back to rules, so a ticket never dies.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Awaitable, Callable

from .. import cards
from ..llm import METER, Meter
from ..config import POLICY, pacing_seconds
from ..guardrails import mask_pii
from ..store import Store, now_ms
from ..text import detect_language, extract_entities, render, small_talk_kind
from ..tools import ToolRunner
from . import action as action_agent
from .context import gather
from .escalation import build_briefing
from .responder import respond
from .triage import triage as run_triage, with_raw_entities

Emit = Callable[[dict], Awaitable[None]]


class Pacer:
    """Adds a visible pause between steps on stage, and keeps it out of measured time."""

    def __init__(self, seconds: float):
        self.seconds, self.total, self.last = seconds, 0.0, 0.0

    async def __call__(self, factor: float = 1.0):
        self.last = 0.0
        if self.seconds > 0:
            d = self.seconds * factor
            self.total += d
            self.last = d
            await asyncio.sleep(d)


# Outcomes that wait on the merchant. The merchant's next message continues the SAME ticket.
ASK_REFERENCE = {"PAYMENT_NOT_FOUND", "REFUND_NOT_FOUND", "SETTLEMENT_NOT_FOUND", "ID_NOT_FOUND"}
ID_KEYS = ("utr", "txn_id", "refund_id", "settlement_id")
CLARIFY_TURNS = 2        # unclear turns before a human is brought in
REFERENCE_TURNS = 3      # tries at naming the record before a human is brought in


def followup_language(text: str, prior: str) -> str:
    """An answer that is only an ID or a number carries no language of its own: keep the language the merchant was using."""
    words = [w for w in text.split() if not re.search(r"\d", w)]
    return prior if len(words) <= 2 else detect_language(text)


async def handle_message(store: Store, merchant_id: str, text: str, emit: Emit, source: str = "live",
                         pacing: float | None = None, watch: dict | None = None, remember: bool = True) -> dict:
    merchant = store.merchant(merchant_id)
    if merchant is None:
        raise ValueError(f"unknown merchant {merchant_id}")
    pace = Pacer(pacing_seconds() if pacing is None else pacing)
    started = time.perf_counter()

    # Conversation memory: is this message the answer to a question PayMates asked?
    raw_ents = {} if watch else extract_entities(text)
    pend = store.pending(merchant_id) if (remember and not watch) else None
    resume = None
    if pend and not small_talk_kind(text):
        answers = any(raw_ents.get(k) for k in ID_KEYS) or raw_ents.get("amount") is not None
        if pend["kind"] == "clarify" or (pend["kind"] == "reference" and answers):
            resume = pend

    if resume:
        ticket_id = resume["ticket_id"]
        created = store.ticket(ticket_id)["created_at"]
        store.update("tickets", ticket_id, {"status": "OPEN", "outcome": None, "summary": None, "reply": None, "resolved_at": None})
    else:
        ticket_id = store.next_id("ticket", "PM-")
        created = now_ms()
        store.insert("tickets", {"id": ticket_id, "merchant_id": merchant_id, "message": text, "lang": "en", "intent": None,
                                 "confidence": None, "status": "OPEN", "outcome": None, "summary": None, "reply": None,
                                 "created_at": created, "resolved_at": None, "duration_ms": None, "recovered_amount": 0,
                                 "models": {}, "source": source, "subject": watch["subject"] if watch else None, "facts": {},
                                 "ai_cost_inr": 0, "llm_calls": []})
    if watch:
        store.audit(ticket_id, "watcher", "noticed", {"kind": watch["kind"], "subject": watch["subject"], "title": watch["title"]})
    else:
        store.add_message(merchant_id, "merchant", text, ticket_id)
        store.audit(ticket_id, "merchant", "message", {"text": mask_pii(text)})
    await emit({"type": "ticket", "ticket": {"id": ticket_id, "merchant_id": merchant_id, "created_at": created, "message": text,
                                             "source": source, "resumed": bool(resume)}})
    models: dict[str, str] = {}
    meter = Meter()                      # every model call this ticket makes, with tokens and ₹
    METER.set(meter)

    async def step(agent: str, status: str, title: str, **extra):
        await emit({"type": "step", "agent": agent, "status": status, "title": title, **extra})

    # 0. WATCHER (proactive tickets only) -----------------------------------
    if watch:
        await step("watcher", "running", "Scanning records")
        await pace(0.6)
        models["watcher"] = "watcher: record scan"
        await step("watcher", "done", watch["title"], model=models["watcher"], ms=0,
                   data={"kind": watch["kind"], "subject": watch["subject"]})
    else:
        await step("watcher", "skipped", "Merchant wrote in first")

    # 1. TRIAGE ------------------------------------------------------------
    await step("triage", "running", "Reading the message" if not watch else "Classifying what the watcher found")

    t0 = time.perf_counter()
    if watch:
        # The record says what the issue is; no model needed. Reply in the merchant's own language.
        tri = {"intent": watch["intent"], "confidence": 1.0, "entities": watch["entities"], "path": "code",
               "urgency": "high" if watch["kind"] != "soundbox" else "medium",
               "language": merchant["lang"], "reasoning": f"Raised by the watcher from {watch['subject']}"}
        models["triage"] = "watcher: from records"
    elif resume and resume["kind"] == "reference":
        # They answered the question we asked: same issue, now with the reference. No need to classify again.
        tri = {"intent": resume["intent"], "confidence": 1.0, "entities": {**resume["entities"], **raw_ents}, "urgency": "high", "path": "code",
               "language": followup_language(text, resume["language"]),
               "reasoning": f"Continuing the earlier question about {resume['intent'].replace('_', ' ').lower()}"}
        models["triage"] = "memory: pending question"
    else:
        tri, models["triage"] = await run_triage(mask_pii(text))
        tri = with_raw_entities(tri, text)          # IDs and amounts come from the raw text; masking hides long numbers
        if tri["intent"] == "SMALL_TALK":
            tri["language"] = merchant["lang"]       # "hi" carries no language: greet in the merchant's own
    await pace()
    await step("triage", "done", ("Continuing " + ticket_id + " · " if resume and resume["kind"] == "reference" else "")
               + f"{tri['intent'].replace('_', ' ').title()} · {int(tri['confidence'] * 100)}% sure",
               model=models["triage"], ms=max(0, int((time.perf_counter() - t0 - pace.last) * 1000)), data=tri)

    first = merchant["owner"].split()[0]
    base = {"owner_first": first, "ticket_id": ticket_id}
    runner = ToolRunner(store, merchant_id, ticket_id, emit)

    # 2. CONTEXT -----------------------------------------------------------
    await step("context", "running", "Pulling account history and help-center docs")
    t0 = time.perf_counter()
    ctx = gather(store, merchant_id, tri, text)
    models["context"] = "retrieval: SQL + TF-IDF vectors"
    await pace()
    await step("context", "done", f"{len(ctx['highlights'])} facts · {len(ctx['kb'])} help docs",
               model=models["context"], ms=max(0, int((time.perf_counter() - t0 - pace.last) * 1000)),
               data={"highlights": ctx["highlights"], "kb": [{"id": d["id"], "title": d["title"], "score": d["score"]} for d in ctx["kb"]],
                     "merchant": ctx["merchant"]})

    # 3. DECIDE + ACT ------------------------------------------------------
    threshold = POLICY["confidence_threshold"]
    if tri["intent"] == "HUMAN_REQUEST":
        act = action_agent.result("ESCALATE", "HUMAN_HANDOFF", base, "Merchant asked for a person", None, "Merchant asked for a person")
        models["action"] = "policy: honour human request"
        await step("action", "done", "Merchant asked for a person, so I'm handing over", model=models["action"], ms=0,
                   data={"decision": "handoff"})
    elif tri["intent"] == "SMALL_TALK":
        act = action_agent.result("RESOLVED_AI", "SMALL_TALK_THANKS" if tri["entities"].get("small_talk") == "thanks" else "SMALL_TALK_GREET",
                                  base, "Small talk; answered with quick options")
        models["action"] = "policy: small talk is never escalated"
        await step("action", "done", "Just a greeting, so I'm answering and offering options", model=models["action"], ms=0,
                   data={"decision": "small_talk"})
    elif tri["confidence"] < threshold or tri["intent"] == "UNKNOWN":
        if resume and resume["kind"] == "clarify":
            # Second unclear turn in a row: stop asking and bring in a person.
            act = action_agent.result("ESCALATE", "LOW_CONFIDENCE", base, f"Still unclear after {CLARIFY_TURNS} turns",
                                      None, f"Still unclear after {CLARIFY_TURNS} turns")
            models["action"] = "policy: unclear twice"
            await step("action", "blocked", "Still not clear after asking, so I'm handing over to a person", model=models["action"], ms=0,
                       data={"decision": "escalate", "threshold": threshold})
        else:
            act = action_agent.result("NEEDS_INFO", "CLARIFY", base, "Message unclear; asked one clarifying question")
            models["action"] = "policy: ask before guessing"
            await step("action", "done", "Not sure what this is about, so I'm asking one question instead of guessing", model=models["action"], ms=0,
                       data={"decision": "clarify", "threshold": threshold})
    else:
        await step("action", "running", "Deciding and acting")
        t0 = time.perf_counter()
        act, models["action"] = await action_agent.act(store, runner, tri, ctx, text, base)
        await pace()
        held = len(runner.blocked)
        writes = sum(1 for c in runner.calls if c["kind"] == "write" and c["guard"]["allowed"])
        await step("action", "done" if act["status"] != "ESCALATE" else "blocked",
                   f"{len(runner.calls)} tool calls · {writes} action(s) taken" + (f" · {held} held for approval" if held else ""),
                   model=models["action"], ms=max(0, int((time.perf_counter() - t0 - pace.last) * 1000)),
                   data={"summary": act["summary"], "outcome": act["outcome"], "fallback": act.get("fallback_note")})

    if act["outcome"] in ASK_REFERENCE and resume and resume["kind"] == "reference" and resume.get("turns", 1) >= REFERENCE_TURNS - 1:
        act = action_agent.result("ESCALATE", "LOW_CONFIDENCE", base, f"Could not identify the record after {REFERENCE_TURNS} tries", None,
                                  f"Could not identify the record after {REFERENCE_TURNS} tries")

    # 4. MODEL COUNCIL ----------------------------------------------------
    if runner.sessions:
        models["council"] = f"3 auditors + {runner.sessions[-1]['jurors']} model juror(s)"
        seats = runner.sessions[-1]["votes"]
        held = [x for x in runner.sessions if not x["approved"]]
        await step("council", "blocked" if held else "done",
                   f"{len(runner.sessions)} money move(s) reviewed · {len(seats)} seats · "
                   + ("held for a human" if held else "approved"),
                   model=f"3 auditors + {runner.sessions[-1]['jurors']} model juror" + ("" if runner.sessions[-1]["jurors"] == 1 else "s"), ms=sum(x["ms"] for x in runner.sessions),
                   data={"sessions": runner.sessions})
    else:
        await step("council", "skipped", "No autonomous money move to review", data={"sessions": []})

    # 5. GUARDRAILS summary ------------------------------------------------
    checked = len(runner.calls)
    await step("guardrails", "done" if not runner.blocked else "blocked",
               f"{checked} call(s) checked · {sum(1 for c in runner.calls if c['guard']['allowed'])} allowed · {len(runner.blocked)} held",
               model="policy engine", ms=0,
               data={"rules": sorted({c["guard"]["rule"] for c in runner.calls}), "limit": POLICY["auto_money_limit"],
                     "held": runner.blocked})

    # 6. ESCALATION --------------------------------------------------------
    escalation = None
    if act["status"] == "ESCALATE":
        await step("escalation", "running", "Writing a briefing for a human specialist")
        t0 = time.perf_counter()
        briefing, models["escalation"] = await build_briefing(store, merchant, ticket_id, text, tri, act, runner.calls)
        esc_id = store.next_id("esc", "ESC-")
        escalation = {"id": esc_id, "ticket_id": ticket_id, "merchant_id": merchant_id, "reason": act.get("escalation_reason") or "",
                      "briefing": briefing, "proposed_action": act.get("proposed_action"), "status": "OPEN",
                      "created_at": now_ms(), "decided_at": None, "decided_by": None, "note": None}
        store.insert("escalations", escalation)
        store.audit(ticket_id, "escalation-agent", "escalate", {"escalation": esc_id, "reason": escalation["reason"]})
        await pace()
        await step("escalation", "done", f"Handed to a specialist as {esc_id}", model=models["escalation"],
                   ms=max(0, int((time.perf_counter() - t0 - pace.last) * 1000)), data={"escalation": escalation})

    # 7. RESPOND -----------------------------------------------------------
    await step("responder", "running", "Writing the reply")
    t0 = time.perf_counter()
    reply, models["responder"], grounded_ok = await respond(act["outcome"], act["facts"], tri["language"], text)
    await pace(0.6)
    await step("responder", "done", f"Reply in {dict(en='English', hi='Hindi', hinglish='Hinglish')[tri['language']]}",
               model=models["responder"], ms=max(0, int((time.perf_counter() - t0 - pace.last) * 1000)), data={"grounded": grounded_ok})
    if watch:
        intro = render(watch["intro"], tri["language"], {**base, **watch["facts"]})
        reply = f"{intro}\n\n{reply}"
    store.add_message(merchant_id, "assistant", reply, ticket_id)
    held = [x for x in runner.sessions if not x["approved"]]
    card = cards.build(act["outcome"], act["facts"], tri["language"], store, merchant_id,
                       {"escalation_id": escalation["id"] if escalation else None, "votes": held[-1]["votes"] if held else None})
    await emit({"type": "reply", "text": reply, "lang": tri["language"], "ticket_id": ticket_id, "card": card})

    # 8. CLOSE THE TICKET --------------------------------------------------
    status = {"RESOLVED_AI": "RESOLVED_AI", "ESCALATE": "ESCALATED", "NEEDS_INFO": "NEEDS_INFO"}[act["status"]]
    duration = int((time.perf_counter() - started - pace.total) * 1000)
    changes = {"lang": tri["language"], "intent": tri["intent"], "confidence": tri["confidence"], "status": status,
               "outcome": act["outcome"], "summary": act["summary"], "reply": reply, "duration_ms": duration,
               "resolved_at": now_ms() if status == "RESOLVED_AI" else None, "recovered_amount": runner.recovered,
               "models": models, "card": card, "facts": {k: v for k, v in act["facts"].items() if isinstance(v, (str, int, float))}}
    # AI cost: model calls only. Rules-engine, retrieval, guardrail and template steps cost ₹0. A resumed ticket keeps what it already cost.
    prior = store.ticket(ticket_id) if resume else None
    changes["ai_cost_inr"] = round((prior["ai_cost_inr"] or 0 if prior else 0) + meter.cost_inr, 6)
    changes["llm_calls"] = (prior["llm_calls"] or [] if prior else []) + meter.calls
    store.update("tickets", ticket_id, changes)
    if remember and not watch:
        if act["outcome"] in ASK_REFERENCE:
            keep = {k: v for k, v in tri["entities"].items() if k == "amount" or (k in ID_KEYS and act["outcome"] != "ID_NOT_FOUND")}
            store.set_pending(merchant_id, {"kind": "reference", "intent": tri["intent"], "entities": keep, "language": tri["language"],
                                            "ticket_id": ticket_id, "turns": (resume["turns"] + 1) if resume and resume["kind"] == "reference" else 1})
        elif act["outcome"] == "CLARIFY":
            store.set_pending(merchant_id, {"kind": "clarify", "intent": None, "entities": {}, "language": tri["language"],
                                            "ticket_id": ticket_id, "turns": 1})
        elif not act["outcome"].startswith("SMALL_TALK"):
            store.set_pending(merchant_id, None)       # the case moved on; small talk keeps the open question open
    store.audit(ticket_id, "orchestrator", "close", {"status": status, "outcome": act["outcome"], "duration_ms": duration})
    ticket = store.ticket(ticket_id)
    await emit({"type": "done", "ticket": ticket, "escalation": escalation, "card": card})
    return ticket
