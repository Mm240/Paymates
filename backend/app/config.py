"""Runtime configuration and multi-model routing.

PayMates is model-agnostic. Each agent has a job, and each job suits a
different kind of model:

  triage     -> fast and cheap (classify intent, pull out IDs)
  action     -> strongest tool-calling model (decides and acts)
  responder  -> good multilingual writer (Hindi / Hinglish / English)
  escalation -> careful summariser (writes the briefing for a human)

If several provider keys are set, each agent gets the provider that suits
it best. If none are set (or PAYMATES_MODE=offline), every agent falls back
to the deterministic rules engine, so the demo always works.

Override any agent with e.g.  PAYMATES_ACTION_MODEL=anthropic:claude-sonnet-5-5
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]          # repo root
SHARED = ROOT / "shared"
FRONTEND_DIST = ROOT / "frontend" / "dist"
DATA_DIR = ROOT / "backend" / "data"


def _load_dotenv() -> None:
    """Tiny .env loader so we don't need python-dotenv."""
    for path in (ROOT / ".env", ROOT / "backend" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


def load_shared(name: str) -> dict:
    return json.loads((SHARED / name).read_text(encoding="utf-8"))


SEED = load_shared("seed.json")
LEXICON = load_shared("lexicon.json")
TEMPLATES = load_shared("templates.json")
POLICY = SEED["policy"]
PRICING = load_shared("pricing.json")


def price_for(model: str) -> dict:
    """The longest price-table key contained in the model name; unknown models use 'default'."""
    hits = [k for k in PRICING["models"] if k in model.lower()]
    return PRICING["models"][max(hits, key=len)] if hits else PRICING["default"]


def cost_inr(model: str, input_tokens: int, output_tokens: int) -> float:
    """₹ for one model call from the (editable) list-price table."""
    p = price_for(model)
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000 * PRICING["usd_inr"]

# --------------------------------------------------------------------------
# Providers. "openai" kind = OpenAI-compatible Chat Completions API, which
# OpenAI, Google Gemini, Groq, OpenRouter and Ollama all speak.
# --------------------------------------------------------------------------
PROVIDERS: dict[str, dict] = {
    "openai": {
        "kind": "openai", "base": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY",
        "fast": "gpt-4.1-mini", "strong": "gpt-4.1",
    },
    "anthropic": {
        "kind": "anthropic", "base": "https://api.anthropic.com/v1", "key_env": "ANTHROPIC_API_KEY",
        "fast": "claude-haiku-5-5", "strong": "claude-sonnet-5-5",
    },
    "gemini": {
        "kind": "openai", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        # Rolling aliases survive model retirements. Pro has no free-tier quota, so free keys use Flash for both.
        "key_env": "GEMINI_API_KEY", "fast": "gemini-flash-latest", "strong": os.getenv("GEMINI_STRONG_MODEL", "gemini-flash-latest"),
    },
    "groq": {
        "kind": "openai", "base": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
        "fast": "llama-3.1-8b-instant", "strong": "llama-3.3-70b-versatile",
    },
    "openrouter": {
        "kind": "openai", "base": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
        "fast": "meta-llama/llama-3.1-8b-instruct", "strong": "openai/gpt-4.1",
    },
    "ollama": {
        "kind": "openai", "base": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "key_env": None, "enable_env": "OLLAMA_ENABLED", "fast": "llama3.1", "strong": "llama3.1",
    },
}

# Which provider each agent prefers, best first.
AGENT_PREFERENCES: dict[str, tuple[str, list[str]]] = {
    "triage":     ("fast",   ["groq", "gemini", "openai", "anthropic", "openrouter", "ollama"]),
    "action":     ("strong", ["anthropic", "openai", "gemini", "groq", "openrouter", "ollama"]),
    "responder":  ("fast",   ["gemini", "openai", "anthropic", "groq", "openrouter", "ollama"]),
    "escalation": ("fast",   ["anthropic", "openai", "gemini", "groq", "openrouter", "ollama"]),
}


# Optional base-URL overrides (Azure/OpenAI proxies, self-hosted gateways, local mocks).
for _name, _p in PROVIDERS.items():
    _env = os.getenv(f"{_name.upper()}_BASE_URL")
    if _env:
        _p["base"] = _env.rstrip("/")


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    kind: str
    base: str
    key: Optional[str]

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def provider_available(name: str) -> bool:
    p = PROVIDERS.get(name)
    if not p:
        return False
    if p.get("key_env"):
        return bool(os.getenv(p["key_env"]))
    if p.get("enable_env"):
        return os.getenv(p["enable_env"], "").lower() in ("1", "true", "yes")
    return False


def mode() -> str:
    """auto = use LLMs when a key exists, else rules. offline = rules only."""
    return os.getenv("PAYMATES_MODE", "auto").lower()


def model_for(agent: str) -> Optional[ModelSpec]:
    """Resolve which model an agent should use, or None for the rules engine."""
    if mode() == "offline":
        return None
    override = os.getenv(f"PAYMATES_{agent.upper()}_MODEL")
    if override and ":" in override:
        prov, model = override.split(":", 1)
        if prov in PROVIDERS and provider_available(prov):
            p = PROVIDERS[prov]
            return ModelSpec(prov, model, p["kind"], p["base"], os.getenv(p["key_env"]) if p.get("key_env") else None)
    tier, prefs = AGENT_PREFERENCES[agent]
    for prov in prefs:
        if provider_available(prov):
            p = PROVIDERS[prov]
            return ModelSpec(prov, p[tier], p["kind"], p["base"], os.getenv(p["key_env"]) if p.get("key_env") else None)
    return None


def routing_table() -> dict[str, str]:
    return {agent: (spec.label if (spec := model_for(agent)) else "rules-engine") for agent in AGENT_PREFERENCES}


def pacing_seconds() -> float:
    """Small pause between agent steps so the live trace is watchable on stage."""
    try:
        return max(0.0, float(os.getenv("PAYMATES_PACING_MS", "450")) / 1000.0)
    except ValueError:
        return 0.45


LLM_TIMEOUT = float(os.getenv("PAYMATES_LLM_TIMEOUT", "15"))
