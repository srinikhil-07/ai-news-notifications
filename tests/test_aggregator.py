from datetime import datetime, timezone
from pathlib import Path

import pytest

from news_aggregator import __main__ as cli
from news_aggregator.aggregator import (
    Article,
    Feed,
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


def test_render_markdown_groups_by_source():
    ts = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
    md = render_markdown(
        [
            Article("OpenAI", "New [model]", "https://o.ai/a", ts, "Summary"),
            Article("DeepMind", "Research", "https://d.ai/b", ts),
        ],
        "daily",
        NOW,
    )
    assert md.startswith("# Daily AI News Digest")
    assert md.index("## DeepMind") < md.index("## OpenAI")
    assert r"[New \[model\]](https://o.ai/a) — 2026-10-04" in md
    assert "  > Summary" in md


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
