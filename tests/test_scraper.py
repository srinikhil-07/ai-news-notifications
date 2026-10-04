from datetime import datetime, timezone

from news_aggregator.aggregator import Feed, scrape_feed
from news_aggregator.scraper import extract_article, extract_links, parse_date

from .test_aggregator import FIXTURES

BASE = "https://lab.example.com/news"
PATTERN = r"https://lab\.example\.com/news/[^/]+/?"

PAGES = {
    BASE: (FIXTURES / "listing.html").read_text(),
    f"{BASE}/claude-new-model": """
        <html><head>
          <meta property="og:title" content="Introducing our new model &amp; API">
          <meta property="og:description" content="A faster, cheaper model for developers.">
          <meta property="article:published_time" content="2026-10-03T16:00:00Z">
        </head></html>""",
    f"{BASE}/interpretability-paper": """
        <html><head><title>Tracing thoughts in a language model</title>
          <meta name="description" content="New interpretability research.">
          <script type="application/ld+json">
            {"@context": "https://schema.org", "@graph": [{"@type": "Article", "datePublished": "2026-10-02"}]}
          </script>
        </head></html>""",
    f"{BASE}/old-post": """
        <html><head><title>Old AI post</title></head>
        <body><time datetime="2025-01-01T00:00:00+00:00">Jan 1</time></body></html>""",
    f"{BASE}/no-date": "<html><head><title>Undated AI post</title></head></html>",
}


def test_extract_links_filters_and_dedupes():
    links = extract_links(PAGES[BASE], BASE, PATTERN)
    assert links == [
        f"{BASE}/claude-new-model",
        f"{BASE}/interpretability-paper",
        f"{BASE}/old-post",
        f"{BASE}/no-date",
    ]


def test_extract_article_meta_sources():
    title, summary, published = extract_article(PAGES[f"{BASE}/claude-new-model"])
    assert title == "Introducing our new model & API"
    assert summary == "A faster, cheaper model for developers."
    assert published == datetime(2026, 10, 3, 16, tzinfo=timezone.utc)

    title, _, published = extract_article(PAGES[f"{BASE}/interpretability-paper"])
    assert title == "Tracing thoughts in a language model"
    assert published == datetime(2026, 10, 2, tzinfo=timezone.utc)

    _, _, published = extract_article(PAGES[f"{BASE}/old-post"])
    assert published == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_parse_date_formats():
    assert parse_date("Sat, 03 Oct 2026 12:00:00 GMT") == datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    assert parse_date("2026-10-03T14:00:00+02:00") == datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    assert parse_date("not a date") is None
    assert parse_date("") is None


def test_scrape_feed_builds_categorized_articles():
    feed = Feed("Lab", BASE, type="html", link_pattern=PATTERN)
    articles = scrape_feed(feed, fetch=PAGES.__getitem__)
    by_title = {a.title: a for a in articles}
    assert set(by_title) == {
        "Introducing our new model & API",
        "Tracing thoughts in a language model",
        "Old AI post",
    }
    assert by_title["Introducing our new model & API"].category == "product"
    assert by_title["Tracing thoughts in a language model"].category == "research"


def test_scrape_feed_limits_page_fetches_and_skips_broken_pages():
    pages = dict(PAGES)
    del pages[f"{BASE}/claude-new-model"]  # fetch raises KeyError -> skipped
    feed = Feed("Lab", BASE, type="html", link_pattern=PATTERN, max_articles=2)
    articles = scrape_feed(feed, fetch=pages.__getitem__)
    assert [a.title for a in articles] == [
        "Tracing thoughts in a language model",
    ]


def test_scrape_feed_keyword_filter_for_mixed_blogs():
    feed = Feed("Lab", BASE, type="html", link_pattern=PATTERN, ai_only=False)
    titles = {a.title for a in scrape_feed(feed, fetch=PAGES.__getitem__)}
    assert "Introducing our new model & API" not in titles
    assert "Old AI post" in titles
