"""Command line entry point: ``python -m news_aggregator --period daily``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from .aggregator import PERIODS, digest_path, fetch_all, load_feeds, render_markdown, select_articles


def _parse_now(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate AI news from tech company blogs.")
    parser.add_argument("--period", choices=sorted(PERIODS), default="daily")
    parser.add_argument("--feeds", default="feeds.toml", help="Path to the feeds TOML config.")
    parser.add_argument("--output-dir", default="digests", help="Directory to write digests into.")
    parser.add_argument("--now", type=_parse_now, help="Override the current time (ISO 8601, UTC by default).")
    parser.add_argument("--stdout", action="store_true", help="Print the digest instead of writing a file.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = args.now or datetime.now(timezone.utc)

    articles = select_articles(fetch_all(load_feeds(args.feeds)), args.period, now)
    markdown = render_markdown(articles, args.period, now)

    if args.stdout:
        sys.stdout.write(markdown)
        return 0

    path = digest_path(args.output_dir, args.period, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    logging.info("Wrote %d article(s) to %s", len(articles), path)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"digest_path={path}\narticle_count={len(articles)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
