"""Fetch, filter and render AI news from tech company blogs."""

from __future__ import annotations

import calendar
import html
import logging
import re
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal
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
ISSUE_MAX_CHARS = 60_000

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
    content: str = ""
    key_points: tuple[str, ...] = ()
    summary_status: Literal["publisher", "ai", "partial", "unavailable"] = "publisher"


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


def _fetch_url_with_timeout(url: str, timeout: float = 20.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - URLs come from config
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_feed(feed: Feed, source) -> list[Article]:
    """Parse a feed from a URL, file path, or raw XML string into articles."""
    if isinstance(source, str) and source.startswith(("http://", "https://")):
        source = _fetch_url_with_timeout(source)
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
        content = "\n\n".join(
            " ".join(item.get("value", "").split())
            if item.get("type") == "text/plain"
            else scraper.extract_text(f"<html><body><article>{item.get('value', '')}</article></body></html>")
            for item in entry.get("content", [])
            if item.get("type") in ("text/plain", "text/html", "application/xhtml+xml")
        )
        articles.append(Article(
            feed.name, title, link, published, summary,
            classify(feed, title, summary, tags), content=content,
        ))
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
            page = fetch(link)
            raw_title, raw_summary, published = scraper.extract_article(page)
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
        articles.append(Article(
            feed.name, title, link, published, summary, classify(feed, title, summary),
            content=scraper.extract_text(page),
        ))
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
    results: list[list[Article]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(feeds))) as pool:
        tasks = {pool.submit(fetch_feed, feed): feed for feed in feeds}
        for future in tasks:
            try:
                result = future.result()
            except Exception:  # noqa: BLE001 - one bad feed must not break the digest
                feed = tasks[future]
                log.exception("Failed to fetch feed %s (%s)", feed.name, feed.url)
                continue
            results.append(result or [])
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


def _summary_text(text: str) -> str:
    # Treat generated prose as text, not issue Markdown, mentions, or HTML.
    return html.escape(re.sub(r"([\\`*{}\[\]()#+.!_|>~-])", r"\\\1", text)).replace("@", "&#64;")


def render_markdown(
    articles: list[Article], period: str, now: datetime,
    part: int | None = None, total_parts: int = 1,
) -> str:
    start = now - PERIODS[period]
    title = "Daily" if period == "daily" else "Weekly"
    lines = [
        f"# {title} AI News Digest" + (f" - Part {part} of {total_parts}" if part else ""),
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
                if article.summary_status in ("ai", "partial"):
                    label = "AI summary" if article.summary_status == "ai" else "AI summary (partial article)"
                    lines.append(f"  **{label}:** {_summary_text(article.summary)}")
                    for point in article.key_points:
                        lines.append(f"  - {_summary_text(point)}")
                elif article.summary:
                    lines.append(f"  > **Publisher excerpt - full summary unavailable:** {_summary_text(article.summary)}")
                else:
                    lines.append("  > **Full summary unavailable.** Read the original article.")
            lines.append("")
    return "\n".join(lines)


def render_markdown_parts(
    articles: list[Article], period: str, now: datetime, max_chars: int = ISSUE_MAX_CHARS,
) -> list[tuple[str, int]]:
    """Split at article boundaries, counting the complete rendered Markdown."""
    whole = render_markdown(articles, period, now)
    if len(whole) <= max_chars:
        return [(whole, len(articles))]
    if not articles:
        raise ValueError("Issue limit is too small for the digest header")

    category_order = {category: index for index, category in enumerate(CATEGORY_TITLES)}
    ordered = sorted(articles, key=lambda a: (category_order[a.category], a.source.lower(), -a.published.timestamp()))
    groups: list[list[Article]] = []
    current: list[Article] = []
    # Reserve the longest possible part label before deciding boundaries.
    for article in ordered:
        candidate = current + [article]
        if len(render_markdown(candidate, period, now, len(articles), len(articles))) > max_chars:
            if not current:
                raise ValueError(f"Article cannot fit in an issue: {article.link}")
            groups.append(current)
            current = [article]
            if len(render_markdown(current, period, now, len(articles), len(articles))) > max_chars:
                raise ValueError(f"Article cannot fit in an issue: {article.link}")
        else:
            current = candidate
    if current:
        groups.append(current)
    return [
        (render_markdown(group, period, now, index, len(groups)), len(group))
        for index, group in enumerate(groups, 1)
    ]
