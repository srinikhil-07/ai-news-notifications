from datetime import datetime, timedelta, timezone

from news_aggregator.feedback import (
    EVENTS_FILE,
    PROFILE_FILE,
    build_profile,
    load_events,
    load_profile,
    parse_commands,
    parse_issue_body,
    process_issue,
    profile_prompt,
    sync,
)
from news_aggregator.ranking import Ranked, render_ranked
from news_aggregator.summarize import Triage

from .test_summarize import art

NOW = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
OWNER = {"srinikhil-07"}


def _body(*items):
    ranked = []
    for title, source, tags, tier in items:
        a = art(title, source)
        ranked.append(Ranked(a, f"{abs(hash(title)) % 16**10:010x}", Triage("gist", tags), tier=tier))
    return render_ranked(ranked, [], "weekly", NOW, {}), ranked


def _tick(body, ranked, index, signal):
    marker = f"<!-- fb:{ranked[index].id}:{signal} -->"
    return body.replace(f"- [ ] {'👍 more' if signal == 'up' else '👎 less'} like this {marker}",
                        f"- [x] {'👍 more' if signal == 'up' else '👎 less'} like this {marker}")


def test_rendered_body_round_trips_through_parser():
    body, ranked = _body(
        ("Agents [beta] launch", "Anthropic News", ("agents",), "top"),
        ("Policy update", "OpenAI", ("policy", "safety"), "middle"),
        ("Datacenter news", "Databricks", (), "low"),
    )
    body = _tick(body, ranked, 0, "up")
    body = _tick(body, ranked, 1, "down")
    articles, states = parse_issue_body(body)
    assert articles[ranked[0].id] == {"title": "Agents [beta] launch", "source": "Anthropic News", "tags": ["agents"]}
    assert articles[ranked[1].id]["tags"] == ["policy", "safety"]
    assert states == {ranked[0].id: "up", ranked[1].id: "down", ranked[2].id: "none"}
    # Both boxes ticked is neutral.
    assert parse_issue_body(_tick(body, ranked, 0, "down"))[1][ranked[0].id] == "none"


def test_parse_commands():
    text = "thanks!\n/more agents\n/LESS source:Databricks\n/mute topic:Hiring News\n/reset\n/more\nnot /mute x"
    assert parse_commands(text) == [
        ("more", "topic:agents"), ("less", "source:Databricks"), ("mute", "topic:hiring-news"), ("reset", None),
    ]
    assert parse_commands("/reset topic:agents") == [("reset", "topic:agents")]
    assert parse_commands("/more <script>") == []


def _issue(body, labels=("ai-news",), number=7, author="github-actions[bot]"):
    return {"number": number, "body": body, "labels": [{"name": n} for n in labels], "user": {"login": author}}


def test_process_issue_is_idempotent_and_owner_only():
    body, ranked = _body(("Agents launch", "Lab", ("agents",), "top"), ("Other", "Lab", (), "middle"))
    body = _tick(body, ranked, 0, "up")
    comments = [
        {"id": 1, "body": "/more evals", "user": {"login": "srinikhil-07"}, "created_at": "2026-10-18T09:00:00Z"},
        {"id": 2, "body": "/mute topic:agents", "user": {"login": "stranger"}, "created_at": "2026-10-18T09:00:00Z"},
    ]
    issue = _issue(body, labels=("ai-news", "too-long"))
    events = process_issue(issue, comments, [], OWNER, NOW)
    kinds = sorted((e["type"], e.get("signal") or e.get("action") or e.get("label")) for e in events)
    assert kinds == [("article", "up"), ("label", "too-long"), ("rule", "more")]
    assert process_issue(issue, comments, events, OWNER, NOW) == []

    # Unticking records a "none" event; removing the label records it too.
    issue2 = _issue(body.replace("- [x]", "- [ ]"), labels=("ai-news",))
    later = process_issue(issue2, comments, events, OWNER, NOW)
    assert sorted((e["type"], e.get("signal", e.get("present"))) for e in later) == [("article", "none"), ("label", False)]


def test_process_issue_ignores_other_issues_and_foreign_bodies():
    body, ranked = _body(("Agents launch", "Lab", ("agents",), "top"))
    body = _tick(body, ranked, 0, "up")
    assert process_issue(_issue(body, labels=("bug",)), [], [], OWNER, NOW) == []
    assert process_issue(_issue(body, author="stranger"), [], [], OWNER, NOW) == []


def _ev(days_ago, **fields):
    ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ts": ts, "issue": 1, **fields}


def test_build_profile_decay_rules_reset_and_labels():
    events = [
        _ev(1, type="article", id="a", signal="up", title="Agents A", source="Lab", tags=["agents"]),
        _ev(15, type="article", id="b", signal="down", title="Infra B", source="Cloud", tags=["infra", "agents"]),
        _ev(20, type="article", id="c", signal="up", title="Reset me", source="Old", tags=["policy"]),
        _ev(18, type="rule", action="mute", target="topic:hiring"),
        _ev(17, type="rule", action="reset", target=None),
        _ev(2, type="rule", action="less", target="source:Databricks"),
        _ev(2, type="rule", action="more", target="topic:evals"),
        _ev(1, type="rule", action="reset", target="topic:evals"),
        _ev(3, type="label", label="too-long", present=True),
        _ev(3, type="label", label="too-much-product", present=True),
    ]
    profile = build_profile(events, NOW, "LLM evals")
    assert profile["sources"] == {"Cloud": -0.64, "Lab": 1.0}  # 2 weeks old -> 0.8**2
    assert profile["topics"] == {"agents": 0.36, "infra": -0.64}
    assert profile["rules"] == {"source:Databricks": "less"}  # mute cleared by /reset, evals reset
    assert profile["format"] == {"top_n_delta": -3}
    assert profile["categories"] == {"product": -1.0}
    assert profile["signals"] == 4
    assert profile["liked_titles"] == ["Agents A"] and profile["disliked_titles"] == ["Infra B"]
    prompt = profile_prompt(profile)
    assert "Interests: LLM evals" in prompt
    assert "Likes: agents, Lab" in prompt
    assert "Dislikes: infra, Cloud, Databricks" in prompt


class FakeClient:
    def __init__(self, issues, comments):
        self.issues, self._comments = issues, comments

    def issue(self, number):
        return next(i for i in self.issues if i["number"] == number)

    def recent_issues(self, since):
        return self.issues

    def comments(self, number):
        return self._comments.get(number, [])


def test_sync_appends_events_and_keeps_interests(tmp_path):
    body, ranked = _body(("Agents launch", "Lab", ("agents",), "top"))
    client = FakeClient([_tick_issue(body, ranked)], {7: [
        {"id": 5, "body": "/less source:Databricks", "user": {"login": "SriNikhil-07"}, "created_at": "2026-10-18T09:00:00Z"},
    ]})
    (tmp_path / PROFILE_FILE).write_text('{"interests": "agents and evals"}')
    sync(client, tmp_path, OWNER, NOW)
    sync(client, tmp_path, OWNER, NOW, issue_number=7)
    assert len(load_events(tmp_path / EVENTS_FILE)) == 2
    profile = load_profile(tmp_path)
    assert profile["interests"] == "agents and evals"
    assert profile["sources"] == {"Lab": 1.0}
    assert profile["rules"] == {"source:Databricks": "less"}


def _tick_issue(body, ranked):
    return _issue(_tick(body, ranked, 0, "up"))
