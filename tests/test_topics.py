import re
from datetime import datetime, timezone

from news_aggregator.aggregator import Article
from news_aggregator.topics import article_id, article_tags, keyword_tags, merge_crossposts

WHEN = datetime(2026, 10, 3, tzinfo=timezone.utc)


def art(title, source="Lab", link=None, summary=None, category="product"):
    link = link or f"https://lab.example.com/{re.sub(r'[^a-z0-9]+', '-', title.lower())}"
    return Article(source, title, link, WHEN, f"{title} excerpt" if summary is None else summary, category)


def test_article_id_ignores_tracking_params():
    a = art("A", link="https://lab.example.com/post?utm_source=x")
    b = art("A", link="https://lab.example.com/post")
    assert article_id(a) == article_id(b) and len(article_id(a)) == 10


def test_keyword_tags():
    assert keyword_tags("Agentic coding agents and benchmarks") == ("agents", "coding", "evals")
    assert article_tags(art("New GPU kernels", summary="Faster inference")) == ("infra",)


def test_merge_crossposts_keeps_first_and_lists_other_sources():
    merged = merge_crossposts([
        art("Introducing Claude on Vertex AI", "Anthropic News"),
        art("Introducing Claude on Vertex AI | Google Cloud", "Google Cloud"),
        art("Introducing Claude for Chrome", "Anthropic News"),
    ])
    assert [a.title for a in merged] == ["Introducing Claude on Vertex AI", "Introducing Claude for Chrome"]
    assert merged[0].also_in == ("Google Cloud",)
