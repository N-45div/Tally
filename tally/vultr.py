"""Vultr Serverless Inference: the only place Tally calls a model.

It is OpenAI-compatible at https://api.vultrinference.com/v1. The key comes from VULTR_INFERENCE_API_KEY on the host
running Tally's control plane; it is never passed into a sandbox, and it is never logged.

A model can answer with empty or cut-off content (seen live: a reasoning model spending its whole budget on thinking),
or the service can be busy. Each call is therefore tried on the models given, in order, a couple of times each, and
what comes back must parse against the JSON schema asked for; otherwise the next try runs.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

BASE_URL = os.environ.get("VULTR_INFERENCE_URL", "https://api.vultrinference.com/v1")
# Prices per million tokens (Vultr's published flat rate), for the cost each call reports.
USD_PER_M_INPUT = 0.55
USD_PER_M_OUTPUT = 2.75


class InferenceError(RuntimeError):
    pass


@dataclass
class Answer:
    data: dict
    model: str
    seconds: float
    input_tokens: int
    output_tokens: int
    attempts: list[str] = field(default_factory=list)

    @property
    def usd(self) -> float:
        return self.input_tokens * USD_PER_M_INPUT / 1e6 + self.output_tokens * USD_PER_M_OUTPUT / 1e6


def _key() -> str:
    key = os.environ.get("VULTR_INFERENCE_API_KEY", "")
    if not key:
        raise InferenceError("VULTR_INFERENCE_API_KEY is not set")
    return key


def chat_json(messages: list[dict], schema: dict, models: list[str], *, name: str = "answer", max_tokens: int = 1200,
              tries_per_model: int = 2, timeout: float = 90.0) -> Answer:
    """Ask for JSON matching `schema`; return the first answer that parses, from the first model that gives one."""
    attempts: list[str] = []
    for model in models:
        for attempt in range(tries_per_model):
            body = {
                "model": model, "messages": messages, "max_tokens": max_tokens, "reasoning_effort": "minimal",
                "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
            }
            req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.load(r)
            except urllib.error.HTTPError as e:
                attempts.append(f"{model}: HTTP {e.code}")
                if e.code in (401, 403):
                    raise InferenceError("the inference key was refused") from None
                time.sleep(1.5 * (attempt + 1))
                continue
            except (urllib.error.URLError, TimeoutError) as e:
                attempts.append(f"{model}: {type(e).__name__}")
                continue
            content = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                attempts.append(f"{model}: {'empty' if not content.strip() else 'not JSON'} reply")
                continue
            if not isinstance(data, dict) or any(k not in data for k in schema.get("required", [])):
                attempts.append(f"{model}: missing fields")
                continue
            u = d.get("usage") or {}
            attempts.append(f"{model}: ok")
            return Answer(data, model, time.monotonic() - started, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)), attempts)
    raise InferenceError("no model gave a usable answer: " + "; ".join(attempts))
