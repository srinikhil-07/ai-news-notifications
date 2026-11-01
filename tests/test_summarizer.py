import json
from dataclasses import replace
from http.client import IncompleteRead
from types import SimpleNamespace
from urllib.error import URLError

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, OpenAI

from news_aggregator import summarizer as module
from news_aggregator import __main__ as cli
from news_aggregator.aggregator import Article
from news_aggregator.summarizer import MAX_INPUT_CHARS, Summarizer, validate_summary

from .test_aggregator import FIXTURES, NOW

TEXT = "This research evaluates language models under controlled conditions. " * 40
SUMMARY = {
    "takeaway": "The lab evaluated language models.",
    "key_points": ["Tests used controlled conditions.", "The findings concern the evaluated models only."],
}


@pytest.fixture
def backend(tmp_path, monkeypatch):
    calls = []
    clock = [0.0]
    sleeps = []

    def create(**kwargs):
        calls.append((clock[0], kwargs))
        return response()

    monkeypatch.setattr(module, "OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(module.time, "sleep", sleep)
    summarizer = Summarizer("test-key", tmp_path / "cache")
    return summarizer, calls, sleeps


def response(data=SUMMARY, finish="stop", refusal=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish,
        message=SimpleNamespace(content=json.dumps(data), refusal=refusal),
    )])


def article(link="https://example.com/article", content=TEXT):
    return Article("Lab", "Research", link, NOW, "Publisher excerpt", content=content)


def test_summary_uses_source_and_structured_output_and_caches(backend):
    summarizer, calls, sleeps = backend
    result = summarizer.summarize(article())
    assert result.summary_status == "ai"
    assert result.summary == SUMMARY["takeaway"]
    assert result.key_points == tuple(SUMMARY["key_points"])
    _, request = calls[0]
    assert request["model"] == "gpt-5-nano"
    assert request["reasoning_effort"] == "minimal"
    assert request["max_completion_tokens"] == 2048
    assert request["store"] is False
    assert request["response_format"]["json_schema"]["strict"] is True
    assert json.loads(request["messages"][1]["content"])["article_text"] == TEXT
    assert summarizer.summarize(article()) == result
    assert len(calls) == 1
    assert sleeps == []
    assert "test-key" not in next(summarizer.cache_dir.glob("*.json")).read_text()


def test_cache_survives_new_instance_and_changes_invalidate_it(backend):
    summarizer, calls, _ = backend
    summarizer.summarize(article())
    other = Summarizer("test-key", summarizer.cache_dir)
    other.summarize(article())
    assert len(calls) == 1
    other.summarize(article(content=TEXT + "Different findings."))
    other.summarize(replace(article(), title="Updated title"))
    other.model = "another-model"
    other.summarize(article())
    assert len(calls) == 4


def test_requests_are_paced_but_cache_hits_are_not(backend):
    summarizer, calls, sleeps = backend
    summarizer.summarize_all([article(), article(), article("https://example.com/b")])
    assert [timestamp for timestamp, _ in calls] == [0, 60]
    assert sleeps == [60]


def test_long_source_is_bounded_and_partial_is_visible(backend):
    summarizer, calls, _ = backend
    result = summarizer.summarize(article(content=TEXT * 100))
    supplied = json.loads(calls[0][1]["messages"][1]["content"])
    assert len(supplied["article_text"]) <= MAX_INPUT_CHARS
    assert supplied["partial_source"] is True
    assert result.summary_status == "partial"


def test_fetch_full_text_when_feed_has_none(backend, monkeypatch):
    summarizer, calls, _ = backend
    fetched = []

    def fetch(link, agent):
        fetched.append(link)
        return f"<html><body><article><p>{TEXT}</p></article></body></html>"

    monkeypatch.setattr(module.scraper, "fetch_url", fetch)
    result = summarizer.summarize(article(content=""))
    assert fetched == ["https://example.com/article"]
    assert result.summary_status == "ai"
    assert calls


def test_unreadable_and_short_sources_do_not_call_api(backend, monkeypatch, caplog):
    summarizer, calls, _ = backend
    monkeypatch.setattr(module.scraper, "fetch_url", lambda *_: (_ for _ in ()).throw(URLError("blocked")))
    failed = summarizer.summarize(article(content=""))
    short = summarizer.summarize(article(content="A marketing description."))
    assert failed.summary_status == short.summary_status == "unavailable"
    assert failed.summary == "Publisher excerpt"
    assert not calls
    assert "Full text unavailable" in caplog.text
    assert "Insufficient article text" in caplog.text


def test_incomplete_http_read_uses_publisher_excerpt(backend, monkeypatch, caplog):
    summarizer, calls, _ = backend
    monkeypatch.setattr(
        module.scraper, "fetch_url",
        lambda *_: (_ for _ in ()).throw(IncompleteRead(b"partial")),
    )
    result = summarizer.summarize(article(content=""))
    assert result.summary_status == "unavailable"
    assert result.summary == "Publisher excerpt"
    assert not calls
    assert "Full text unavailable" in caplog.text


@pytest.mark.parametrize("data", [
    {}, {"takeaway": "T", "key_points": ["one"]},
    {"takeaway": "", "key_points": ["one", "two"]},
    {"takeaway": "T", "key_points": ["one", 2]},
    {"takeaway": "T", "key_points": ["one", "two"], "extra": "bad"},
])
def test_validate_summary_rejects_bad_shape(data):
    with pytest.raises(ValueError):
        validate_summary(data)


def test_validate_summary_allows_more_than_sixty_words():
    assert validate_summary({"takeaway": "word " * 59, "key_points": ["one", "two"]})


@pytest.mark.parametrize("result", [
    response(finish="length"), response(refusal="Cannot summarize"),
])
def test_invalid_response_falls_back_explicitly_and_is_not_cached(backend, caplog, result):
    summarizer, _, _ = backend
    summarizer.client.chat.completions.create = lambda **_: result
    value = summarizer.summarize(article())
    assert value.summary_status == "unavailable"
    assert "Invalid AI summary" in caplog.text
    assert not summarizer.cache_dir.exists()


def test_response_over_sixty_words_remains_an_ai_summary(backend):
    summarizer, _, _ = backend
    summarizer.client.chat.completions.create = lambda **_: response(
        {"takeaway": "word " * 59, "key_points": ["one", "two"]}
    )
    result = summarizer.summarize(article())
    assert result.summary_status == "ai"
    assert result.summary == " ".join(["word"] * 59)
    assert result.summary != "Publisher excerpt"


def api_error(status, code=None, retry_after=None):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    headers = {"retry-after": retry_after} if retry_after else {}
    return APIStatusError("error", response=httpx.Response(status, request=request, headers=headers),
                          body={"code": code} if code else {})


def test_rate_limit_honors_retry_after_and_pacing(backend):
    summarizer, _, sleeps = backend
    attempts = []

    def create(**kwargs):
        attempts.append(module.time.monotonic())
        if len(attempts) == 1:
            raise api_error(429, retry_after="120")
        return response()

    summarizer.client.chat.completions.create = create
    assert summarizer.summarize(article()).summary_status == "ai"
    assert attempts == [0, 120]
    assert sleeps == [120]


def test_retry_after_http_date_is_honored(backend, monkeypatch):
    summarizer, _, sleeps = backend
    monkeypatch.setattr(module.time, "time", lambda: NOW.timestamp())
    attempts = []
    from email.utils import formatdate

    def create(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise api_error(429, retry_after=formatdate(NOW.timestamp() + 180, usegmt=True))
        return response()

    summarizer.client.chat.completions.create = create
    summarizer.summarize(article())
    assert sleeps == [180]


@pytest.mark.parametrize("retry_after", ["3600", "inf", "nan"])
def test_long_or_invalid_cooldown_stops_without_long_sleep(backend, retry_after):
    summarizer, _, sleeps = backend
    summarizer.client.chat.completions.create = lambda **_: (_ for _ in ()).throw(
        api_error(429, retry_after=retry_after)
    )
    with pytest.raises(RuntimeError):
        summarizer.summarize(article())
    assert sleeps == []


@pytest.mark.parametrize("error", [api_error(401), api_error(404), api_error(429, "insufficient_quota")])
def test_permanent_api_errors_abort_without_retries(backend, error):
    summarizer, _, sleeps = backend
    summarizer.client.chat.completions.create = lambda **_: (_ for _ in ()).throw(error)
    with pytest.raises(RuntimeError):
        summarizer.summarize(article())
    assert not sleeps
    assert not summarizer.cache_dir.exists()


@pytest.mark.parametrize("error", [
    api_error(408),
    api_error(500),
    APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")),
])
def test_transient_failures_have_bounded_retries(backend, error):
    summarizer, _, sleeps = backend
    attempts = []

    def create(**kwargs):
        attempts.append(module.time.monotonic())
        raise error

    summarizer.client.chat.completions.create = create
    with pytest.raises(RuntimeError, match="four attempts"):
        summarizer.summarize(article())
    assert len(attempts) == 4
    assert all(b - a >= 60 for a, b in zip(attempts, attempts[1:]))
    assert len(sleeps) == 3


def test_corrupt_cache_is_logged_and_regenerated(backend, caplog):
    summarizer, calls, _ = backend
    summarizer.summarize(article())
    next(summarizer.cache_dir.glob("*.json")).write_text("not JSON")
    summarizer.summarize(article())
    assert len(calls) == 2
    assert "Invalid summary cache" in caplog.text


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_invalid_interval_is_rejected(tmp_path, interval):
    with pytest.raises(ValueError, match="at least 1"):
        Summarizer("test-key", tmp_path, interval_seconds=interval)


def test_real_sdk_serializes_and_parses_summary_without_network(backend):
    summarizer, _, _ = backend
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": "gpt-5-nano",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(SUMMARY), "refusal": None},
            }],
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        summarizer.client = OpenAI(
            api_key="test-key", base_url="https://api.openai.com/v1",
            http_client=http, max_retries=0,
        )
        assert summarizer.summarize(article()).summary_status == "ai"
    assert requests[0].url.path == "/v1/chat/completions"
    body = json.loads(requests[0].content)
    assert body["model"] == "gpt-5-nano"
    assert body["response_format"]["json_schema"]["schema"] == module.SUMMARY_SCHEMA


def test_end_to_end_weekly_digest_contains_inline_summaries_and_original_links(backend, tmp_path, monkeypatch):
    summarizer, calls, sleeps = backend
    monkeypatch.setattr(cli, "Summarizer", lambda *args: summarizer)
    monkeypatch.setattr(
        module.scraper, "fetch_url",
        lambda *_: f"<html><body><article><p>{TEXT}</p></article></body></html>",
    )
    feeds = tmp_path / "feeds.toml"
    feeds.write_text(f'[[feeds]]\nname = "Lab"\nurl = "{FIXTURES / "rss.xml"}"\n')
    assert cli.main([
        "--period", "weekly", "--now", NOW.isoformat(), "--feeds", str(feeds),
        "--summarize", "--output-dir", str(tmp_path / "digests"),
    ]) == 0
    body = (tmp_path / "digests" / "weekly" / "2026-W40.md").read_text()
    assert body.count("**AI summary:**") == 3
    assert body.count("  - ") == 6
    assert "Copilot" not in body
    assert "Publisher excerpt" not in body
    assert "[Introducing a new large language model]" in body
    assert len(calls) == 3
    assert sleeps == [60, 60]
