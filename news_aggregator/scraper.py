"""Minimal HTML scraper for blogs that do not publish an RSS/Atom feed.

A listing page is fetched, article links are selected with a regular
expression, and each article page is fetched to read its title, summary and
publication date from standard metadata (Open Graph / ``article:*`` meta tags,
JSON-LD ``datePublished`` or ``<time datetime>``).
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 20

DATE_META_KEYS = (
    "article:published_time",
    "og:published_time",
    "datepublished",
    "publish-date",
    "publish_date",
    "pubdate",
    "date",
    "dc.date",
    "parsely-pub-date",
)


def fetch_url(url: str, user_agent: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - URLs come from config
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read(MAX_BYTES).decode(charset, errors="replace")


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.time_datetimes: list[str] = []
        self.json_ld: list[str] = []
        self.title = ""
        self._in_title = False
        self._in_json_ld = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
            content = attrs.get("content")
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "time" and attrs.get("datetime"):
            self.time_datetimes.append(attrs["datetime"])
        elif tag == "title":
            self._in_title = True
            self._buffer = []
        elif tag == "script" and (attrs.get("type") or "").lower() == "application/ld+json":
            self._in_json_ld = True
            self._buffer = []

    def handle_data(self, data):
        if self._in_title or self._in_json_ld:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            if not self.title:
                self.title = "".join(self._buffer).strip()
        elif tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            self.json_ld.append("".join(self._buffer))


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def extract_links(html: str, base_url: str, link_pattern: str) -> list[str]:
    """Return unique absolute links on the page that fully match ``link_pattern``."""
    parser = _LinkParser()
    parser.feed(html)
    pattern = re.compile(link_pattern)
    base = _canonical(base_url).rstrip("/")
    seen: set[str] = set()
    links = []
    for href in parser.hrefs:
        url = _canonical(urljoin(base_url, href))
        if url.rstrip("/") == base or url in seen or not pattern.fullmatch(url):
            continue
        seen.add(url)
        links.append(url)
    return links


def parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _json_ld_dates(blob: str) -> list[str]:
    try:
        data = json.loads(blob)
    except ValueError:
        return []
    found: list[str] = []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("datePublished"), str):
                found.append(node["datePublished"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def extract_article(html: str) -> tuple[str, str, datetime | None]:
    """Return ``(title, summary, published)`` from an article page."""
    parser = _MetaParser()
    parser.feed(html)
    meta = parser.meta

    title = meta.get("og:title") or meta.get("twitter:title") or parser.title
    summary = meta.get("og:description") or meta.get("description") or meta.get("twitter:description") or ""

    candidates = [meta[k] for k in DATE_META_KEYS if k in meta]
    for blob in parser.json_ld:
        candidates.extend(_json_ld_dates(blob))
    candidates.extend(parser.time_datetimes)

    published = None
    for candidate in candidates:
        published = parse_date(candidate)
        if published:
            break
    return title, summary, published
