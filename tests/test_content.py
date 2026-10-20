from datetime import datetime, timezone

from news_aggregator.aggregator import Article, Feed, parse_feed
from news_aggregator.cache import JsonCache
from news_aggregator.content import extract_main_text, fill_contents

from .test_aggregator import FIXTURES

MESSY = (FIXTURES / "messy_article.html").read_text()
EMPTY = (FIXTURES / "empty_article.html").read_text()
WHEN = datetime(2026, 10, 3, tzinfo=timezone.utc)


def test_extract_main_text_keeps_article_and_drops_boilerplate():
    text = extract_main_text(MESSY)
    assert text.startswith("## Introducing a multimodal model\nToday we are releasing")
    assert "## What it can do" in text
    assert "Image understanding improved by 12 points" in text
    for junk in ("analytics", "newsletter", "cookies", "Share on", "Figure caption",
                 "client.generate", "Related posts", "Copyright", "Products and pricing", "Short\n"):
        assert junk not in text


def test_extract_main_text_caps_length_at_block_boundary():
    text = extract_main_text(MESSY, max_chars=150)
    assert len(text) <= 150
    assert text.endswith("single network.")


def test_extract_main_text_empty_pages():
    assert extract_main_text(EMPTY) == ""
    assert extract_main_text("") == ""
    assert extract_main_text("<html><body><p>tiny</p></body></html>") == ""


def test_full_text_from_feed_drops_code_and_references():
    articles = parse_feed(Feed("Full", "unused"), str(FIXTURES / "fulltext_rss.xml"))
    by_title = {a.title: a for a in articles}
    evals = by_title["Scaling agent evaluations"]
    assert evals.content_source == "feed"
    assert "## Why agent evals are hard" in evals.content
    assert "Inter-annotator agreement" in evals.content
    assert "def grade" not in evals.content
    assert "citation" not in evals.content
    assert evals.summary == "How we evaluate agents at scale."
    # A teaser in content:encoded is not treated as the full post.
    teaser = by_title["Short teaser post about our model"]
    assert teaser.content == "" and teaser.content_source == ""


def test_fill_contents_fetches_caches_and_falls_back(tmp_path):
    pages = {"https://lab.example.com/a/": MESSY, "https://lab.example.com/empty": EMPTY}
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        return pages[url]  # KeyError for the missing page -> logged and skipped

    articles = [
        Article("Lab", "Has feed text", "https://lab.example.com/feed", WHEN, "x", content="Feed text", content_source="feed"),
        Article("Lab", "Page", "https://lab.example.com/a/", WHEN, "Page excerpt"),
        Article("Lab", "JS only", "https://lab.example.com/empty", WHEN, "Client-rendered page."),
        Article("Lab", "Down", "https://lab.example.com/down", WHEN, "Down excerpt"),
    ]
    cache = JsonCache(tmp_path / "c.json")
    filled = fill_contents(articles, fetch, cache)
    assert [a.content_source for a in filled] == ["feed", "page", "excerpt", "excerpt"]
    assert filled[0].content == "Feed text"
    assert "multimodal model" in filled[1].content
    assert filled[2].content == "Client-rendered page."
    assert sorted(fetched) == ["https://lab.example.com/a/", "https://lab.example.com/down", "https://lab.example.com/empty"]

    # Cached by normalised URL: a rerun does not fetch the page again.
    cache.save()
    fetched.clear()
    again = fill_contents(articles[1:2], fetch, JsonCache(tmp_path / "c.json"))
    assert fetched == []
    assert again[0].content == filled[1].content


def test_json_cache_prunes_old_entries(tmp_path):
    cache = JsonCache(tmp_path / "c.json", max_age_days=0)
    cache.set("k", 1)
    cache._data["k"]["ts"] -= 10
    cache.save()
    assert JsonCache(tmp_path / "c.json").get("k") is None
