import json

import pytest

from news_aggregator import publisher


def test_publishes_whole_parts_and_reuses_existing_scheduled_issue(tmp_path, monkeypatch):
    first = tmp_path / "2026-W40.md"
    second = tmp_path / "2026-W40-part-2.md"
    first.write_text("# Weekly AI News Digest - Part 1 of 2\n\nArticle one\n")
    second.write_text("# Weekly AI News Digest - Part 2 of 2\n\nArticle two\n")
    calls = []
    first_title = "Weekly AI News Digest - Part 1 of 2 — 2026-W40"

    def gh(*args):
        calls.append(args)
        if args[0] == "api":
            return "User"
        if args[:2] == ("issue", "list"):
            return json.dumps([{"title": first_title, "url": "https://example.com/issues/1"}])
        return "https://example.com/issues/2"

    monkeypatch.setattr(publisher, "_gh", gh)
    publisher.publish([first, second], "owner", scheduled=True)
    creates = [args for args in calls if args[:2] == ("issue", "create")]
    assert len(creates) == 1
    assert creates[0][creates[0].index("--body-file") + 1] == str(second)
    assert "--assignee" in creates[0]
    assert second.read_text().endswith("Article two\n")


def test_manual_runs_do_not_skip_existing_issues_and_org_not_assigned(tmp_path, monkeypatch):
    path = tmp_path / "digest.md"
    path.write_text("# Digest\n")
    calls = []

    def gh(*args):
        calls.append(args)
        return "Organization" if args[0] == "api" else "https://example.com/1"

    monkeypatch.setattr(publisher, "_gh", gh)
    publisher.publish([path], "org", scheduled=False)
    assert not any(args[:2] == ("issue", "list") for args in calls)
    create = next(args for args in calls if args[:2] == ("issue", "create"))
    assert "--assignee" not in create


def test_oversized_body_fails_before_any_issue_is_created(tmp_path, monkeypatch):
    path = tmp_path / "digest.md"
    path.write_text("# Digest\n" + "漢" * 60_000)
    calls = []
    monkeypatch.setattr(publisher, "_gh", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="exceeds"):
        publisher.publish([path], "owner", scheduled=True)
    assert calls == []


def test_publisher_counts_characters_not_bytes(tmp_path, monkeypatch):
    path = tmp_path / "digest.md"
    path.write_text("# Digest\n" + "漢" * 30_000)
    calls = []
    monkeypatch.setattr(publisher, "_gh", lambda *args: calls.append(args) or "User")
    publisher.publish([path], "owner", scheduled=False)
    assert any(args[:2] == ("issue", "create") for args in calls)
