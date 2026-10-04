"""Command line entry point: ``python -m news_aggregator --period daily``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, time, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from .aggregator import PERIODS, digest_path, fetch_all, load_feeds, render_markdown, select_articles

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
    due = []
    for period in ("daily", "weekly"):
        if period == "weekly" and window_end.weekday() != weekly_day:
            continue
        if not digest_path(output_dir, period, window_end).exists():
            due.append((period, window_end))
    return due


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
        help="'scheduled' builds whichever digests are due (daily at --send-hour, weekly on --weekly-day).",
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
    parser.add_argument("--email", action="store_true", help="Email each digest (SMTP settings from env).")
    parser.add_argument("--stdout", action="store_true", help="Print the digest instead of writing a file.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = (args.now or datetime.now(timezone.utc)).astimezone(args.timezone)

    smtp_config = None
    if args.email:
        from . import mailer

        smtp_config = mailer.SmtpConfig.from_env()

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
    written: list[Path] = []
    total = 0
    for period, window_end in jobs:
        articles = select_articles(all_articles, period, window_end)
        markdown = render_markdown(articles, period, window_end)
        total += len(articles)

        if args.stdout:
            sys.stdout.write(markdown)
            continue

        if smtp_config is not None:
            from . import mailer

            message = mailer.build_message(
                smtp_config,
                mailer.subject_for(period, window_end, len(articles)),
                markdown,
                mailer.render_html(articles, period, window_end),
            )
            mailer.send_message(smtp_config, message)
            logging.info("Emailed %s digest to %d recipient(s)", period, len(smtp_config.recipients))

        # Written after a successful send so a failed delivery is retried by the next scheduled run.
        path = digest_path(args.output_dir, period, window_end)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        written.append(path)
        logging.info("Wrote %d article(s) to %s", len(articles), path)

    _write_outputs(written, total)
    return 0


def _write_outputs(paths: list[Path], total: int) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"digest_path={paths[0] if paths else ''}\n")
            fh.write(f"digest_paths={' '.join(str(p) for p in paths)}\n")
            fh.write(f"article_count={total}\n")


if __name__ == "__main__":
    raise SystemExit(main())
