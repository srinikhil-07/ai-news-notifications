from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from news_aggregator import __main__ as cli
from news_aggregator.aggregator import (
    Article,
    Feed,
    classify,
    clean_text,
    digest_path,
    load_feeds,
    parse_feed,
    render_markdown,
    select_articles,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_load_repo_feeds_config():
    feeds = load_feeds(REPO_ROOT / "feeds.toml")
    assert feeds
    assert all(f.name and f.url.startswith("https://") for f in feeds)


def test_load_feeds_requires_name_and_url(tmp_path):
    cfg = tmp_path / "feeds.toml"
    cfg.write_text('[[feeds]]\nname = "x"\n')
    with pytest.raises(ValueError):
        load_feeds(cfg)


def test_parse_rss_skips_undated_and_cleans_summary():
    articles = parse_feed(Feed("Example", "unused"), str(FIXTURES / "rss.xml"))
    titles = [a.title for a in articles]
    assert "Post without a date" not in titles
    assert len(articles) == 4
    llm = articles[0]
    assert llm.summary == "Our LLM is faster & smarter."
    assert llm.published == datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def test_parse_feed_filters_non_ai_posts_for_general_blogs():
    articles = parse_feed(Feed("Example", "unused", ai_only=False), str(FIXTURES / "rss.xml"))
    titles = {a.title for a in articles}
    assert "Scaling our database fleet" not in titles
    assert "Agentic workflows for developers" in titles


def test_parse_atom():
    articles = parse_feed(Feed("Atom", "unused"), str(FIXTURES / "atom.xml"))
    assert [a.link for a in articles] == ["https://atom.example.com/diffusion"]


def test_parse_feed_uses_timeout_for_http_sources(monkeypatch):
    calls = []

    class FakeResponse:
        headers = SimpleNamespace(get_content_charset=lambda: "utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"<rss><channel><title>Example</title><item><title>hi</title><link>https://example.com/post</link><pubDate>Sun, 05 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>"

    def fake_urlopen(request, timeout):
        calls.append(timeout)
        assert request.full_url == "https://example.com/feed.xml"
        assert request.get_header("User-agent") == "ai-news-notifications/1.0 (+https://github.com/srinikhil-07/ai-news-notifications)"
        return FakeResponse()

    monkeypatch.setattr("news_aggregator.aggregator.urllib.request.urlopen", fake_urlopen)

    articles = parse_feed(Feed("Example", "unused"), "https://example.com/feed.xml")

    assert calls == [20.0]
    assert [a.title for a in articles] == ["hi"]


def test_select_daily_and_weekly_windows():
    articles = parse_feed(Feed("Example", "unused"), str(FIXTURES / "rss.xml"))
    daily = select_articles(articles, "daily", NOW)
    weekly = select_articles(articles, "weekly", NOW)
    assert [a.title for a in daily] == ["Introducing a new large language model"]
    assert [a.title for a in weekly] == [
        "Introducing a new large language model",
        "Scaling our database fleet",
        "Agentic workflows for developers",
    ]


def test_select_deduplicates_links():
    ts = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
    articles = [
        Article("A", "Post", "https://Example.com/post/", ts),
        Article("B", "Post", "https://example.com/post?utm_source=x", ts),
    ]
    assert len(select_articles(articles, "daily", NOW)) == 1


def test_digest_path():
    assert digest_path("out", "daily", NOW) == Path("out/daily/2026-10-04.md")
    assert digest_path("out", "weekly", NOW) == Path("out/weekly/2026-W40.md")


def test_render_markdown_groups_by_category_and_source():
    ts = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
    md = render_markdown(
        [
            Article("OpenAI", "New [model]", "https://o.ai/a", ts, "Summary", "product"),
            Article("DeepMind", "Launch", "https://d.ai/c", ts, "", "product"),
            Article("DeepMind", "Paper", "https://d.ai/b", ts, "", "research"),
        ],
        "daily",
        NOW,
    )
    assert md.startswith("# Daily AI News Digest")
    research = md.index("## Research")
    product = md.index("## Product & Announcements")
    assert research < md.index("[Paper]") < product
    assert product < md.index("### DeepMind", product) < md.index("[Launch]") < md.index("### OpenAI")
    assert r"[New \[model\]](https://o.ai/a) — 2026-10-04" in md
    assert "  > Summary" in md


def test_render_markdown_omits_empty_category():
    ts = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
    md = render_markdown([Article("A", "T", "https://a/1", ts, "", "product")], "daily", NOW)
    assert "## Research" not in md
    assert "## Product & Announcements" in md


def test_classify():
    auto = Feed("X", "u")
    assert classify(auto, "Introducing GPT-9", "Available today in the API") == "product"
    assert classify(auto, "Scaling laws", "A new paper on arXiv") == "research"
    assert classify(Feed("X", "u", category="research"), "Introducing GPT-9", "") == "research"


def test_load_feeds_validates_type_and_category(tmp_path):
    cfg = tmp_path / "feeds.toml"
    cfg.write_text('[[feeds]]\nname = "x"\nurl = "u"\ntype = "html"\n')
    with pytest.raises(ValueError, match="link_pattern"):
        load_feeds(cfg)
    cfg.write_text('[[feeds]]\nname = "x"\nurl = "u"\ncategory = "misc"\n')
    with pytest.raises(ValueError, match="category"):
        load_feeds(cfg)
    cfg.write_text('[[feeds]]\nname = "x"\nurl = "u"\ntype = "json"\n')
    with pytest.raises(ValueError, match="type"):
        load_feeds(cfg)


def test_render_markdown_empty():
    md = render_markdown([], "weekly", NOW)
    assert "# Weekly AI News Digest" in md
    assert "No new AI posts" in md


def test_clean_text_truncates():
    text = clean_text("word " * 200, max_chars=50)
    assert len(text) <= 50
    assert text.endswith("…")


def test_cli_writes_digest(tmp_path, monkeypatch):
    feeds = tmp_path / "feeds.toml"
    feeds.write_text(f'[[feeds]]\nname = "Example"\nurl = "{(FIXTURES / "rss.xml").as_posix()}"\n')
    gh_output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))

    rc = cli.main([
        "--period", "weekly",
        "--feeds", str(feeds),
        "--output-dir", str(tmp_path / "digests"),
        "--now", "2026-10-04T06:00:00",
    ])

    assert rc == 0
    out = tmp_path / "digests" / "weekly" / "2026-W40.md"
    assert "Agentic workflows for developers" in out.read_text()
    assert f"digest_path={out}" in gh_output.read_text()
    assert "article_count=3" in gh_output.read_text()
