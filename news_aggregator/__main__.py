"""Command line entry point: ``python -m news_aggregator --period daily``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, time, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from . import llm, scraper
from .aggregator import (
    PERIODS,
    USER_AGENT,
    Article,
    digest_path,
    fetch_all,
    load_feeds,
    render_markdown,
    select_articles,
)
from .cache import JsonCache
from .content import fill_contents
from .feedback import load_profile, profile_prompt
from .ranking import DEFAULT_TOP_N, assign_tiers, render_issue_body, render_ranked
from .summarize import Summarizer, merge_crossposts

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def due_periods(
    now: datetime, output_dir: str | Path, send_hour: int = 8, weekly_day: int = 6
) -> list[tuple[str, datetime]]:
    """Return ``(period, window_end)`` pairs that are due and not yet delivered.

    ``now`` must be in the delivery timezone. A digest is due once the local
    time reaches ``send_hour`` and its digest file does not exist yet, so a
    late or retried run still delivers exactly once. The window always ends at
    ``send_hour`` local time, so consecutive digests never overlap or leave gaps.
    """
    window_end = datetime.combine(now.date(), time(send_hour), tzinfo=now.tzinfo)
    if now < window_end:
        return []
    if window_end.weekday() != weekly_day:
        return []
    if not digest_path(output_dir, "weekly", window_end).exists():
        return [("weekly", window_end)]
    return []


def _parse_now(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_timezone(value: str) -> tzinfo:
    try:
        return ZoneInfo(value)
    except Exception as exc:  # noqa: BLE001
        raise argparse.ArgumentTypeError(f"unknown timezone {value!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate AI news from tech company blogs.")
    parser.add_argument(
        "--period",
        choices=sorted(PERIODS) + ["scheduled"],
        default="daily",
        help="'scheduled' builds the weekly digest when due; manual daily/weekly runs are still available.",
    )
    parser.add_argument("--feeds", default="feeds.toml", help="Path to the feeds TOML config.")
    parser.add_argument("--output-dir", default="digests", help="Directory to write digests into.")
    parser.add_argument("--now", type=_parse_now, help="Override the current time (ISO 8601, UTC by default).")
    parser.add_argument(
        "--timezone",
        type=_parse_timezone,
        default=os.environ.get("DIGEST_TIMEZONE") or "UTC",
        help="IANA timezone for dates and the send schedule (default: $DIGEST_TIMEZONE or UTC).",
    )
    parser.add_argument("--send-hour", type=int, default=8, choices=range(24), metavar="HOUR",
                        help="Local hour at which scheduled digests are sent (default: 8).")
    parser.add_argument("--weekly-day", choices=WEEKDAYS, default="sunday",
                        help="Day the scheduled weekly digest is sent (default: sunday).")
    parser.add_argument("--stdout", action="store_true", help="Print the digest instead of writing a file.")
    parser.add_argument("--fetch-content", action="store_true",
                        help="Load the full text of each digest article (feed content, then the article page).")
    parser.add_argument("--ranked", action="store_true",
                        help="Personalised digest: relevance tiers, feedback checkboxes and the feedback profile.")
    parser.add_argument("--llm", choices=llm.PROVIDERS, default=os.environ.get("LLM_PROVIDER") or "none",
                        help="LLM provider for summaries (default: $LLM_PROVIDER or none). Implies "
                             "--ranked and --fetch-content. 'openai' reads LLM_API_KEY, LLM_MODEL and LLM_ENDPOINT.")
    parser.add_argument("--cache-dir", default=".cache/news_aggregator",
                        help="Cache for article text and summaries (not committed).")
    parser.add_argument("--feedback-dir", default="feedback", help="Directory with the feedback profile.")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="Articles in the top tier (default: 12).")
    parser.add_argument("--issue-dir", help="Also write an issue-sized body for each digest into this directory.")
    args = parser.parse_args(argv)
    if args.llm != "none":
        args.ranked = args.fetch_content = True

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = (args.now or datetime.now(timezone.utc)).astimezone(args.timezone)

    if args.period == "scheduled":
        jobs = due_periods(now, args.output_dir, args.send_hour, WEEKDAYS.index(args.weekly_day))
        if not jobs:
            logging.info("No digest due at %s", now.isoformat())
    else:
        jobs = [(args.period, now)]

    if not jobs:
        _write_outputs([], 0)
        return 0

    all_articles = fetch_all(load_feeds(args.feeds))
    cache_dir = Path(args.cache_dir)
    content_cache = JsonCache(cache_dir / "content.json")
    summary_cache = JsonCache(cache_dir / "summaries.json")
    written: list[tuple[Path, int]] = []
    total = 0
    try:
        for period, window_end in jobs:
            articles = select_articles(all_articles, period, window_end)
            if args.ranked:
                articles = merge_crossposts(articles)
            if args.fetch_content:
                articles = fill_contents(articles, lambda url: scraper.fetch_url(url, USER_AGENT), content_cache)
            path = digest_path(args.output_dir, period, window_end)
            issue_body = None
            if args.ranked:
                markdown, issue_body = _render_ranked_digest(args, articles, period, window_end, path, summary_cache)
            else:
                markdown = render_markdown(articles, period, window_end)
            total += len(articles)

            if args.stdout:
                sys.stdout.write(markdown)
                continue

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            written.append((path, len(articles)))
            logging.info("Wrote %d article(s) to %s", len(articles), path)
            if args.issue_dir and issue_body is not None:
                issue_path = Path(args.issue_dir) / path.name
                issue_path.parent.mkdir(parents=True, exist_ok=True)
                issue_path.write_text(issue_body, encoding="utf-8")
    finally:
        content_cache.save()
        summary_cache.save()

    _write_outputs(written, total)
    return 0


def _full_digest_url(path: Path) -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    ref = os.environ.get("GITHUB_REF_NAME")
    if not repo or not ref or path.is_absolute():
        return ""
    return f"{server}/{repo}/blob/{ref}/{path.as_posix()}"


def _render_ranked_digest(args, articles: list[Article], period: str, window_end: datetime, path: Path,
                          summary_cache: JsonCache) -> tuple[str, str]:
    profile = load_profile(args.feedback_dir)
    summarizer = Summarizer(llm.make_provider(args.llm), summary_cache, profile_prompt(profile))
    triages = summarizer.triage(articles)
    ranked = assign_tiers(articles, triages, profile, top_n=args.top_n)
    top = [r for r in ranked if r.tier == "top"]
    details = summarizer.details([r.article for r in top])
    for r in top:
        r.detail = details.get(r.id)
    overview = summarizer.overview([r.article for r in ranked], triages)
    logging.info("LLM calls this digest: %d", summarizer.calls)
    markdown = render_ranked(ranked, overview, period, window_end, profile)
    issue_body = render_issue_body(ranked, overview, period, window_end, profile, _full_digest_url(path))
    return markdown, issue_body


def _write_outputs(written: list[tuple[Path, int]], total: int) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        paths = [str(path) for path, _ in written]
        with_articles = [str(path) for path, count in written if count]
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"digest_path={paths[0] if paths else ''}\n")
            fh.write(f"digest_paths={' '.join(paths)}\n")
            fh.write(f"nonempty_digest_paths={' '.join(with_articles)}\n")
            fh.write(f"article_count={total}\n")


if __name__ == "__main__":
    raise SystemExit(main())
