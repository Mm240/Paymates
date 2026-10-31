"""End-to-end tests for the demo scenarios, guardrails and fallbacks (rules engine, no API keys needed)."""
import asyncio
import os

os.environ["PAYMATES_MODE"] = "offline"

import pytest  # noqa: E402

from app import ops  # noqa: E402
from app.agents import orchestrator  # noqa: E402
from app.config import ModelSpec  # noqa: E402
from app.guardrails import check, mask_pii  # noqa: E402
from app.store import Store  # noqa: E402
from app.text import detect_language, inr  # noqa: E402


def run(store, mid, text):
    events = []

    async def emit(e):
        events.append(e)

    ticket = asyncio.run(orchestrator.handle_message(store, mid, text, emit, pacing=0))
    return ticket, events


@pytest.fixture
def store():
    return Store(":memory:")


def tools_called(events):
    return [e["name"] for e in events if e["type"] == "tool"]


def test_demo_1_failed_payment_recovered(store):
    t, ev = run(store, "M1001", "Customer's payment failed but money was deducted.")
    assert t["status"] == "RESOLVED_AI" and t["outcome"] == "PAYMENT_RECOVERED"
    assert t["recovered_amount"] == 1250
    assert tools_called(ev)[-2:] == ["recheck_payment_status", "mark_payment_recovered"]
    assert store.transaction("T88120045")["gateway_status"] == "SUCCESS"
    assert "₹1,250" in t["reply"]


def test_demo_2_settlement_hinglish(store):
    t, ev = run(store, "M1001", "Kal ka settlement nahi aaya")
    assert t["lang"] == "hinglish"
    assert t["outcome"] == "SETTLEMENT_RETRIED"
    assert store.settlement("S2610300")["status"] == "PROCESSING"
    assert "ghante" in t["reply"] and "₹18,640" in t["reply"]


def test_demo_3_refund_escalates_then_human_approves(store):
    t, ev = run(store, "M1002", "Customer says refund not received.")
    assert t["status"] == "ESCALATED" and t["outcome"] == "REFUND_NEEDS_APPROVAL"
    esc = store.escalations()[0]
    assert esc["proposed_action"]["tool"] == "reinitiate_refund"
    assert esc["proposed_action"]["args"]["refund_id"] == "R77812"
    assert any("above" in r for r in esc["briefing"]["risk"])
    assert store.refund("R77812")["status"] == "FAILED"          # nothing moved without approval
    out = asyncio.run(ops.decide(store, esc["id"], True, "Priya"))
    assert out["ticket"]["status"] == "RESOLVED_HUMAN"
    assert store.refund("R77812")["status"] == "PROCESSING"
    assert "Priya" in out["followup"]["text"]


def test_hindi_soundbox_muted_is_guided_not_faked(store):
    t, ev = run(store, "M1003", "मेरा साउंडबॉक्स भुगतान बोल नहीं रहा")
    assert t["lang"] == "hi" and t["outcome"] == "SOUNDBOX_VOLUME_GUIDE"
    sb = store.merchant("M1003")["soundbox"]
    assert sb["volume"] == 0                                         # volume is a physical button; never "fixed" remotely
    assert sb["network_fallback"] is True and "backup" in sb["signal"]  # weak signal: moved to the backup network
    assert "+ बटन 3 बार" in t["reply"]
    assert "update_soundbox_settings" not in tools_called(ev)


def test_kyc_hold_settlement(store):
    t, _ = run(store, "M1003", "कल का सेटलमेंट अभी तक बैंक में नहीं आया")
    assert t["outcome"] == "SETTLEMENT_KYC_HOLD"
    assert "GST" in t["reply"]


def test_human_request_and_low_confidence(store):
    t, _ = run(store, "M1002", "I want to talk to a real person about a customer dispute.")
    assert t["status"] == "ESCALATED" and t["outcome"] == "HUMAN_HANDOFF"
    t2, _ = run(store, "M1002", "Something is off with my account, can you look into it?")
    assert t2["status"] == "ESCALATED" and t2["outcome"] == "LOW_CONFIDENCE"


def test_faq_answer(store):
    t, _ = run(store, "M1001", "Statement kaise download karein?")
    assert t["outcome"] == "FAQ_ANSWER" and t["status"] == "RESOLVED_AI"


def test_guardrail_merchant_scope(store):
    v = check("reinitiate_refund", {"refund_id": "R77812"}, store, "M1001")
    assert not v["allowed"] and v["rule"] == "merchant-scope"


def test_guardrail_evidence_required(store):
    v = check("mark_payment_recovered", {"txn_id": "T88120045"}, store, "M1001")
    assert not v["allowed"] and v["rule"] == "evidence-required"


def test_pii_masking():
    s = mask_pii("call 9876543221 or pay ramesh.k@okaxis, acct 123456789012")
    assert "9876543221" not in s and "ramesh.k" not in s and "123456789012" not in s


def test_language_and_money_format():
    assert detect_language("Kal ka settlement nahi aaya") == "hinglish"
    assert detect_language("Customer says refund not received.") == "en"
    assert detect_language("मेरा साउंडबॉक्स") == "hi"
    assert inr(124500) == "₹1,24,500" and inr(18640) == "₹18,640"


def test_simulation_keeps_demo_merchants_clean(store):
    events = []

    async def emit(e):
        events.append(e)

    m = asyncio.run(ops.simulate(store, 30, emit, orchestrator.handle_message, seed=7))
    assert m["total"] == 30 and 0.5 < m["ai_resolution_rate"] < 1
    assert store.settlement("S2610300")["status"] == "ON_HOLD"
    assert store.refund("R77812")["status"] == "FAILED"
    t, _ = run(store, "M1001", "Customer's payment failed but money was deducted.")
    assert t["outcome"] == "PAYMENT_RECOVERED"


def test_llm_failure_falls_back_to_rules(store, monkeypatch):
    dead = ModelSpec("openai", "gpt-test", "openai", "http://127.0.0.1:9", "sk-test")
    for mod in ("app.agents.triage", "app.agents.action", "app.agents.responder", "app.agents.escalation"):
        monkeypatch.setattr(f"{mod}.model_for", lambda agent: dead)
    t, ev = run(store, "M1001", "Kal ka settlement nahi aaya")
    assert t["outcome"] == "SETTLEMENT_RETRIED"
    assert "fallback" in t["models"]["triage"] and "fallback" in t["models"]["action"]


# ---------------------------------------------------------------- Model Council
def test_council_reviews_every_autonomous_money_move(store):
    t, ev = run(store, "M1001", "Customer's payment failed but money was deducted.")
    sessions = [e for e in ev if e["type"] == "step" and e["agent"] == "council" and e["status"] == "done"]
    assert sessions, "council step should run when money moves"
    votes = sessions[0]["data"]["sessions"][0]["votes"]
    assert {v["who"] for v in votes} == {"ledger", "replay", "velocity"}
    assert all(v["vote"] == "APPROVE" for v in votes)
    assert t["status"] == "RESOLVED_AI"


def test_council_skipped_when_no_money_moves(store):
    _, ev = run(store, "M1003", "KYC ke liye kaunse documents chahiye?")
    council = [e for e in ev if e["type"] == "step" and e["agent"] == "council"]
    assert council[-1]["status"] == "skipped"


def test_council_replay_auditor_blocks_double_payout(store):
    from app import council
    run(store, "M1001", "Customer's payment failed but money was deducted.")
    s = asyncio.run(council.review(store, "M1001", "mark_payment_recovered", {"txn_id": "T88120045"}, "within limit"))
    assert not s["approved"]
    assert next(v for v in s["votes"] if v["who"] == "replay")["vote"] == "HOLD"


def test_council_velocity_cap_holds_and_escalates(store, monkeypatch):
    from app import council
    monkeypatch.setattr(council, "DAILY_CAP", 1000)
    t, ev = run(store, "M1001", "Customer's payment failed but money was deducted.")
    assert t["status"] == "ESCALATED"
    assert store.transaction("T88120045")["gateway_status"] == "FAILED"     # nothing moved
    held = [e for e in ev if e["type"] == "tool" and e["name"] == "mark_payment_recovered"][0]
    assert held["guard"]["rule"] == "model-council"


def test_council_model_jurors_vote_and_abstain(store, monkeypatch):
    from app import council

    async def fake_chat(spec, messages, **kw):
        if spec.provider == "groq":
            raise RuntimeError("timeout")
        return {"text": '{"vote": "APPROVE", "reason": "bank confirmed"}', "tool_calls": []}

    monkeypatch.setattr(council, "chat", fake_chat)
    monkeypatch.setattr(council, "jurors", lambda: [ModelSpec(p, "m", "openai", "", "k") for p in ("anthropic", "openai", "gemini", "groq")])
    store.update("transactions", "T88120045", {"bank_checked": 1})
    s = asyncio.run(council.review(store, "M1001", "mark_payment_recovered", {"txn_id": "T88120045"}, "ok"))
    by = {v["seat"]: v["vote"] for v in s["votes"]}
    assert by["Groq"] == "ABSTAIN" and by["Anthropic"] == "APPROVE" and s["approved"]


def test_demo_4_council_vetoes_kyc_pending_credit(store):
    t, ev = run(store, "M1003", "ग्राहक के पैसे कट गए पर पेमेंट फेल दिखा रहा है")
    assert t["lang"] == "hi"
    assert t["status"] == "ESCALATED" and t["outcome"] == "COUNCIL_HOLD"
    assert "₹5,000" not in t["reply"]                                     # it was within the limit; don't claim otherwise
    council = [e for e in ev if e["type"] == "step" and e["agent"] == "council" and e["status"] != "running"][-1]
    assert council["status"] == "blocked"
    velocity = next(v for v in council["data"]["sessions"][0]["votes"] if v["who"] == "velocity")
    assert velocity["vote"] == "HOLD" and "KYC" in velocity["reason"]
    assert store.transaction("T88340266")["gateway_status"] == "FAILED"    # guardrail would have allowed ₹3,400; council held it
    esc = store.escalations()[0]
    assert esc["proposed_action"]["tool"] == "mark_payment_recovered"
    asyncio.run(ops.decide(store, esc["id"], True, "Priya"))                  # a human approval overrides the council
    assert store.transaction("T88340266")["gateway_status"] == "SUCCESS"


# ---------------------------------------------------------------- proactive check-in
def checkin(store, mid):
    from app import watcher
    events = []

    async def emit(e):
        events.append(e)

    return asyncio.run(watcher.checkin(store, mid, emit, pacing=0)), events


def test_checkin_m1003_fixes_before_the_merchant_asks(store):
    out, ev = checkin(store, "M1003")
    by = {t["subject"]: t for t in out["tickets"]}
    assert all(t["source"] == "proactive" for t in out["tickets"])
    # ₹860 failed-but-charged payment recovered without anyone asking
    assert by["T88340299"]["status"] == "RESOLVED_AI" and by["T88340299"]["recovered_amount"] == 860
    assert store.transaction("T88340299")["gateway_status"] == "SUCCESS"
    # KYC-held settlement: guided, not force-paid
    assert by["S2610302"]["outcome"] == "SETTLEMENT_KYC_HOLD"
    assert store.settlement("S2610302")["status"] == "ON_HOLD"
    # Soundbox issue handled
    assert by["SB-41890"]["intent"] == "SOUNDBOX" and by["SB-41890"]["status"] == "RESOLVED_AI"
    # Outbound message comes first, in the merchant's language (Hindi)
    assert by["T88340299"]["reply"].startswith("Anil जी, PayMates")
    # Watcher is the first step
    steps = [e for e in ev if e["type"] == "step"]
    assert steps[0]["agent"] == "watcher"


def test_checkin_twice_creates_nothing_new(store):
    first, _ = checkin(store, "M1003")
    n_tickets = len(store.tickets(limit=1000))
    n_esc = len(store.escalations())
    second, _ = checkin(store, "M1003")
    assert first["found"] > 0 and second["found"] == 0
    assert len(store.tickets(limit=1000)) == n_tickets and len(store.escalations()) == n_esc
    assert second["message"]                                             # "all clear" message instead


def test_checkin_big_refund_still_needs_a_human(store):
    out, _ = checkin(store, "M1002")
    refund = next(t for t in out["tickets"] if t["subject"] == "R77812")
    assert refund["status"] == "ESCALATED" and refund["outcome"] == "REFUND_NEEDS_APPROVAL"
    assert store.refund("R77812")["status"] == "FAILED"


def test_checkin_skips_records_already_handled_in_chat(store):
    run(store, "M1002", "Customer says refund not received.")             # Demo 3 opens an escalation for R77812
    out, _ = checkin(store, "M1002")
    assert "R77812" not in {t["subject"] for t in out["tickets"]}


def test_proactive_metric_counts_fixes(store):
    checkin(store, "M1003")
    assert ops.metrics(store)["proactive_fixed"] >= 2


# ---------------------------------------------------------------- realistic Soundbox
def test_soundbox_diagnostics_report_telemetry(store):
    from app.tools import run_soundbox_diagnostics
    d = run_soundbox_diagnostics(store, "M1003", {})
    assert {"battery", "last_ping_minutes", "missed_announcements"} <= set(d)
    assert "missed_announcements" in d["issues"] and "volume_zero" in d["issues"]


def test_soundbox_missed_announcements_resent(store):
    sb = {**store.merchant("M1001")["soundbox"], "missed_announcements": 2}
    store.update("merchants", "M1001", {"soundbox": sb})
    t, ev = run(store, "M1001", "Soundbox ki awaaz nahi aa rahi, payment announce nahi hua")
    assert t["outcome"] == "SOUNDBOX_FIXED"
    assert tools_called(ev)[-2:] == ["resend_missed_announcements", "play_test_announcement"]
    assert store.merchant("M1001")["soundbox"]["missed_announcements"] == 0


def test_soundbox_language_change(store):
    t, ev = run(store, "M1002", "Please change my soundbox announcement language to Tamil")
    assert t["outcome"] == "SOUNDBOX_LANGUAGE_SET"
    assert store.merchant("M1002")["soundbox"]["language"] == "Tamil"


def test_soundbox_unsupported_language_blocked(store):
    from app.guardrails import check as guard
    assert not guard("set_announcement_language", {"language": "Klingon"}, store, "M1002")["allowed"]


def test_soundbox_long_offline_books_technician(store):
    sb = {**store.merchant("M1002")["soundbox"], "online": False, "last_ping_minutes": 600}
    store.update("merchants", "M1002", {"soundbox": sb})
    t, ev = run(store, "M1002", "My soundbox is not announcing payments")
    assert t["outcome"] == "SOUNDBOX_REPLACEMENT" and "raise_ticket" in tools_called(ev)


# ---------------------------------------------------------------- autonomy dial
def test_autonomy_suggest_holds_demo_1_for_a_human(store):
    store.set_setting("autonomy_level", "suggest")
    t, ev = run(store, "M1001", "Customer's payment failed but money was deducted.")
    assert t["status"] == "ESCALATED" and t["outcome"] == "AUTONOMY_HOLD"
    esc = store.escalations()[0]
    assert esc["proposed_action"]["tool"] == "mark_payment_recovered"
    assert store.transaction("T88120045")["gateway_status"] == "FAILED"      # nothing moved
    assert "₹5,000" not in t["reply"]
    asyncio.run(ops.decide(store, esc["id"], True, "Priya"))                 # a human approves -> it runs
    assert store.transaction("T88120045")["gateway_status"] == "SUCCESS"


def test_autonomy_standard_is_the_default_and_unchanged(store):
    t, _ = run(store, "M1001", "Customer's payment failed but money was deducted.")
    assert t["status"] == "RESOLVED_AI" and t["outcome"] == "PAYMENT_RECOVERED"


def test_autonomy_low_caps_money_at_1000(store):
    store.set_setting("autonomy_level", "low")
    t, _ = run(store, "M1001", "Kal ka settlement nahi aaya")                 # ₹18,640 payout retry
    assert t["status"] == "ESCALATED" and t["outcome"] == "AUTONOMY_HOLD"
    assert store.escalations()[0]["proposed_action"]["tool"] == "retry_settlement"
    assert store.settlement("S2610300")["status"] == "ON_HOLD"


def test_autonomy_suggest_still_allows_non_money_writes(store):
    store.set_setting("autonomy_level", "suggest")
    t, ev = run(store, "M1003", "कल का सेटलमेंट अभी तक बैंक में नहीं आया")       # KYC hold: guidance + raise_ticket
    assert t["outcome"] == "SETTLEMENT_KYC_HOLD" and "raise_ticket" in tools_called(ev)


def test_policy_api_audits_changes():
    from fastapi.testclient import TestClient
    from app.main import app, store as live_store
    live_store.reset()
    c = TestClient(app)
    assert c.get("/api/policy").json()["autonomy_level"] == "standard"
    assert c.post("/api/policy", json={"autonomy_level": "suggest", "changed_by": "Asha (Ops)"}).json()["autonomy_level"] == "suggest"
    assert any(a["action"] == "set_autonomy" and a["actor"] == "ops:Asha (Ops)" for a in live_store.audit_log())
    assert c.post("/api/policy", json={"autonomy_level": "yolo"}).status_code == 400
    live_store.reset()


# ---------------------------------------------------------------- daily hisaab
def test_hisaab_after_checkin(store):
    out, ev = checkin(store, "M1003")
    h = out["hisaab"]
    assert "आज का हिसाब" in h
    assert "₹740" in h                      # UPI today, already in the bank
    assert "₹1,180" in h                    # card today, arrives next working day
    assert "₹860 का अटका भुगतान वापस" in h  # fixed by the check-in, with its amount
    assert any(e["type"] == "checkin_done" and e["hisaab"] == h for e in ev)


def test_hisaab_on_request_in_hinglish(store):
    from app import hisaab
    run(store, "M1001", "Customer's payment failed but money was deducted.")
    out = hisaab.send(store, "M1001")
    assert out["text"].startswith("Raju ji, aaj ka hisaab")
    assert out["card_pending"] == 650 and "₹1,250 ka atka payment wapas" in out["text"]


# ---------------------------------------------------------------- accuracy evals
def test_eval_set_is_balanced():
    from app.evals import EVALS
    assert len(EVALS) == 45
    assert {lang: sum(1 for x in EVALS if x["lang"] == lang) for lang in ("en", "hi", "hinglish")} == {"en": 15, "hi": 15, "hinglish": 15}


def test_rules_triage_accuracy_at_least_85_percent():
    from app.evals import run_evals
    out = asyncio.run(run_evals())
    assert out["models"] == {"rules-engine": 45}          # offline in tests: the rules engine answered every one
    assert out["accuracy"] >= 0.85, out["failures"]
    assert all(b["accuracy"] >= 0.8 for b in out["by_language"].values())
