"""Orchestrator: runs one merchant message through the agent team.

    triage -> context -> action (guarded tools) -> [council] -> [escalation] -> responder

Every step is streamed as an event so the UI can show the agents working
live. Failures in any model fall back to rules, so a ticket never dies.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from ..config import POLICY, pacing_seconds
from ..guardrails import mask_pii
from ..store import Store, now_ms
from ..tools import ToolRunner
from . import action as action_agent
from .context import gather
from .escalation import build_briefing
from .responder import respond
from .triage import triage as run_triage

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


async def handle_message(store: Store, merchant_id: str, text: str, emit: Emit, source: str = "live",
                         pacing: float | None = None) -> dict:
    merchant = store.merchant(merchant_id)
    if merchant is None:
        raise ValueError(f"unknown merchant {merchant_id}")
    pace = Pacer(pacing_seconds() if pacing is None else pacing)
    started = time.perf_counter()
    ticket_id = store.next_id("ticket", "PM-")
    created = now_ms()
    store.insert("tickets", {"id": ticket_id, "merchant_id": merchant_id, "message": text, "lang": "en", "intent": None,
                             "confidence": None, "status": "OPEN", "outcome": None, "summary": None, "reply": None,
                             "created_at": created, "resolved_at": None, "duration_ms": None, "recovered_amount": 0,
                             "models": {}, "source": source})
    store.add_message(merchant_id, "merchant", text, ticket_id)
    store.audit(ticket_id, "merchant", "message", {"text": mask_pii(text)})
    await emit({"type": "ticket", "ticket": {"id": ticket_id, "merchant_id": merchant_id, "created_at": created, "message": text}})
    models: dict[str, str] = {}

    async def step(agent: str, status: str, title: str, **extra):
        await emit({"type": "step", "agent": agent, "status": status, "title": title, **extra})

    # 1. TRIAGE ------------------------------------------------------------
    await step("triage", "running", "Reading the message")
    t0 = time.perf_counter()
    tri, models["triage"] = await run_triage(mask_pii(text))
    await pace()
    await step("triage", "done", f"{tri['intent'].replace('_', ' ').title()} · {int(tri['confidence'] * 100)}% sure",
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
    elif tri["confidence"] < threshold or tri["intent"] == "UNKNOWN":
        act = action_agent.result("ESCALATE", "LOW_CONFIDENCE", base,
                                  f"Triage confidence {int(tri['confidence'] * 100)}% is below the {int(threshold * 100)}% threshold",
                                  None, f"Not confident enough to act ({int(tri['confidence'] * 100)}%)")
        models["action"] = "policy: confidence threshold"
        await step("action", "blocked", "Not sure enough to act, so I'm escalating instead of guessing", model=models["action"], ms=0,
                   data={"decision": "escalate", "threshold": threshold})
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

    # 4. MODEL COUNCIL ----------------------------------------------------
    if runner.sessions:
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
    store.add_message(merchant_id, "assistant", reply, ticket_id)
    await emit({"type": "reply", "text": reply, "lang": tri["language"], "ticket_id": ticket_id})

    # 8. CLOSE THE TICKET --------------------------------------------------
    status = {"RESOLVED_AI": "RESOLVED_AI", "ESCALATE": "ESCALATED", "NEEDS_INFO": "NEEDS_INFO"}[act["status"]]
    duration = int((time.perf_counter() - started - pace.total) * 1000)
    changes = {"lang": tri["language"], "intent": tri["intent"], "confidence": tri["confidence"], "status": status,
               "outcome": act["outcome"], "summary": act["summary"], "reply": reply, "duration_ms": duration,
               "resolved_at": now_ms() if status == "RESOLVED_AI" else None, "recovered_amount": runner.recovered,
               "models": models}
    store.update("tickets", ticket_id, changes)
    store.audit(ticket_id, "orchestrator", "close", {"status": status, "outcome": act["outcome"], "duration_ms": duration})
    ticket = store.ticket(ticket_id)
    await emit({"type": "done", "ticket": ticket, "escalation": escalation})
    return ticket
