"""PayMates API: FastAPI app with Server-Sent Events for the live agent trace.

Run:  uvicorn app.main:app --reload --port 8000   (from the backend/ folder)
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import council, hisaab, ops, watcher
from .agents.orchestrator import handle_message
from .config import FRONTEND_DIST, POLICY, PROVIDERS, mode, provider_available, routing_table
from .rag import index
from .store import store

app = FastAPI(title="PayMates API", version="1.0.0",
              description="Autonomous AI support teammate for merchants — Paytm Build for India, Track 3")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatIn(BaseModel):
    merchant_id: str
    message: str = Field(min_length=1, max_length=2000)


class DecisionIn(BaseModel):
    specialist: str = "Priya (Support)"
    note: str = ""


class SimIn(BaseModel):
    n: int = Field(default=25, ge=1, le=200)
    seed: Optional[int] = None


def sse(gen_factory):
    """Run a coroutine that emits events, and stream them as Server-Sent Events."""
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict):
        await queue.put(event)

    async def runner():
        try:
            await gen_factory(emit)
        except Exception as exc:  # surface errors to the UI instead of hanging
            await queue.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(runner())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        finally:
            task.cancel() if not task.done() else None

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "engine": "backend",
        "mode": mode(),
        "routing": routing_table(),
        "providers": {name: provider_available(name) for name in PROVIDERS},
        "autonomy_level": store.setting("autonomy_level", POLICY["default_autonomy"]),
        "council": ["auditor:ledger", "auditor:replay", "auditor:velocity"] + [j.label for j in council.jurors()],
        "policy": POLICY,
    }


class PolicyIn(BaseModel):
    autonomy_level: str
    changed_by: str = "Ops"


def _policy() -> dict:
    return {"autonomy_level": store.setting("autonomy_level", POLICY["default_autonomy"]),
            "levels": [{"id": k, **v} for k, v in POLICY["autonomy_levels"].items()]}


@app.get("/api/policy")
def get_policy():
    return _policy()


@app.post("/api/policy")
def set_policy(body: PolicyIn):
    if body.autonomy_level not in POLICY["autonomy_levels"]:
        raise HTTPException(400, f"unknown autonomy level {body.autonomy_level}")
    before = store.setting("autonomy_level", POLICY["default_autonomy"])
    store.set_setting("autonomy_level", body.autonomy_level)
    store.audit(None, f"ops:{body.changed_by}", "set_autonomy", {"from": before, "to": body.autonomy_level})
    return _policy()


@app.get("/api/merchants")
def merchants():
    return [m for m in store.merchants() if m["id"].startswith("M1")]


@app.get("/api/merchants/{mid}/snapshot")
def merchant_snapshot(mid: str):
    m = store.merchant(mid)
    if not m:
        raise HTTPException(404, "merchant not found")
    return {"merchant": m, "transactions": store.transactions(mid, limit=6), "settlements": store.settlements(mid, limit=4),
            "refunds": store.refunds(mid, limit=4)}


@app.post("/api/chat")
async def chat(body: ChatIn):
    if not store.merchant(body.merchant_id):
        raise HTTPException(404, "merchant not found")
    return sse(lambda emit: handle_message(store, body.merchant_id, body.message.strip(), emit))


@app.post("/api/merchants/{mid}/checkin")
async def checkin(mid: str):
    """Proactive check-in: same SSE events as /api/chat, one ticket per problem found."""
    if not store.merchant(mid):
        raise HTTPException(404, "merchant not found")
    return sse(lambda emit: watcher.checkin(store, mid, emit))


@app.post("/api/merchants/{mid}/hisaab")
def merchant_hisaab(mid: str):
    """Today's hisaab for one merchant, sent to their chat."""
    if not store.merchant(mid):
        raise HTTPException(404, "merchant not found")
    return hisaab.send(store, mid)


@app.get("/api/tickets")
def tickets(limit: int = 50):
    return store.tickets(limit=limit)


@app.get("/api/escalations")
def escalations():
    return store.escalations()


@app.post("/api/escalations/{esc_id}/approve")
async def approve(esc_id: str, body: DecisionIn):
    try:
        out = await ops.decide(store, esc_id, True, body.specialist, body.note)
    except KeyError:
        raise HTTPException(404, "escalation not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {**out, "metrics": ops.metrics(store)}


@app.post("/api/escalations/{esc_id}/reject")
async def reject(esc_id: str, body: DecisionIn):
    try:
        out = await ops.decide(store, esc_id, False, body.specialist, body.note)
    except KeyError:
        raise HTTPException(404, "escalation not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {**out, "metrics": ops.metrics(store)}


@app.get("/api/metrics")
def get_metrics():
    return ops.metrics(store)


@app.get("/api/audit")
def audit(limit: int = 80):
    return store.audit_log(limit)


@app.get("/api/kb/search")
def kb_search(q: str):
    return index.search(q, 5)


@app.post("/api/simulate")
async def simulate(body: SimIn):
    return sse(lambda emit: ops.simulate(store, body.n, emit, handle_message, body.seed))


@app.post("/api/reset")
def reset():
    store.reset()
    return {"ok": True, "metrics": ops.metrics(store)}


# ---------------------------------------------------------------- serve the built web app
if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        target = FRONTEND_DIST / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(FRONTEND_DIST / "index.html")
