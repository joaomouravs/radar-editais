"""A small client for OpenAI-compatible chat APIs, with a response cache on disk.

Gemini and Groq both speak this API, so a provider is just a base URL and a key. The
cache makes every evaluation reproducible: re-scoring a run never calls a model again.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
}


@dataclass(frozen=True)
class Model:
    provider: str
    name: str
    # Sent as reasoning_effort when set. Thinking models can spend over a minute on a
    # trivial question without it, so it is part of what the evaluation compares.
    reasoning: str | None = None

    @property
    def id(self) -> str:
        suffix = f"@{self.reasoning}" if self.reasoning else ""
        return f"{self.provider}:{self.name}{suffix}"

    @classmethod
    def parse(cls, model_id: str) -> Model:
        """'gemini:gemini-3.8-flash@low' -> provider, model name and reasoning effort."""
        provider, _, rest = model_id.partition(":")
        if provider not in PROVIDERS or not rest:
            raise ValueError(f"expected provider:model[@effort], got {model_id!r}")
        name, _, reasoning = rest.partition("@")
        return cls(provider, name, reasoning or None)


@dataclass
class Completion:
    content: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cached: bool


class LlmError(Exception):
    pass


class ChatClient:
    def __init__(
        self,
        http: httpx.Client,
        cache_dir: Path,
        *,
        keys: dict[str, str] | None = None,
        retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._cache_dir = cache_dir
        self._keys = keys if keys is not None else load_keys()
        self._retries = retries
        self._sleep = sleep

    def complete(
        self, model: Model, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> Completion:
        body: dict[str, Any] = {
            "model": model.name,
            "messages": messages,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "strict": True, "schema": schema},
            },
        }
        if model.reasoning:
            body["reasoning_effort"] = model.reasoning

        key = hashlib.sha256(
            json.dumps([model.provider, body], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        path = self._cache_dir / key[:2] / f"{key}.json"
        if path.exists():
            hit = json.loads(path.read_text(encoding="utf-8"))
            return Completion(**hit, cached=True)

        start = time.monotonic()
        data = self._post(model, body)
        completion = Completion(
            content=data["choices"][0]["message"]["content"] or "",
            tokens_in=data.get("usage", {}).get("prompt_tokens", 0),
            tokens_out=data.get("usage", {}).get("completion_tokens", 0),
            latency_ms=round((time.monotonic() - start) * 1000),
            cached=False,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = {k: v for k, v in completion.__dict__.items() if k != "cached"}
        path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        return completion

    def _post(self, model: Model, body: dict[str, Any]) -> dict[str, Any]:
        """POST with retries. Free tiers answer 429 when the per-minute quota is spent,
        and the wait they ask for in Retry-After is honoured."""
        base_url, env_var = PROVIDERS[model.provider]
        api_key = self._keys.get(env_var)
        if not api_key:
            raise LlmError(f"{env_var} is not set")
        attempt = 0
        while True:
            try:
                resp = self._http.post(
                    f"{base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.TransportError as exc:
                if attempt == self._retries:
                    raise LlmError(f"{model.id}: {exc!r}") from exc
                wait = 2.0**attempt
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code != 429 and resp.status_code < 500:
                    raise LlmError(f"{model.id}: HTTP {resp.status_code}: {resp.text[:500]}")
                if attempt == self._retries:
                    raise LlmError(f"{model.id}: HTTP {resp.status_code} after retries")
                wait = _retry_after(resp) or 2.0**attempt
            self._sleep(wait)
            attempt += 1


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return min(float(value), 120.0) if value else None
    except ValueError:
        return None


def load_keys(path: Path = Path(".env")) -> dict[str, str]:
    """API keys from the environment, falling back to a .env file."""
    keys: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and not name.strip().startswith("#"):
                keys[name.strip()] = value.strip()
    for _, env_var in PROVIDERS.values():
        if os.environ.get(env_var):
            keys[env_var] = os.environ[env_var]
    return keys
