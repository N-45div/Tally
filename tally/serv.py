"""SERV Reasoning (OpenServ): where Tally asks a model for judgement, never for a decision.

It is OpenAI-compatible at https://inference-api.openserv.ai/v1. SERV turns the system prompt into a structured
reasoning plan that the model then follows, which is why every call here must carry one (the service refuses a
request without it). The key comes from OPENSERV_API_KEY, held only by the worker that plans payment runs; it is never
passed into a sandbox, never given to the web app, and never logged.

What comes back must match a strict JSON schema, and the caller checks it again in code: a model can propose, the
records and the rules dispose. Nothing printed on an invoice is ever sent here, only records Tally already holds.
"""
from __future__ import annotations

import os

from tally.vultr import Answer, InferenceError, ask_json  # noqa: F401  (InferenceError is re-exported)

BASE_URL = os.environ.get("OPENSERV_URL", "https://inference-api.openserv.ai/v1")
# Dollars per million tokens in and out, SERV's reasoning included (docs.openserv.ai, models page, Nov 13).
PRICES = {
    "gpt-5.4-mini": (1.00, 6.00),
    "gpt-5.4-nano": (0.25, 1.60),
    "claude-haiku-4.5": (1.25, 6.50),
    "gemini-3.1-flash-lite": (0.30, 1.80),
    "gemini-2.5-flash": (0.40, 3.50),
}
# Two families, so one model's quirk does not stop a plan; the first is the one used when it answers.
PLANNER_MODELS = ["gpt-5.4-mini", "gemini-2.5-flash"]


def _key() -> str:
    key = os.environ.get("OPENSERV_API_KEY", "")
    if not key:
        raise InferenceError("OPENSERV_API_KEY is not set")
    return key


def reason_json(system: str, user: str, schema: dict, models: list[str] | None = None, *, name: str = "answer",
                effort: str = "low", max_tokens: int = 2000, timeout: float = 120.0, tools: list[dict] | None = None) -> Answer:
    """One SERV Reasoning call: a system prompt (required by the service), the user's message, and a strict schema.
    `tools` turns on SERV's own features (serv_prompt_guard, serv_shadow_agent): SERV reads and strips them before the
    model runs, so the model is never given a tool it could call."""
    if any(not t.get("function", {}).get("name", "").startswith("serv_") for t in tools or []):
        raise ValueError("only SERV's own serv_* tools are passed; the model is given no tools")
    if not system.strip():
        raise ValueError("SERV Reasoning needs a system prompt")
    if effort not in ("none", "low", "medium", "high"):
        raise ValueError(f"unknown reasoning effort {effort!r}")
    return ask_json(BASE_URL, _key(), [{"role": "system", "content": system}, {"role": "user", "content": user}], schema,
                    models or PLANNER_MODELS, extra={"reasoning_effort": effort} | ({"tools": tools} if tools else {}), prices=PRICES, name=name,
                    max_tokens=max_tokens, max_tokens_field="max_completion_tokens", tries_per_model=2, timeout=timeout)
