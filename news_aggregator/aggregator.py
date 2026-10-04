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

from . import scraper

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

RESEARCH_KEYWORDS = re.compile(
    r"\b(research|paper|papers|arxiv|study|studies|benchmarks?|datasets?|"
    r"interpretability|alignment|we propose|we present|findings|evaluations?|"
    r"theory|theoretical|experiments?|preprint|neurips|icml|iclr|cvpr|acl|emnlp)\b",
    re.IGNORECASE,
)

SUMMARY_MAX_CHARS = 280

FEED_TYPES = ("rss", "html")
CATEGORIES = ("research", "product", "auto")
CATEGORY_TITLES = {
    "research": "Research",
    "product": "Product & Announcements",
}


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    ai_only: bool = True
    category: str = "auto"
    type: str = "rss"
    link_pattern: str = ""
    max_articles: int = 15


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    link: str
    published: datetime
    summary: str = ""
    category: str = "product"


def load_feeds(path: str | Path) -> list[Feed]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    feeds = []
    for entry in data.get("feeds", []):
        if not entry.get("name") or not entry.get("url"):
            raise ValueError(f"Feed entry requires 'name' and 'url': {entry!r}")
        feed = Feed(
            name=entry["name"],
            url=entry["url"],
            ai_only=entry.get("ai_only", True),
            category=entry.get("category", "auto"),
            type=entry.get("type", "rss"),
            link_pattern=entry.get("link_pattern", ""),
            max_articles=int(entry.get("max_articles", 15)),
        )
        if feed.type not in FEED_TYPES:
            raise ValueError(f"Feed {feed.name!r}: 'type' must be one of {FEED_TYPES}")
        if feed.category not in CATEGORIES:
            raise ValueError(f"Feed {feed.name!r}: 'category' must be one of {CATEGORIES}")
        if feed.type == "html":
            if not feed.link_pattern:
                raise ValueError(f"Feed {feed.name!r}: html feeds require 'link_pattern'")
            re.compile(feed.link_pattern)
        feeds.append(feed)
    return feeds


def classify(feed: Feed, title: str, summary: str, tags: str = "") -> str:
    """Return 'research' or 'product' for an article."""
    if feed.category != "auto":
        return feed.category
    return "research" if RESEARCH_KEYWORDS.search(f"{title} {summary} {tags}") else "product"


def _tags(entry) -> str:
    return " ".join(t.get("term", "") for t in (entry or {}).get("tags", []) or [])


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
        tags = _tags(entry)
        if not feed.ai_only and not is_ai_related(title, summary, tags):
            continue
        articles.append(Article(feed.name, title, link, published, summary, classify(feed, title, summary, tags)))
    return articles


def is_ai_related(title: str, summary: str, tags: str = "") -> bool:
    return bool(AI_KEYWORDS.search(f"{title} {summary} {tags}"))


def scrape_feed(feed: Feed, fetch=None) -> list[Article]:
    """Scrape an HTML listing page and its article pages into articles."""
    fetch = fetch or (lambda url: scraper.fetch_url(url, USER_AGENT))
    links = scraper.extract_links(fetch(feed.url), feed.url, feed.link_pattern)[: feed.max_articles]
    articles = []
    for link in links:
        try:
            raw_title, raw_summary, published = scraper.extract_article(fetch(link))
        except Exception:  # noqa: BLE001 - skip broken article pages
            log.warning("Could not scrape article %s for %s", link, feed.name, exc_info=True)
            continue
        title = clean_text(raw_title, max_chars=300)
        summary = clean_text(raw_summary)
        if not title or published is None:
            log.info("Skipping %s (%s): missing title or date", link, feed.name)
            continue
        if not feed.ai_only and not is_ai_related(title, summary):
            continue
        articles.append(Article(feed.name, title, link, published, summary, classify(feed, title, summary)))
    return articles


def fetch_feed(feed: Feed) -> list[Article]:
    try:
        if feed.type == "html":
            return scrape_feed(feed)
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
        f"_{start:%Y-%m-%d %H:%M %Z} → {now:%Y-%m-%d %H:%M %Z} · {len(articles)} article(s)_",
        "",
    ]
    if not articles:
        lines.append("No new AI posts from the tracked blogs in this period.")
        return "\n".join(lines) + "\n"

    for category, heading in CATEGORY_TITLES.items():
        by_source: dict[str, list[Article]] = {}
        for article in articles:
            if article.category == category:
                by_source.setdefault(article.source, []).append(article)
        if not by_source:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        for source in sorted(by_source, key=str.lower):
            lines.append(f"### {source}")
            lines.append("")
            for article in by_source[source]:
                lines.append(f"- [{_escape_md(article.title)}]({article.link}) — {article.published:%Y-%m-%d}")
                if article.summary:
                    lines.append(f"  > {article.summary}")
            lines.append("")
    return "\n".join(lines)
