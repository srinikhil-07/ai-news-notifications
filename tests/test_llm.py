import io
import json
import urllib.error

import pytest

from news_aggregator.llm import (
    LLMError,
    OpenAICompatibleProvider,
    RateLimitError,
    make_provider,
    parse_json_reply,
)


def test_parse_json_reply_accepts_fenced_json():
    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json_reply("not json")
    with pytest.raises(LLMError):
        parse_json_reply("[1, 2]")


def test_make_provider_modes():
    assert make_provider("none", {}) is None
    assert make_provider("openai", {}) is None  # no key -> no-LLM mode
    provider = make_provider("openai", {"LLM_API_KEY": "k", "LLM_MODEL": "m", "LLM_MAX_CALLS": "3"})
    assert provider.model == "m" and provider.max_calls == 3
    assert provider.url == "https://api.openai.com/v1/chat/completions"
    with pytest.raises(ValueError):
        make_provider("openai", {"LLM_API_KEY": "k", "LLM_ENDPOINT": "http://insecure.example.com/v1"})
    with pytest.raises(ValueError):
        make_provider("other", {})


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _http_429(retry_after: str):
    return urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": retry_after}, io.BytesIO())


def test_provider_budget_throttle_and_rate_limits():
    sleeps: list[float] = []
    provider = OpenAICompatibleProvider("k", max_calls=3, min_interval=0, sleep=sleeps.append)
    responses = [_http_429("2"), _reply('{"ok": true}'), _http_429("600")]
    bodies = []

    def fake_post(body):
        bodies.append(json.loads(body))
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    provider._post = fake_post
    # A short Retry-After is honoured once, then the call succeeds.
    assert provider.complete_json("sys", "user") == {"ok": True}
    assert sleeps == [2.0]
    assert bodies[0]["response_format"] == {"type": "json_object"}
    # A long Retry-After stops LLM use for the run.
    with pytest.raises(RateLimitError):
        provider.complete_json("sys", "user")
    # The per-run call budget is enforced.
    with pytest.raises(RateLimitError):
        provider.complete_json("sys", "user")
    assert provider.calls == 3


def test_provider_wraps_http_errors():
    provider = OpenAICompatibleProvider("k", min_interval=0)

    def fail(body):
        raise urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO())

    provider._post = fail
    with pytest.raises(LLMError) as info:
        provider.complete_json("s", "u")
    assert not isinstance(info.value, RateLimitError)
