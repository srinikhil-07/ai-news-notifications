"""Full article text: from the feed, from the article page, or the excerpt.

Text is looked up in this order:

1. The full post embedded in the feed (``content:encoded`` / Atom ``<content>``).
2. The article page, reduced to its main text by :func:`extract_main_text`.
   For ``html`` feeds the page is already downloaded while scraping.
3. The feed/metadata excerpt, marked with ``content_source = "excerpt"``.

Only articles that made it into the digest are fetched (see :func:`fill_contents`).
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from html.parser import HTMLParser
from typing import Callable, Iterable

from .cache import JsonCache

log = logging.getLogger(__name__)

# Roughly 4k tokens: enough for the lead, headings and key paragraphs.
MAX_CONTENT_CHARS = 16000
# Feed content shorter than this is treated as an excerpt, not the full post.
MIN_FULL_TEXT_CHARS = 500

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
SKIP_TAGS = {
    "script", "style", "noscript", "nav", "header", "footer", "aside", "form", "pre",
    "code", "figure", "svg", "button", "iframe", "template", "select", "dialog", "math",
}
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "dd", "dt", "td", "div", "section", "article", "main", "br"}
HEADING_TAGS = {"h1", "h2", "h3", "h4"}
BOILERPLATE_ATTR = re.compile(
    r"(^|[\s_-])(nav|navbar|menu|footer|sidebar|share|sharing|social|cookie|consent|newsletter|"
    r"related|comments?|breadcrumbs?|subscribe|banner|promo|advert|ads|popup|modal|toc)($|[\s_-])",
    re.IGNORECASE,
)
STOP_HEADINGS = re.compile(
    r"^(references|bibliography|citations?|footnotes|notes|acknowledge?ments|related (posts|articles|reading)|"
    r"read more|more from .*|further reading)\s*:?$",
    re.IGNORECASE,
)
MIN_BLOCK_CHARS = 30


class _MainTextParser(HTMLParser):
    def __init__(self, region_only: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.region_only = region_only
        self.blocks: list[tuple[str, str]] = []  # (kind, text)
        self._stack: list[tuple[str, bool, bool]] = []  # (tag, skipping, region)
        self._buf: list[str] = []
        self._link_chars = 0
        self._block_kind = "p"
        self._stopped = False

    @property
    def _skipping(self) -> bool:
        return bool(self._stack) and self._stack[-1][1]

    @property
    def _in_region(self) -> bool:
        return not self.region_only or (bool(self._stack) and self._stack[-1][2])

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        link_chars = self._link_chars
        self._buf, self._link_chars = [], 0
        if not text or self._stopped:
            return
        if self._block_kind == "h":
            if STOP_HEADINGS.match(text):
                self._stopped = True
                return
            self.blocks.append(("h", text))
            return
        if len(text) < MIN_BLOCK_CHARS or link_chars > 0.5 * len(text):
            return
        self.blocks.append(("p", text))

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            self._flush()
            self._block_kind = "h" if tag in HEADING_TAGS else "p"
        if tag in VOID_TAGS:
            return
        attrs = dict(attrs)
        parent_skip = self._skipping
        parent_region = self._in_region if self.region_only else True
        marker = f"{attrs.get('class') or ''} {attrs.get('id') or ''} {attrs.get('role') or ''}"
        skip = parent_skip or tag in SKIP_TAGS or bool(BOILERPLATE_ATTR.search(marker))
        region = parent_region if not self.region_only else (parent_region or tag in ("article", "main"))
        self._stack.append((tag, skip, region))
        if tag == "a":
            self._link_start = len("".join(self._buf))

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self._block_kind = "p"
        if tag == "a" and hasattr(self, "_link_start"):
            self._link_chars += max(0, len("".join(self._buf)) - self._link_start)
            del self._link_start
        # Pop up to and including the matching tag; ignore stray end tags.
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._skipping or not self._in_region or self._stopped:
            return
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _join_blocks(blocks: list[tuple[str, str]], max_chars: int) -> str:
    out: list[str] = []
    size = 0
    for kind, text in blocks:
        line = f"## {text}" if kind == "h" else text
        if size + len(line) > max_chars:
            if not out:
                out.append(line[:max_chars])
            break
        out.append(line)
        size += len(line) + 1
    # Drop trailing headings without body text.
    while out and out[-1].startswith("## "):
        out.pop()
    if not any(not line.startswith("## ") for line in out):
        return ""
    return "\n".join(out)


def extract_main_text(page: str, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Return the readable main text of an HTML page or fragment.

    Navigation, headers/footers, sidebars, code blocks, figures and reference
    sections are dropped. Content inside ``<article>``/``<main>`` is preferred.
    Headings are kept (prefixed with ``## ``) to preserve the article structure.
    """
    if not page or not page.strip():
        return ""
    lowered = page.lower()
    attempts = [True, False] if ("<article" in lowered or "<main" in lowered) else [False]
    for region_only in attempts:
        parser = _MainTextParser(region_only)
        try:
            parser.feed(page)
            parser.close()
        except Exception:  # noqa: BLE001 - malformed HTML: use whatever was parsed
            log.debug("HTML parse error", exc_info=True)
        text = _join_blocks(parser.blocks, max_chars)
        if len(text) >= 200 or (text and not region_only):
            return text
    return ""


def feed_full_text(entry, summary: str = "", max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Return the full post embedded in a feedparser entry, or ``""``."""
    best = ""
    for item in (entry or {}).get("content", []) or []:
        value = item.get("value", "") if hasattr(item, "get") else ""
        if not value:
            continue
        if "<" in value:
            text = extract_main_text(value, max_chars)
        else:
            text = re.sub(r"[ \t]+", " ", html_lib.unescape(value)).strip()[:max_chars]
        if len(text) > len(best):
            best = text
    if len(best) < MIN_FULL_TEXT_CHARS or len(best) <= len(summary or "") + 100:
        return ""
    return best


def fill_contents(
    articles: Iterable,
    fetch: Callable[[str], str],
    cache: JsonCache | None = None,
    max_workers: int = 8,
) -> list:
    """Return the articles with ``content`` filled in.

    Articles that already carry feed/page text are kept. Others are looked up
    in the cache by normalised URL, then fetched in parallel. If nothing usable
    is found, the excerpt (``summary``) is used and ``content_source`` is set to
    ``"excerpt"``. Fetch errors are logged and never raised.
    """
    from .aggregator import normalize_link

    cache = cache if cache is not None else JsonCache()
    articles = list(articles)
    result = list(articles)
    todo: list[int] = []
    for i, article in enumerate(articles):
        if article.content:
            result[i] = replace(article, content=article.content[:MAX_CONTENT_CHARS])
            continue
        cached = cache.get(f"text:{normalize_link(article.link)}")
        if cached:
            result[i] = replace(article, content=cached, content_source="page")
        else:
            todo.append(i)

    def load(i: int) -> str:
        link = articles[i].link
        try:
            return extract_main_text(fetch(link))
        except Exception:  # noqa: BLE001 - one broken page must not break the digest
            log.warning("Could not fetch article text for %s", link, exc_info=True)
            return ""

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(todo)))) as pool:
            texts = list(pool.map(load, todo))
        for i, text in zip(todo, texts):
            article = articles[i]
            if text:
                cache.set(f"text:{normalize_link(article.link)}", text)
                result[i] = replace(article, content=text, content_source="page")
            else:
                result[i] = replace(article, content=article.summary, content_source="excerpt")
    return result
