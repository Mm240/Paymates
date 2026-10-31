"""One interface over many model providers.

    result = await chat(spec, messages, tools=..., json_mode=...)
    result -> {"text": str, "tool_calls": [{"id", "name", "args"}]}

Internal message format (provider-neutral):
    {"role": "system" | "user" | "assistant" | "tool",
     "content": str,
     "tool_calls": [{"id", "name", "args"}],   # assistant only
     "tool_call_id": str}                       # tool only

Two adapters cover every provider we support:
  * OpenAI-compatible Chat Completions -> OpenAI, Gemini, Groq, OpenRouter, Ollama
  * Anthropic Messages API             -> Claude
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

import httpx

from ..config import LLM_TIMEOUT, ModelSpec


class LLMError(Exception):
    pass


class RateLimited(LLMError):
    """HTTP 429 from the provider."""


RETRY_WAIT_S = 2.0


async def chat(
    spec: ModelSpec,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    json_mode: bool = False,
    temperature: float = 0.2,
    max_tokens: int = 900,
) -> dict:
    for attempt in (1, 2):
        try:
            if spec.kind == "anthropic":
                return await _anthropic(spec, messages, tools, temperature, max_tokens)
            return await _openai_compatible(spec, messages, tools, json_mode, temperature, max_tokens)
        except RateLimited:
            # Free tiers rate-limit in bursts: wait once, then let the caller fall back to rules.
            if attempt == 2:
                raise
            await asyncio.sleep(RETRY_WAIT_S)
        except LLMError:
            raise
        except Exception as exc:  # network, timeout, bad JSON from provider...
            raise LLMError(f"{spec.label}: {type(exc).__name__}: {exc}") from exc
    raise LLMError(f"{spec.label}: unreachable")


# ---------------------------------------------------------------- OpenAI-compatible
def _to_openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        role = m["role"]
        if role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        elif role == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": m.get("content") or None,
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                     # Gemini 3 signs each tool call and rejects the next turn without the signature.
                     **({"extra_content": tc["extra"]} if tc.get("extra") else {})}
                    for tc in m["tool_calls"]
                ],
            })
        else:
            out.append({"role": role, "content": m["content"]})
    return out


async def _openai_compatible(spec, messages, tools, json_mode, temperature, max_tokens) -> dict:
    body: dict[str, Any] = {
        "model": spec.model,
        "messages": _to_openai_messages(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]
        body["tool_choice"] = "auto"
    elif json_mode:
        body["response_format"] = {"type": "json_object"}

    headers = {"Content-Type": "application/json"}
    if spec.key:
        headers["Authorization"] = f"Bearer {spec.key}"
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        r = await client.post(f"{spec.base}/chat/completions", headers=headers, json=body)
    if r.status_code == 429:
        raise RateLimited(f"{spec.label} HTTP 429: rate limited")
    if r.status_code >= 400:
        raise LLMError(f"{spec.label} HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    msg = data["choices"][0]["message"]
    calls = []
    for tc in msg.get("tool_calls") or []:
        raw = tc["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            args = {}
        call = {"id": tc.get("id") or f"call_{len(calls)}", "name": tc["function"]["name"], "args": args or {}}
        if tc.get("extra_content"):
            call["extra"] = tc["extra_content"]
        calls.append(call)
    return {"text": msg.get("content") or "", "tool_calls": calls}


# ---------------------------------------------------------------- Anthropic
def _to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            # consecutive tool results must share one user turn
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]})
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": role, "content": m["content"]})
    return system, out


async def _anthropic(spec, messages, tools, temperature, max_tokens) -> dict:
    system, msgs = _to_anthropic(messages)
    body: dict[str, Any] = {
        "model": spec.model, "max_tokens": max_tokens, "temperature": temperature, "messages": msgs,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
    headers = {"x-api-key": spec.key or "", "anthropic-version": "2023-06-01", "content-type": "application/json"}
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        r = await client.post(f"{spec.base}/messages", headers=headers, json=body)
    if r.status_code == 429:
        raise RateLimited(f"{spec.label} HTTP 429: rate limited")
    if r.status_code >= 400:
        raise LLMError(f"{spec.label} HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    calls = [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}}
             for b in data.get("content", []) if b.get("type") == "tool_use"]
    return {"text": text, "tool_calls": calls}


# ---------------------------------------------------------------- helpers
def parse_json(text: str) -> dict:
    """Read a JSON object out of a model reply, tolerating code fences or a stray sentence."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        return json.loads(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise LLMError("model reply contained no JSON object")
