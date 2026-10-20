"""LLM providers behind a tiny interface.

``complete_json(system, user)`` returns the model's reply parsed as a JSON
object. Providers raise :class:`RateLimitError` when the provider rate limit or
the per-run call budget is reached, and :class:`LLMError` for other failures,
so callers can degrade gracefully (detailed → one-line → excerpt).

GitHub Models was retired on 2026-07-30, so the built-in provider talks to any
OpenAI-compatible ``/chat/completions`` endpoint (OpenAI, Azure AI Foundry, …).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PROVIDERS = ("none", "openai")
DEFAULT_ENDPOINT = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_CALLS = 40
MAX_RETRY_AFTER_SECONDS = 30


class LLMError(Exception):
    """The call failed; fall back for this call only."""


class RateLimitError(LLMError):
    """Rate limit or call budget reached; make no more calls this run."""


class Provider(Protocol):
    name: str

    def complete_json(self, system: str, user: str, max_tokens: int = 1500) -> dict: ...


def parse_json_reply(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LLMError(f"model reply is not JSON: {text[:200]!r}") from exc
    if not isinstance(data, dict):
        raise LLMError("model reply is not a JSON object")
    return data


class OpenAICompatibleProvider:
    """Chat completions over HTTPS with JSON mode, throttling and a call budget."""

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        max_calls: int = DEFAULT_MAX_CALLS,
        min_interval: float = 1.0,
        timeout: float = 90.0,
        sleep=time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("an API key is required")
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError(f"LLM endpoint must be an https URL, got {endpoint!r}")
        url = endpoint.rstrip("/")
        self.url = url if url.endswith("/chat/completions") else f"{url}/chat/completions"
        self.host = parts.hostname or ""
        self.api_key = api_key
        self.model = model
        self.max_calls = max_calls
        self.min_interval = min_interval
        self.timeout = timeout
        self.calls = 0
        self._sleep = sleep
        self._last = 0.0
        self._lock = threading.Lock()

    def _post(self, body: bytes):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key}
        if self.host.endswith((".azure.com", ".azure-api.net")):
            headers["api-key"] = self.api_key
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - https enforced
            return json.loads(response.read().decode("utf-8"))

    def complete_json(self, system: str, user: str, max_tokens: int = 1500) -> dict:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        for attempt in range(2):
            with self._lock:
                if self.calls >= self.max_calls:
                    raise RateLimitError(f"LLM call budget of {self.max_calls} reached")
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    self._sleep(wait)
                self.calls += 1
                self._last = time.monotonic()
            try:
                data = self._post(body)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry_after = _retry_after(exc)
                    if attempt == 0 and retry_after is not None and retry_after <= MAX_RETRY_AFTER_SECONDS:
                        self._sleep(retry_after)
                        continue
                    raise RateLimitError(f"rate limited (HTTP 429, retry-after={retry_after})") from exc
                raise LLMError(f"HTTP {exc.code} from LLM endpoint") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                raise LLMError(f"LLM request failed: {exc}") from exc
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError("unexpected LLM response shape") from exc
            return parse_json_reply(content)
        raise RateLimitError("rate limited")


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    try:
        return float(exc.headers.get("Retry-After", ""))
    except (TypeError, ValueError, AttributeError):
        return None


def make_provider(name: str, env: dict | None = None) -> Provider | None:
    """Build the configured provider, or ``None`` for the no-LLM mode.

    ``openai`` reads ``LLM_API_KEY``, ``LLM_MODEL``, ``LLM_ENDPOINT``,
    ``LLM_MAX_CALLS`` and ``LLM_MIN_INTERVAL`` from the environment. If no key
    is configured, a warning is logged and the no-LLM mode is used.
    """
    env = os.environ if env is None else env
    if name in ("", "none"):
        return None
    if name != "openai":
        raise ValueError(f"unknown LLM provider {name!r}; expected one of {PROVIDERS}")
    key = env.get("LLM_API_KEY", "")
    if not key:
        log.warning("LLM_API_KEY is not set; building the digest without LLM summaries")
        return None
    return OpenAICompatibleProvider(
        api_key=key,
        model=env.get("LLM_MODEL") or DEFAULT_MODEL,
        endpoint=env.get("LLM_ENDPOINT") or DEFAULT_ENDPOINT,
        max_calls=int(env.get("LLM_MAX_CALLS") or DEFAULT_MAX_CALLS),
        min_interval=float(env.get("LLM_MIN_INTERVAL") or 1.0),
    )
