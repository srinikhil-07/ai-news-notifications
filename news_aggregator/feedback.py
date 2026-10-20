"""Reader feedback collected on GitHub, stored in the repository.

Feedback comes from the ``ai-news`` digest issues:

* **Checkboxes** under each article (``👍 more like this`` / ``👎 less like this``).
  Each article line carries a hidden ``<!-- a:ID -->`` marker and each checkbox a
  ``<!-- fb:ID:up|down -->`` marker, so the issue body can be read back.
* **Slash commands** in comments: ``/more agents``, ``/less source:Databricks``,
  ``/mute topic:hiring``, ``/reset`` (everything) or ``/reset topic:agents``.
* **Format labels** on the issue: ``too-long``, ``too-short``,
  ``too-much-product``, ``too-much-research``.

Only the repository owner's comments (plus ``FEEDBACK_USERS``) are counted.
Every change is appended to ``feedback/events.jsonl``; ``feedback/profile.json``
is rebuilt from the log with weekly exponential decay. The ``interests`` field
in ``profile.json`` is free text you can edit to seed the profile.

Run ``python -m news_aggregator.feedback sync`` to read recent issues (the
digest workflow does this first), or ``--issue N`` for one issue (feedback.yml).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

LABEL = "ai-news"
BOT_LOGINS = ("github-actions[bot]",)
DECAY_PER_WEEK = 0.8
MAX_COMMANDS_PER_COMMENT = 20
FORMAT_LABELS = ("too-long", "too-short", "too-much-product", "too-much-research")
ACTIONS = ("more", "less", "mute", "reset")
EVENTS_FILE = "events.jsonl"
PROFILE_FILE = "profile.json"

ARTICLE_LINE = re.compile(
    r"\*\*\[(?P<title>(?:\\.|[^\\\]])*)\]\((?P<link>[^)\s]+)\)\*\* · (?P<source>.+?) · "
    r"(?P<date>\d{4}-\d{2}-\d{2})(?P<rest>.*?)<!-- a:(?P<id>[0-9a-f]{6,40}) -->"
)
CHECKBOX = re.compile(r"^\s*[-*] \[(?P<mark>[ xX])\] .*?<!-- fb:(?P<id>[0-9a-f]{6,40}):(?P<signal>up|down) -->")
TAG_IN_LINE = re.compile(r"`([a-z0-9][a-z0-9-]{1,23})`")
COMMAND = re.compile(r"^\s*/(more|less|mute|reset)\b[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)


# --------------------------------------------------------------------- markers
def article_marker(article_id: str) -> str:
    return f"<!-- a:{article_id} -->"


def checkbox_lines(article_id: str, indent: str = "  ") -> list[str]:
    return [
        f"{indent}- [ ] 👍 more like this <!-- fb:{article_id}:up -->",
        f"{indent}- [ ] 👎 less like this <!-- fb:{article_id}:down -->",
    ]


def parse_issue_body(body: str) -> tuple[dict[str, dict], dict[str, str]]:
    """Return ``(articles, states)`` read from a digest issue body.

    ``articles`` maps article id to ``{"title", "source", "tags"}``;
    ``states`` maps article id to ``"up"``, ``"down"`` or ``"none"``.
    """
    articles: dict[str, dict] = {}
    ticked: dict[str, set[str]] = defaultdict(set)
    for line in (body or "").splitlines():
        match = ARTICLE_LINE.search(line)
        if match:
            articles[match["id"]] = {
                "title": re.sub(r"\\([\[\]])", r"\1", match["title"]),
                "source": match["source"].strip(),
                "tags": TAG_IN_LINE.findall(match["rest"]),
            }
            continue
        box = CHECKBOX.match(line)
        if box:
            ticked[box["id"]]  # register the id even when unticked
            if box["mark"] in "xX":
                ticked[box["id"]].add(box["signal"])
    states = {}
    for article_id, signals in ticked.items():
        states[article_id] = signals.pop() if len(signals) == 1 else "none"  # both ticked -> neutral
    return articles, states


def normalize_target(raw: str) -> str | None:
    raw = raw.strip().strip("`\"'")
    if not raw:
        return None
    kind, sep, value = raw.partition(":")
    if sep and kind.lower() in ("source", "topic"):
        kind = kind.lower()
    else:
        kind, value = "topic", raw
    value = value.strip()
    if kind == "topic":
        value = re.sub(r"[\s_]+", "-", value.lower())
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", value):
            return None
    elif not value or len(value) > 80:
        return None
    return f"{kind}:{value}"


def parse_commands(text: str) -> list[tuple[str, str | None]]:
    commands: list[tuple[str, str | None]] = []
    for action, rest in COMMAND.findall(text or ""):
        action = action.lower()
        target = normalize_target(rest) if rest.strip() else None
        if action != "reset" and target is None:
            continue
        commands.append((action, target))
        if len(commands) >= MAX_COMMANDS_PER_COMMENT:
            break
    return commands


# ---------------------------------------------------------------------- events
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_events(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in ("article", "rule", "label"):
            events.append(event)
    return events


def append_events(path: str | Path, events: Iterable[dict]) -> None:
    events = list(events)
    if not events:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def allowed_users(owner: str = "", extra: str | None = None) -> set[str]:
    extra = os.environ.get("FEEDBACK_USERS", "") if extra is None else extra
    users = {u.strip().lower() for u in extra.split(",") if u.strip()}
    if owner:
        users.add(owner.lower())
    return users


def process_issue(issue: dict, comments: list[dict], events: list[dict], users: set[str], now: datetime) -> list[dict]:
    """Return new events for an ``ai-news`` issue compared with the event log.

    The result is idempotent: processing the same issue twice adds nothing.
    """
    number = issue.get("number")
    labels = {(lbl.get("name") if isinstance(lbl, dict) else str(lbl)) for lbl in issue.get("labels") or []}
    if LABEL not in labels or issue.get("pull_request"):
        return []
    ts = _iso(now)
    new: list[dict] = []

    last_state: dict[str, str] = {}
    seen_comments: set = set()
    label_state: dict[str, bool] = {}
    for event in events:
        if event.get("issue") != number:
            continue
        if event["type"] == "article":
            last_state[event.get("id")] = event.get("signal", "none")
        elif event["type"] == "rule" and event.get("comment") is not None:
            seen_comments.add(event["comment"])
        elif event["type"] == "label":
            label_state[event.get("label")] = bool(event.get("present"))

    author = ((issue.get("user") or {}).get("login") or "").lower()
    if author in users or author in BOT_LOGINS:
        articles, states = parse_issue_body(issue.get("body") or "")
        for article_id, state in states.items():
            if state == last_state.get(article_id, "none"):
                continue
            meta = articles.get(article_id, {})
            new.append({
                "ts": ts, "type": "article", "issue": number, "id": article_id, "signal": state,
                "title": meta.get("title", ""), "source": meta.get("source", ""), "tags": meta.get("tags", []),
            })

    for comment in sorted(comments, key=lambda c: c.get("created_at", "")):
        login = ((comment.get("user") or {}).get("login") or "").lower()
        if login not in users or comment.get("id") in seen_comments:
            continue
        for action, target in parse_commands(comment.get("body", "")):
            new.append({
                "ts": _iso(_parse_ts(comment.get("created_at", ts))), "type": "rule", "issue": number,
                "comment": comment.get("id"), "action": action, "target": target,
            })

    for label in FORMAT_LABELS:
        present = label in labels
        if present != label_state.get(label, False):
            new.append({"ts": ts, "type": "label", "issue": number, "label": label, "present": present})
    return new


# --------------------------------------------------------------------- profile
def _decay(ts: datetime, now: datetime) -> float:
    weeks = max(0, (now - ts).days // 7)
    return DECAY_PER_WEEK ** weeks


def build_profile(events: list[dict], now: datetime, interests: str = "") -> dict:
    """Derive the reader profile from the event log (see module docs)."""
    events = sorted(events, key=lambda e: _parse_ts(e.get("ts", "")))  # stable: keeps log order on ties
    rules: dict[str, str] = {}
    reset_all: datetime | None = None
    reset_target: dict[str, datetime] = {}
    article_final: dict[tuple, dict] = {}
    label_final: dict[tuple, dict] = {}
    for event in events:
        if event["type"] == "rule":
            action, target = event.get("action"), event.get("target")
            if action == "reset":
                if target:
                    rules.pop(target, None)
                    reset_target[target] = _parse_ts(event["ts"])
                else:
                    rules.clear()
                    reset_target.clear()
                    reset_all = _parse_ts(event["ts"])
            elif action in ACTIONS and target:
                rules[target] = action
        elif event["type"] == "article":
            article_final[(event.get("issue"), event.get("id"))] = event
        elif event["type"] == "label":
            label_final[(event.get("issue"), event.get("label"))] = event

    def active(target: str | None, ts: datetime) -> bool:
        if reset_all and ts <= reset_all:
            return False
        return not (target and target in reset_target and ts <= reset_target[target])

    sources: dict[str, float] = defaultdict(float)
    topics: dict[str, float] = defaultdict(float)
    liked: list[tuple[datetime, str]] = []
    disliked: list[tuple[datetime, str]] = []
    signals = 0
    for event in article_final.values():
        signal = event.get("signal")
        ts = _parse_ts(event.get("ts", ""))
        if signal not in ("up", "down") or not active(None, ts):
            continue
        signals += 1
        weight = (1 if signal == "up" else -1) * _decay(ts, now)
        source = event.get("source") or ""
        if source and active(f"source:{source}", ts):
            sources[source] += weight
        for tag in event.get("tags") or []:
            if active(f"topic:{tag}", ts):
                topics[tag] += weight
        if event.get("title"):
            (liked if signal == "up" else disliked).append((ts, event["title"]))

    categories: dict[str, float] = defaultdict(float)
    top_n_delta = 0.0
    for (_, label), event in label_final.items():
        ts = _parse_ts(event.get("ts", ""))
        if not event.get("present") or not active(None, ts):
            continue
        signals += 1
        weight = _decay(ts, now)
        if label == "too-long":
            top_n_delta -= 3 * weight
        elif label == "too-short":
            top_n_delta += 3 * weight
        elif label == "too-much-product":
            categories["product"] -= weight
        elif label == "too-much-research":
            categories["research"] -= weight

    def rounded(weights: dict[str, float]) -> dict[str, float]:
        return {k: round(v, 3) for k, v in sorted(weights.items()) if abs(v) >= 0.001}

    return {
        "interests": interests,
        "signals": signals,
        "sources": rounded(sources),
        "topics": rounded(topics),
        "categories": rounded(categories),
        "rules": dict(sorted(rules.items())),
        "format": {"top_n_delta": int(round(top_n_delta))},
        "liked_titles": [t for _, t in sorted(liked, reverse=True)[:10]],
        "disliked_titles": [t for _, t in sorted(disliked, reverse=True)[:10]],
    }


def empty_profile() -> dict:
    return build_profile([], datetime.now(timezone.utc))


def load_profile(feedback_dir: str | Path) -> dict:
    path = Path(feedback_dir) / PROFILE_FILE
    profile = empty_profile()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                profile.update(data)
        except ValueError:
            log.warning("Ignoring unreadable feedback profile %s", path)
    return profile


def rebuild_profile(feedback_dir: str | Path, now: datetime) -> dict:
    """Rebuild ``profile.json`` from ``events.jsonl``, keeping the ``interests`` text."""
    feedback_dir = Path(feedback_dir)
    interests = load_profile(feedback_dir).get("interests", "") or ""
    profile = build_profile(load_events(feedback_dir / EVENTS_FILE), now, interests)
    feedback_dir.mkdir(parents=True, exist_ok=True)
    (feedback_dir / PROFILE_FILE).write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return profile


def profile_prompt(profile: dict, threshold: float = 0.3) -> str:
    """Describe the profile for the triage prompt ("likes: …, dislikes: …")."""
    def names(weights: dict, positive: bool) -> list[str]:
        items = [(k, v) for k, v in (weights or {}).items() if (v >= threshold if positive else v <= -threshold)]
        return [k for k, _ in sorted(items, key=lambda kv: -abs(kv[1]))[:8]]

    rules = profile.get("rules") or {}
    likes = names(profile.get("topics"), True) + names(profile.get("sources"), True)
    likes += [t.split(":", 1)[1] for t, a in rules.items() if a == "more"]
    dislikes = names(profile.get("topics"), False) + names(profile.get("sources"), False)
    dislikes += [t.split(":", 1)[1] for t, a in rules.items() if a in ("less", "mute")]
    parts = []
    if profile.get("interests"):
        parts.append(f"Interests: {str(profile['interests'])[:500]}")
    if likes:
        parts.append("Likes: " + ", ".join(dict.fromkeys(likes)))
    if dislikes:
        parts.append("Dislikes: " + ", ".join(dict.fromkeys(dislikes)))
    if profile.get("liked_titles"):
        parts.append("Recently liked: " + "; ".join(profile["liked_titles"][:5]))
    if profile.get("disliked_titles"):
        parts.append("Recently disliked: " + "; ".join(profile["disliked_titles"][:5]))
    return "\n".join(parts)


# ------------------------------------------------------------------ GitHub API
class GitHubClient:
    def __init__(self, repo: str, token: str, api_url: str = "https://api.github.com") -> None:
        self.repo = repo
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _get(self, path: str, params: dict | None = None):
        url = f"{self.api_url}/repos/{self.repo}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - GitHub API URL
            return json.loads(response.read().decode("utf-8"))

    def issue(self, number: int) -> dict:
        return self._get(f"issues/{int(number)}")

    def recent_issues(self, since: datetime) -> list[dict]:
        issues = self._get("issues", {"labels": LABEL, "state": "all", "since": _iso(since), "per_page": 50})
        return [i for i in issues if not i.get("pull_request")]

    def comments(self, number: int) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 11):
            batch = self._get(f"issues/{int(number)}/comments", {"per_page": 100, "page": page})
            out.extend(batch)
            if len(batch) < 100:
                break
        return out


def sync(client, feedback_dir: str | Path, users: set[str], now: datetime,
         issue_number: int | None = None, days: int = 28) -> list[dict]:
    """Read issue(s) from GitHub, append new events and rebuild the profile."""
    feedback_dir = Path(feedback_dir)
    events_path = feedback_dir / EVENTS_FILE
    events = load_events(events_path)
    issues = [client.issue(issue_number)] if issue_number else client.recent_issues(now - timedelta(days=days))
    new: list[dict] = []
    for issue in sorted(issues, key=lambda i: i.get("number", 0)):
        new_for_issue = process_issue(issue, client.comments(issue["number"]), events + new, users, now)
        new.extend(new_for_issue)
    append_events(events_path, new)
    rebuild_profile(feedback_dir, now)
    log.info("Recorded %d new feedback event(s) from %d issue(s)", len(new), len(issues))
    return new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync reader feedback from ai-news issues.")
    sub = parser.add_subparsers(dest="command", required=True)
    sync_cmd = sub.add_parser("sync", help="Read feedback from GitHub issues into feedback/.")
    sync_cmd.add_argument("--issue", type=int, help="Only this issue number (default: recently updated issues).")
    sync_cmd.add_argument("--days", type=int, default=28, help="Look-back window for recent issues (default: 28).")
    sync_cmd.add_argument("--dir", default="feedback", help="Feedback directory (default: feedback).")
    sync_cmd.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name")
    sync_cmd.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", ""),
                          help="User whose feedback counts (default: repository owner). Add more with FEEDBACK_USERS.")
    rebuild_cmd = sub.add_parser("rebuild", help="Rebuild profile.json from events.jsonl.")
    rebuild_cmd.add_argument("--dir", default="feedback")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = datetime.now(timezone.utc)
    if args.command == "rebuild":
        rebuild_profile(args.dir, now)
        return 0
    if not args.repo:
        parser.error("--repo (or GITHUB_REPOSITORY) is required")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    users = allowed_users(args.owner or args.repo.split("/")[0])
    sync(GitHubClient(args.repo, token, os.environ.get("GITHUB_API_URL", "https://api.github.com")),
         args.dir, users, now, args.issue, args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
