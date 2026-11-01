"""Command line entry point: ``python -m news_aggregator --period daily``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, time, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from .aggregator import ISSUE_MAX_CHARS, PERIODS, digest_path, fetch_all, load_feeds, render_markdown_parts, select_articles
from .summarizer import DEFAULT_INTERVAL_SECONDS, DEFAULT_MODEL, Summarizer

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def digest_snapshot_path(output_dir: str | Path, period: str, window_end: datetime) -> Path:
    base = digest_path(output_dir, period, window_end)
    return base.with_name(f".{base.stem}.snapshot.json")


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
    if (
        digest_snapshot_path(output_dir, "weekly", window_end).exists()
        or not digest_path(output_dir, "weekly", window_end).exists()
    ):
        return [("weekly", window_end)]
    return []


def _read_digest_snapshot(path: Path) -> tuple[list[tuple[str, int]], int]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict) or set(snapshot) != {"parts", "total_articles"}:
        raise ValueError(f"Invalid pending digest snapshot: {path}")
    parts = snapshot["parts"]
    total = snapshot["total_articles"]
    if not isinstance(parts, list) or not parts or type(total) is not int or total < 0:
        raise ValueError(f"Invalid pending digest snapshot: {path}")
    validated = []
    for part in parts:
        if (
            not isinstance(part, dict)
            or set(part) != {"markdown", "article_count"}
            or not isinstance(part["markdown"], str)
            or not part["markdown"].startswith("# ")
            or len(part["markdown"]) > ISSUE_MAX_CHARS
            or type(part["article_count"]) is not int
            or part["article_count"] < 0
        ):
            raise ValueError(f"Invalid pending digest snapshot: {path}")
        validated.append((part["markdown"], part["article_count"]))
    if sum(count for _, count in validated) != total:
        raise ValueError(f"Invalid pending digest snapshot: {path}")
    return validated, total


def _write_digest_snapshot(path: Path, parts: list[tuple[str, int]], total: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "parts": [{"markdown": markdown, "article_count": count} for markdown, count in parts],
        "total_articles": total,
    }, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


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
    parser.add_argument("--summarize", action="store_true", help="Generate concise OpenAI summaries (requires OPENAI_API_KEY).")
    parser.add_argument("--summary-model", default=DEFAULT_MODEL, help="OpenAI summary model (default: %(default)s).")
    parser.add_argument("--summary-interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS,
                        help="Minimum seconds between OpenAI requests, including retries (default: %(default)s).")
    parser.add_argument("--summary-cache", default=".summary-cache", help="Persistent summary cache directory.")
    args = parser.parse_args(argv)

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

    snapshots = {
        period: digest_snapshot_path(args.output_dir, period, window_end)
        if args.period == "scheduled" else None
        for period, window_end in jobs
    }
    needs_build = any(path is None or not path.exists() for path in snapshots.values())
    summarizer = None
    if args.summarize and needs_build:
        try:
            summarizer = Summarizer(
                os.environ.get("OPENAI_API_KEY", ""), args.summary_cache,
                args.summary_model, args.summary_interval_seconds,
            )
        except ValueError as exc:
            parser.error(str(exc))
    all_articles = fetch_all(load_feeds(args.feeds)) if needs_build else []
    written: list[tuple[Path, int]] = []
    total = 0
    persisted_snapshot: Path | None = None
    for period, window_end in jobs:
        snapshot_path = snapshots[period]
        if snapshot_path and snapshot_path.exists():
            logging.info("Reusing pending digest snapshot %s", snapshot_path)
            parts, article_count = _read_digest_snapshot(snapshot_path)
        else:
            articles = select_articles(all_articles, period, window_end)
            if summarizer and articles:
                logging.info("Summarizing %d selected article(s) with %s", len(articles), args.summary_model)
                try:
                    articles = summarizer.summarize_all(articles)
                except RuntimeError as exc:
                    logging.error("Digest not delivered: %s", exc)
                    return 1
            parts = render_markdown_parts(articles, period, window_end)
            article_count = len(articles)
            if snapshot_path and not args.stdout:
                _write_digest_snapshot(snapshot_path, parts, article_count)
        total += article_count

        if args.stdout:
            for markdown, _ in parts:
                sys.stdout.write(markdown)
            continue

        base = digest_path(args.output_dir, period, window_end)
        base.parent.mkdir(parents=True, exist_ok=True)
        for index, (markdown, count) in enumerate(parts, 1):
            path = base if index == 1 else base.with_name(f"{base.stem}-part-{index}.md")
            path.write_text(markdown, encoding="utf-8")
            written.append((path, count))
            logging.info("Wrote %d article(s) to %s", count, path)
        if snapshot_path and not args.stdout:
            persisted_snapshot = snapshot_path

    _write_outputs(written, total, persisted_snapshot)
    return 0


def _write_outputs(
    written: list[tuple[Path, int]], total: int, snapshot_path: Path | None = None
) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        paths = [str(path) for path, _ in written]
        with_articles = [str(path) for path, count in written if count]
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"digest_path={paths[0] if paths else ''}\n")
            fh.write(f"digest_paths={' '.join(paths)}\n")
            fh.write(f"nonempty_digest_paths={' '.join(with_articles)}\n")
            fh.write(f"article_count={total}\n")
            fh.write(f"snapshot_path={snapshot_path or ''}\n")


if __name__ == "__main__":
    raise SystemExit(main())
