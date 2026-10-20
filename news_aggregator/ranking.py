"""Relevance scoring, tiers and the personalised (ranked) digest.

Each article gets a score from the reader profile (source weight, topic-tag
weights, category weight, slash-command rules) plus the LLM relevance
judgement from Pass A. Articles are split into tiers:

* **Top** (default 12, adjusted by ``too-long``/``too-short`` labels): detailed
  summary at the top of the digest, including 1–2 "🔍 exploring" slots for
  sources/topics without feedback so the digest does not become a filter bubble.
* **Middle**: one-line gist, grouped into Research / Product & Announcements.
* **Low** (and muted): title and link only, collapsed in ``<details>``.

Nothing is dropped. Every article has 👍/👎 checkboxes that feed back into
the profile (see feedback.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .aggregator import CATEGORY_TITLES, PERIODS, Article, escape_md
from .feedback import article_marker, checkbox_lines
from .summarize import Detail, Triage, article_id, fallback_triage

DEFAULT_TOP_N = 12
DEFAULT_MIDDLE_N = 30
EXPLORE_SLOTS = 2
LOW_SCORE = -0.75
RULE_BOOST = 1.5
ISSUE_MAX_CHARS = 60000
DETAIL_LEVELS = 4  # 0 = everything … 3 = shortest before truncating

FEEDBACK_HELP = (
    "_Feedback: tick 👍/👎 under any article, comment `/more <topic>`, `/less source:<name>`, "
    "`/mute topic:<tag>` or `/reset`, or add a `too-long`, `too-short`, `too-much-product` or "
    "`too-much-research` label. Next week's digest is ranked with it._"
)


@dataclass
class Ranked:
    article: Article
    id: str
    triage: Triage
    score: float = 0.0
    muted: bool = False
    tier: str = "low"
    exploring: bool = False
    detail: Detail | None = None

    @property
    def category(self) -> str:
        return self.triage.kind or self.article.category


def _squash(value: float) -> float:
    return math.tanh(value / 2)


def _rule_targets(article: Article, triage: Triage) -> list[str]:
    return [f"source:{article.source.lower()}"] + [f"topic:{tag}" for tag in triage.tags]


def score_article(article: Article, triage: Triage, profile: dict) -> tuple[float, bool]:
    """Return ``(score, muted)`` for an article; higher is more relevant."""
    sources = {k.lower(): v for k, v in (profile.get("sources") or {}).items()}
    topics = profile.get("topics") or {}
    score = _squash(sources.get(article.source.lower(), 0.0))
    if triage.tags:
        score += max(-1.0, min(1.0, sum(_squash(topics.get(tag, 0.0)) for tag in triage.tags)))
    category = triage.kind or article.category
    score += 0.5 * _squash((profile.get("categories") or {}).get(category, 0.0))
    if triage.relevance is not None:
        score += (triage.relevance - 0.5) * 2

    rules = {k.lower(): v for k, v in (profile.get("rules") or {}).items()}
    muted = False
    for target in _rule_targets(article, triage):
        action = rules.get(target)
        if action == "more":
            score += RULE_BOOST
        elif action == "less":
            score -= RULE_BOOST
        elif action == "mute":
            muted = True
    return round(score, 4), muted


def assign_tiers(
    articles: list[Article],
    triages: dict[str, Triage],
    profile: dict,
    top_n: int = DEFAULT_TOP_N,
    middle_n: int = DEFAULT_MIDDLE_N,
    explore_slots: int = EXPLORE_SLOTS,
) -> list[Ranked]:
    ranked = []
    for article in articles:
        aid = article_id(article)
        triage = triages.get(aid) or fallback_triage(article)
        score, muted = score_article(article, triage, profile)
        ranked.append(Ranked(article, aid, triage, score, muted))

    top_n = max(3, min(25, top_n + int((profile.get("format") or {}).get("top_n_delta", 0))))
    order = sorted(
        (r for r in ranked if not r.muted),
        key=lambda r: (-r.score, -r.article.published.timestamp()),
    )
    explore = min(explore_slots, top_n // 4) if profile.get("signals") else 0
    top = order[: top_n - explore]
    rest = order[top_n - explore:]

    if explore:
        known_sources = {k.lower() for k in (profile.get("sources") or {})}
        known_sources |= {t.split(":", 1)[1].lower() for t in (profile.get("rules") or {}) if t.startswith("source:")}
        known_topics = set(profile.get("topics") or {})

        def novelty(r: Ranked) -> int:
            if r.article.source.lower() not in known_sources:
                return 2
            return 1 if r.triage.tags and not set(r.triage.tags) & known_topics else 0

        candidates = sorted(
            (r for r in rest if novelty(r) and r.score > LOW_SCORE),
            key=lambda r: (-novelty(r), -(r.triage.relevance if r.triage.relevance is not None else 0.5),
                           -r.article.published.timestamp()),
        )
        for r in candidates[:explore]:
            r.exploring = True
        rest = [r for r in rest if not r.exploring]
        # Slots without an exploration candidate go to the next best articles.
        backfill = explore - len(candidates[:explore])
        top += rest[:backfill] + candidates[:explore]
        rest = rest[backfill:]

    for r in top:
        r.tier = "top"
    middle_count = 0
    for r in rest:
        if middle_count < middle_n and r.score > LOW_SCORE:
            r.tier = "middle"
            middle_count += 1
        else:
            r.tier = "low"
    return top + rest + [r for r in ranked if r.muted]


def footer_line(profile: dict) -> str:
    signals = int(profile.get("signals") or 0)
    if not signals and not profile.get("rules") and not profile.get("interests"):
        return "_No feedback yet: articles are ranked by general relevance._"
    weights = {**(profile.get("topics") or {}), **(profile.get("sources") or {})}
    rules = profile.get("rules") or {}
    boosted = [k for k, v in sorted(weights.items(), key=lambda kv: -kv[1]) if v >= 0.3][:4]
    boosted += [t.split(":", 1)[1] for t, a in rules.items() if a == "more"]
    reduced = [k for k, v in sorted(weights.items(), key=lambda kv: kv[1]) if v <= -0.3][:4]
    reduced += [t.split(":", 1)[1] for t, a in rules.items() if a == "less"]
    muted = [t.split(":", 1)[1] for t, a in rules.items() if a == "mute"]
    parts = [f"Ranked using {signals} feedback signal{'s' if signals != 1 else ''}"]
    for label, names in (("boosted", boosted), ("reduced", reduced), ("muted", muted)):
        if names:
            parts.append(f"{label}: {', '.join(escape_md(n) for n in dict.fromkeys(names))}")
    return "_" + "; ".join(parts) + "._"


def _article_line(r: Ranked) -> str:
    a = r.article
    source = a.source.replace("·", "-")
    bits = [f"- **[{escape_md(a.title)}]({a.link})** · {escape_md(source)} · {a.published:%Y-%m-%d}"]
    if r.triage.tags:
        bits.append(" ".join(f"`{t}`" for t in r.triage.tags))
    if r.detail and r.detail.kind:
        bits.append(r.detail.kind)
    if a.also_in:
        bits.append("also on " + ", ".join(escape_md(s) for s in a.also_in))
    if r.exploring:
        bits.append("🔍 exploring")
    return " · ".join(bits) + f" {article_marker(r.id)}"


def _summary_text(r: Ranked) -> str:
    text = r.triage.gist or escape_md(r.article.summary)
    if text and r.article.content_source == "excerpt":
        text += " _(from excerpt)_"
    return text


def _render_item(r: Ranked, level: int) -> list[str]:
    lines = [_article_line(r)]
    if r.tier == "top":
        detail = r.detail
        if detail and level < 1:
            lines.append(f"  > {detail.summary}")
            if detail.key_points:
                lines.append("  >")
                lines.extend(f"  > - {point}" for point in detail.key_points)
            if detail.why:
                lines.append("  >")
                lines.append(f"  > _Why it matters:_ {detail.why}")
        else:
            text = detail.summary if detail else _summary_text(r)
            if text:
                lines.append(f"  > {text}")
    elif r.tier == "middle" and level < 2:
        text = _summary_text(r)
        if text:
            lines.append(f"  > {text}")
    if r.tier != "low" or level < 3:
        lines.extend(checkbox_lines(r.id))
    return lines


def render_ranked(
    ranked: list[Ranked],
    overview: list[str],
    period: str,
    now: datetime,
    profile: dict,
    level: int = 0,
    note: str = "",
) -> str:
    start = now - PERIODS[period]
    title = "Daily" if period == "daily" else "Weekly"
    lines = [
        f"# {title} AI News Digest",
        "",
        f"_{start:%Y-%m-%d %H:%M %Z} → {now:%Y-%m-%d %H:%M %Z} · {len(ranked)} article(s)_",
        "",
    ]
    if note:
        lines += [note, ""]
    if not ranked:
        lines.append("No new AI posts from the tracked blogs in this period.")
        return "\n".join(lines) + "\n"

    if overview:
        lines += ["## This week in AI", ""]
        lines += [f"- {bullet}" for bullet in overview]
        lines.append("")

    top = [r for r in ranked if r.tier == "top"]
    if top:
        lines += ["## ⭐ Top picks", ""]
        for r in top:
            lines += _render_item(r, level)
        lines.append("")

    middle = [r for r in ranked if r.tier == "middle"]
    for category, heading in CATEGORY_TITLES.items():
        items = [r for r in middle if r.category == category]
        if not items:
            continue
        lines += [f"## {heading}", ""]
        for r in items:
            lines += _render_item(r, level)
        lines.append("")

    low = [r for r in ranked if r.tier == "low"]
    if low:
        low.sort(key=lambda r: (r.article.source.lower(), -r.article.published.timestamp()))
        lines += ["<details>", f"<summary>{len(low)} more article(s), lightly summarised</summary>", ""]
        for r in low:
            lines += _render_item(r, level)
        lines += ["", "</details>", ""]

    lines += ["---", "", footer_line(profile), "", FEEDBACK_HELP, ""]
    return "\n".join(lines)


def render_issue_body(
    ranked: list[Ranked],
    overview: list[str],
    period: str,
    now: datetime,
    profile: dict,
    full_digest_url: str = "",
    max_chars: int = ISSUE_MAX_CHARS,
) -> str:
    """Render the digest to fit in a GitHub issue body (``max_chars``).

    Detail is reduced step by step (key points → middle gists → low-tier
    checkboxes) and, as a last resort, the body is cut at a line boundary.
    The full version is the committed digest file, linked when available.
    """
    link = f" See the [full digest]({full_digest_url})." if full_digest_url else ""
    for level in range(DETAIL_LEVELS):
        note = f"_Shortened to fit in an issue.{link}_" if level else ""
        body = render_ranked(ranked, overview, period, now, profile, level, note)
        if len(body) <= max_chars:
            return body
    note = f"_Truncated to fit in an issue.{link}_"
    body = render_ranked(ranked, overview, period, now, profile, DETAIL_LEVELS - 1, note)
    tail = "\n\n…\n"
    cut = body[: max_chars - len(tail) - len("\n</details>")].rsplit("\n", 1)[0]
    if cut.count("<details>") > cut.count("</details>"):
        cut += "\n</details>"
    return cut + tail
