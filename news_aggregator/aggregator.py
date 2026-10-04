"""Fetch, filter and render AI news from tech company blogs."""

from __future__ import annotations

import calendar
import html
import logging
import re
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import feedparser

log = logging.getLogger(__name__)

USER_AGENT = "ai-news-notifications/1.0 (+https://github.com/srinikhil-07/ai-news-notifications)"

PERIODS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
}

AI_KEYWORDS = re.compile(
    r"\b(ai|a\.i\.|artificial intelligence|machine learning|ml|deep learning|"
    r"neural|llms?|large language models?|language models?|gen ?ai|generative|"
    r"transformers?|diffusion|agents?|agentic|gpt|gemini|llama|copilot|"
    r"reinforcement learning|inference|fine-?tun\w*|multimodal|embeddings?|rag)\b",
    re.IGNORECASE,
)

SUMMARY_MAX_CHARS = 280


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    ai_only: bool = True


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    link: str
    published: datetime
    summary: str = ""


def load_feeds(path: str | Path) -> list[Feed]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    feeds = []
    for entry in data.get("feeds", []):
        if not entry.get("name") or not entry.get("url"):
            raise ValueError(f"Feed entry requires 'name' and 'url': {entry!r}")
        feeds.append(Feed(name=entry["name"], url=entry["url"], ai_only=entry.get("ai_only", True)))
    return feeds


def clean_text(raw: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def parse_feed(feed: Feed, source) -> list[Article]:
    """Parse a feed from a URL, file path, or raw XML string into articles."""
    parsed = feedparser.parse(source, agent=USER_AGENT)
    if parsed.get("bozo") and not parsed.entries:
        log.warning("Could not parse feed %s (%s): %s", feed.name, feed.url, parsed.get("bozo_exception"))
        return []

    articles = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title", ""), max_chars=300)
        link = entry.get("link", "")
        published = _entry_datetime(entry)
        if not title or not link or published is None:
            continue
        summary = clean_text(entry.get("summary", ""))
        if not feed.ai_only and not is_ai_related(title, summary, entry):
            continue
        articles.append(Article(feed.name, title, link, published, summary))
    return articles


def is_ai_related(title: str, summary: str, entry=None) -> bool:
    tags = " ".join(t.get("term", "") for t in (entry or {}).get("tags", []) or [])
    return bool(AI_KEYWORDS.search(f"{title} {summary} {tags}"))


def fetch_feed(feed: Feed) -> list[Article]:
    try:
        return parse_feed(feed, feed.url)
    except Exception:  # noqa: BLE001 - one bad feed must not break the digest
        log.exception("Failed to fetch feed %s (%s)", feed.name, feed.url)
        return []


def fetch_all(feeds: Iterable[Feed], max_workers: int = 8) -> list[Article]:
    feeds = list(feeds)
    if not feeds:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(feeds))) as pool:
        results = pool.map(fetch_feed, feeds)
    return [article for articles in results for article in articles]


def _normalize_link(link: str) -> str:
    parts = urlsplit(link.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def select_articles(articles: Iterable[Article], period: str, now: datetime) -> list[Article]:
    """Keep articles published within the period window, deduplicated, newest first."""
    start = now - PERIODS[period]
    seen: set[str] = set()
    selected = []
    for article in sorted(articles, key=lambda a: a.published, reverse=True):
        if not (start < article.published <= now):
            continue
        key = _normalize_link(article.link)
        if key in seen:
            continue
        seen.add(key)
        selected.append(article)
    return selected


def digest_path(output_dir: str | Path, period: str, now: datetime) -> Path:
    if period == "weekly":
        year, week, _ = now.isocalendar()
        name = f"{year}-W{week:02d}.md"
    else:
        name = f"{now:%Y-%m-%d}.md"
    return Path(output_dir) / period / name


def _escape_md(text: str) -> str:
    return re.sub(r"([\[\]])", r"\\\1", text)


def render_markdown(articles: list[Article], period: str, now: datetime) -> str:
    start = now - PERIODS[period]
    title = "Daily" if period == "daily" else "Weekly"
    lines = [
        f"# {title} AI News Digest",
        "",
        f"_{start:%Y-%m-%d %H:%M} UTC → {now:%Y-%m-%d %H:%M} UTC · {len(articles)} article(s)_",
        "",
    ]
    if not articles:
        lines.append("No new AI posts from the tracked blogs in this period.")
        return "\n".join(lines) + "\n"

    by_source: dict[str, list[Article]] = {}
    for article in articles:
        by_source.setdefault(article.source, []).append(article)

    for source in sorted(by_source, key=str.lower):
        lines.append(f"## {source}")
        lines.append("")
        for article in by_source[source]:
            lines.append(f"- [{_escape_md(article.title)}]({article.link}) — {article.published:%Y-%m-%d}")
            if article.summary:
                lines.append(f"  > {article.summary}")
        lines.append("")
    return "\n".join(lines)
