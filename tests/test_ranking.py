from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import unquote

from news_aggregator import __main__ as cli
from news_aggregator.aggregator import COPILOT_PROMPT, copilot_summary_url
from news_aggregator.feedback import parse_issue_body
from news_aggregator.ranking import (
    assign_tiers,
    footer_line,
    render_issue_body,
    render_ranked,
    score_article,
)
from news_aggregator.topics import article_id

from .test_aggregator import FIXTURES
from .test_topics import art

NOW = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)


def test_score_combines_profile_and_rules():
    a = art("Agent evals", "Lab")
    profile = {"topics": {"agents": 2.0, "infra": -2.0}, "categories": {"product": -2.0}}
    base, muted = score_article(a, (), {})
    assert base == 0 and not muted
    liked, _ = score_article(a, ("agents",), {**profile, "sources": {"lab": 2.0}})
    disliked, _ = score_article(a, ("infra",), profile)
    assert liked > base > disliked
    boosted, _ = score_article(a, ("evals",), {"rules": {"topic:evals": "more"}})
    assert boosted == 1.5
    _, muted = score_article(a, ("evals",), {"rules": {"source:Lab": "mute"}})
    assert muted


def _articles(n, source="Lab"):
    return [art(f"{source} post {i}", source) for i in range(n)]


def test_tiers_without_feedback():
    articles = _articles(50)
    ranked = assign_tiers(articles, {}, top_n=12, middle_n=30)
    tiers = [r.tier for r in ranked]
    assert tiers.count("top") == 12 and tiers.count("middle") == 30 and tiers.count("low") == 8
    assert not any(r.exploring for r in ranked)


def test_tiers_with_feedback_explore_mute_and_format_delta():
    liked = _articles(20, "Liked")
    unseen = [art("Unseen post", "Brand New Lab")]
    muted = [art("Muted post", "Databricks")]
    articles = liked + unseen + muted
    profile = {"signals": 5, "sources": {"Liked": 3.0}, "topics": {"agents": 1.0},
               "rules": {"source:Databricks": "mute"}, "format": {"top_n_delta": -4}}
    ranked = assign_tiers(articles, profile, top_n=12, middle_n=5)
    top = [r for r in ranked if r.tier == "top"]
    assert len(top) == 8  # 12 - 4 from the too-long label
    assert [r.article.title for r in top if r.exploring] == ["Unseen post"]
    assert {r.article.title: r.tier for r in ranked}["Muted post"] == "low"
    assert len(ranked) == len(articles)  # nothing is dropped


def test_render_tiers_and_footer():
    articles = _articles(6)
    articles[0] = art("New agents for coding", summary="Agents write code.")
    ranked = assign_tiers(articles, {}, top_n=3, middle_n=2)
    md = render_ranked(ranked, "weekly", NOW, {})
    assert md.index("## ⭐ Top picks") < md.index("## Product & Announcements")
    assert " · `agents` `coding` · [✨ Summarise with Copilot](https://github.com/copilot?prompt=" in md
    assert "  > Agents write code." in md
    assert "<details>\n<summary>1 more article(s), lightly summarised</summary>" in md
    assert "No feedback yet" in md
    assert md.count("👍 more like this") == 6
    assert md.count("✨ Summarise with Copilot") == 6


def test_footer_transparency():
    profile = {"signals": 14, "topics": {"agents": 1.2, "infra": -0.9}, "sources": {"Anthropic Research": 0.8},
               "rules": {"source:Databricks": "mute", "topic:evals": "more"}}
    assert footer_line(profile) == (
        "_Ranked using 14 feedback signals; boosted: agents, Anthropic Research, evals; "
        "reduced: infra; muted: Databricks._"
    )


def test_issue_body_fits_budget_and_keeps_feedback_markers():
    articles = [art(f"Article with a fairly long title number {i}", summary="S " * 140) for i in range(100)]
    ranked = assign_tiers(articles, {}, top_n=12, middle_n=40)
    full = render_ranked(ranked, "weekly", NOW, {})
    url = "https://github.com/o/r/blob/main/digests/weekly/2026-W42.md"
    body = render_issue_body(ranked, "weekly", NOW, {}, url, max_chars=45000)
    assert len(full) > 45000 >= len(body)
    assert "[full digest](" + url + ")" in body
    assert len(parse_issue_body(body)[0]) == 100
    shortest = render_ranked(ranked, "weekly", NOW, {}, level=2)
    assert shortest.count("✨ Summarise with Copilot") == 52  # top + middle only
    tiny = render_issue_body(ranked, "weekly", NOW, {}, url, max_chars=5000)
    assert len(tiny) <= 5000 and "Truncated" in tiny
    assert tiny.count("<details>") == tiny.count("</details>")


def test_cli_ranked_mode(tmp_path, monkeypatch):
    feeds = tmp_path / "feeds.toml"
    feeds.write_text(f'[[feeds]]\nname = "Lab"\nurl = "{(FIXTURES / "rss.xml").as_posix()}"\n')
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.chdir(tmp_path)
    assert cli.main([
        "--period", "weekly", "--ranked", "--feeds", str(feeds), "--now", "2026-10-04T08:10:00",
        "--output-dir", "digests", "--feedback-dir", str(tmp_path / "fb"), "--issue-dir", str(tmp_path / "issues"),
    ]) == 0
    md = (tmp_path / "digests" / "weekly" / "2026-W40.md").read_text()
    assert "## ⭐ Top picks" in md and "✨ Summarise with Copilot" in md and "<!-- fb:" in md
    assert (tmp_path / "issues" / "2026-W40.md").read_text() == md  # small digest fits unchanged


def test_issue_link_only_for_repo_relative_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    assert cli._full_digest_url(Path("digests/weekly/x.md")) == "https://github.com/o/r/blob/main/digests/weekly/x.md"
    assert cli._full_digest_url(tmp_path / "x.md") == ""


def test_copilot_summary_url_encodes_article_link():
    link = "https://example.com/post?id=1&ref=feed#top"
    url = copilot_summary_url(link)
    assert url.startswith("https://github.com/copilot?prompt=Read%20https://example.com/post%3Fid%3D1%26ref%3Dfeed%23top%20")
    assert unquote(url.split("?prompt=", 1)[1]) == COPILOT_PROMPT.format(link=link)
    assert not set(url) & set(" ()[]<>\"&#")


def test_copilot_link_keeps_feedback_parseable():
    a = art("Agents (part 1)", link="https://example.com/a?id=1&x=y")
    md = render_ranked(assign_tiers([a], {}, top_n=1), "weekly", NOW, {})
    assert "[✨ Summarise with Copilot](https://github.com/copilot?prompt=Read%20https://example.com/a%3Fid%3D1%26x%3Dy%20" in md
    items = parse_issue_body(md)[0]
    assert list(items) == [article_id(a)]
    assert items[article_id(a)]["title"] == "Agents (part 1)"
