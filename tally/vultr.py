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
    usd_per_m: tuple[float, float] = (USD_PER_M_INPUT, USD_PER_M_OUTPUT)
    request_id: str = ""  # the service's own id for the call, when it gives one (SERV: x-openserv-request-id)

    @property
    def usd(self) -> float:
        return self.input_tokens * self.usd_per_m[0] / 1e6 + self.output_tokens * self.usd_per_m[1] / 1e6


def _key() -> str:
    key = os.environ.get("VULTR_INFERENCE_API_KEY", "")
    if not key:
        raise InferenceError("VULTR_INFERENCE_API_KEY is not set")
    return key


def chat_json(messages: list[dict], schema: dict, models: list[str], *, name: str = "answer", max_tokens: int = 1200,
              tries_per_model: int = 2, timeout: float = 90.0) -> Answer:
    """Ask for JSON matching `schema`; return the first answer that parses, from the first model that gives one."""
    return ask_json(BASE_URL, _key(), messages, schema, models, extra={"reasoning_effort": "minimal"}, name=name,
                    max_tokens=max_tokens, tries_per_model=tries_per_model, timeout=timeout)


def ask_json(base_url: str, key: str, messages: list[dict], schema: dict, models: list[str], *, extra: dict | None = None,
             prices: dict[str, tuple[float, float]] | None = None, name: str = "answer", max_tokens: int = 1200,
             max_tokens_field: str = "max_tokens", tries_per_model: int = 2, timeout: float = 90.0) -> Answer:
    """The loop behind every model call, on any OpenAI-compatible service: each model in turn, a couple of tries each,
    until an answer parses against `schema`. `prices` maps a model to its dollars per million tokens in and out;
    `max_tokens_field` is what the service calls the answer's length limit (newer OpenAI models refuse `max_tokens`)."""
    attempts: list[str] = []
    for model in models:
        for attempt in range(tries_per_model):
            body = {
                "model": model, "messages": messages, max_tokens_field: max_tokens, **(extra or {}),
                "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
            }
            req = urllib.request.Request(f"{base_url}/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.load(r)
                    request_id = r.headers.get("x-openserv-request-id") or r.headers.get("x-request-id") or ""
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
            answer = Answer(data, model, time.monotonic() - started, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)),
                            attempts, request_id=request_id)
            if prices is not None:
                answer.usd_per_m = prices.get(model, max(prices.values()))
            return answer
    raise InferenceError("no model gave a usable answer: " + "; ".join(attempts))
