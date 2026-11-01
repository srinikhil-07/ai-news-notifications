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
    fetch_all,
    load_feeds,
    parse_feed,
    render_markdown,
    render_markdown_parts,
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


def test_fetch_all_skips_broken_feeds_but_keeps_others():
    ok = Feed("Good", "https://good.example.com/feed", type="rss")
    broken = Feed("Broken", "https://broken.example.com/feed", type="rss")

    def fake_fetch(feed: Feed):
        if feed.name == "Good":
            return [Article(feed.name, "Good article", "https://good.example.com/post", datetime(2026, 10, 3, tzinfo=timezone.utc))]
        raise ValueError("site down")

    import news_aggregator.aggregator as aggregator_module

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(aggregator_module, "fetch_feed", fake_fetch)
    try:
        articles = fetch_all([ok, broken], max_workers=2)
    finally:
        monkeypatch.undo()

    assert [a.title for a in articles] == ["Good article"]


def test_parse_feed_filters_non_ai_posts_for_general_blogs():
    articles = parse_feed(Feed("Example", "unused", ai_only=False), str(FIXTURES / "rss.xml"))
    titles = {a.title for a in articles}
    assert "Scaling our database fleet" not in titles
    assert "Agentic workflows for developers" in titles


def test_parse_atom():
    articles = parse_feed(Feed("Atom", "unused"), str(FIXTURES / "atom.xml"))
    assert [a.link for a in articles] == ["https://atom.example.com/diffusion"]


@pytest.mark.parametrize("content_type", ["html", "text"])
def test_parse_atom_retains_full_content_not_only_excerpt(content_type):
    text = "The paper evaluates models in controlled experiments. " * 40
    payload = f"""
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Lab</title><id>https://example.com</id>
      <entry><title>Research</title><id>https://example.com/paper</id>
        <link href="https://example.com/paper"/>
        <updated>2026-10-03T12:00:00Z</updated>
        <summary>Publisher excerpt.</summary>
        <content type="{content_type}">{text}</content>
      </entry>
    </feed>"""
    article = parse_feed(Feed("Lab", "unused"), payload)[0]
    assert article.summary == "Publisher excerpt."
    assert len(article.content.split()) >= 80
    assert "controlled experiments" in article.content


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
    assert "Copilot" not in md
    assert "Publisher excerpt - full summary unavailable:** Summary" in md
    assert "Full summary unavailable." in md
    assert "Safari" not in md


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
    assert "Safari" not in md


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


def test_render_ai_summary_and_safe_plain_text():
    article = Article(
        "Lab", "Model", "https://example.com/post", NOW,
        "A new model.", key_points=("Available today.", "<script>@owner **ignore**</script>"),
        summary_status="ai",
    )
    md = render_markdown([article], "weekly", NOW)
    assert "**AI summary:** A new model\\." in md
    assert "  - Available today\\." in md
    assert "<script>" not in md
    assert "@owner" not in md
    assert "Publisher excerpt" not in md


def test_partial_article_label_is_rendered():
    article = Article(
        "Lab", "Post", "https://example.com/post", NOW,
        "Takeaway", key_points=("One", "Two"), summary_status="partial",
    )
    assert "**AI summary (partial article):**" in render_markdown([article], "weekly", NOW)


def test_render_parts_exact_character_limits_and_preserves_every_article():
    articles = [
        Article(
            "Lab", f"Post {i}", f"https://example.com/{i}", NOW,
            "漢" * 280, category="research" if i % 2 else "product",
        )
        for i in range(250)
    ]
    parts = render_markdown_parts(articles, "weekly", NOW)
    assert len(parts) >= 2
    assert sum(count for _, count in parts) == len(articles)
    assert all(len(body) <= 60_000 for body, _ in parts)
    combined = "".join(body for body, _ in parts)
    assert all(combined.count(f"](https://example.com/{i})") == 1 for i in range(250))
    for index, (body, _) in enumerate(parts, 1):
        assert body.startswith(f"# Weekly AI News Digest - Part {index} of {len(parts)}")
    # The limit counts Unicode characters, not UTF-8 bytes.
    assert any(len(body.encode("utf-8")) > 60_000 for body, _ in parts)


def test_render_parts_at_boundary_and_oversized_single_article():
    article = Article("Lab", "Post", "https://example.com/a", NOW, "excerpt")
    whole = render_markdown([article], "weekly", NOW)
    assert render_markdown_parts([article], "weekly", NOW, len(whole)) == [(whole, 1)]
    with pytest.raises(ValueError, match="cannot fit"):
        render_markdown_parts([article], "weekly", NOW, len(whole) - 1)


def test_cli_summarizes_after_selection_and_writes_all_parts(tmp_path, monkeypatch):
    articles = [
        Article("Lab", f"Post {i}", f"https://example.com/{i}", NOW, "漢" * 280)
        for i in range(250)
    ]
    old = Article("Lab", "Old", "https://example.com/old", NOW.replace(year=2025))
    calls = []

    class FakeSummarizer:
        def __init__(self, *args):
            pass

        def summarize_all(self, selected):
            calls.extend(selected)
            return selected

    monkeypatch.setattr(cli, "Summarizer", FakeSummarizer)
    monkeypatch.setattr(cli, "load_feeds", lambda _: [])
    monkeypatch.setattr(cli, "fetch_all", lambda _: articles + [old, articles[0]])
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert cli.main([
        "--period", "weekly", "--now", NOW.isoformat(), "--summarize",
        "--output-dir", str(tmp_path / "digests"),
    ]) == 0
    assert len(calls) == 250
    paths = list((tmp_path / "digests" / "weekly").glob("*.md"))
    assert len(paths) >= 2
    assert (tmp_path / "digests" / "weekly" / "2026-W40-part-2.md").exists()
    assert all(len(path.read_text()) <= 60_000 for path in paths)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(outputs["nonempty_digest_paths"].split()) == {str(path) for path in paths}
    assert outputs["article_count"] == "250"


def test_cli_api_failure_does_not_mark_digest_delivered(tmp_path, monkeypatch):
    class FailedSummarizer:
        def __init__(self, *args):
            pass

        def summarize_all(self, articles):
            raise RuntimeError("quota exhausted")

    monkeypatch.setattr(cli, "Summarizer", FailedSummarizer)
    monkeypatch.setattr(cli, "load_feeds", lambda _: [])
    monkeypatch.setattr(cli, "fetch_all", lambda _: [Article("A", "Post", "https://example.com/a", NOW)])
    assert cli.main([
        "--period", "weekly", "--now", NOW.isoformat(), "--summarize",
        "--output-dir", str(tmp_path / "digests"),
    ]) == 1
    assert not list(tmp_path.rglob("*.md"))


def test_cli_summary_key_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit) as error:
        cli.main(["--period", "weekly", "--summarize", "--output-dir", str(tmp_path)])
    assert error.value.code == 2
