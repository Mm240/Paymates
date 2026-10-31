"""Accuracy evals: run every labelled message in shared/evals.json through triage.

Uses the configured triage model when keys are set, otherwise the rules engine,
and reports which one actually answered. The lexicon was tuned on this set, so
treat the rules-engine score as an upper bound, not a field accuracy.
"""
from __future__ import annotations

import time

from .agents.triage import rules_triage, triage
from .config import load_shared

EVALS = load_shared("evals.json")["items"]


async def run_evals(rules_only: bool = False) -> dict:
    t0 = time.perf_counter()
    by_lang: dict[str, dict] = {}
    failures, models = [], {}
    for x in EVALS:
        # rules_only: the code path alone (free and instant), used by the pilot-readiness gate on every dashboard load
        r, model = (rules_triage(x["text"]), "rules-engine") if rules_only else await triage(x["text"])
        models[model] = models.get(model, 0) + 1
        hit = r["intent"] == x["intent"]
        b = by_lang.setdefault(x["lang"], {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += hit
        if not hit:
            failures.append({"id": x["id"], "lang": x["lang"], "text": x["text"], "expected": x["intent"],
                             "got": r["intent"], "confidence": r["confidence"]})
    correct = sum(b["correct"] for b in by_lang.values())
    for b in by_lang.values():
        b["accuracy"] = round(b["correct"] / b["n"], 3)
    return {"total": len(EVALS), "correct": correct, "accuracy": round(correct / len(EVALS), 3), "by_language": by_lang,
            "failures": failures, "models": models, "ms": int((time.perf_counter() - t0) * 1000)}
