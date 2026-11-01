"""Publish complete digest parts, reusing scheduled issues on retries."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from .aggregator import ISSUE_MAX_CHARS

log = logging.getLogger(__name__)


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout.strip()


def publish(paths: list[Path], owner: str, scheduled: bool) -> None:
    prepared = []
    for path in paths:
        body = path.read_text(encoding="utf-8")
        if len(body) > ISSUE_MAX_CHARS:
            raise ValueError(f"Digest exceeds {ISSUE_MAX_CHARS} characters: {path}")
        if not body.startswith("# "):
            raise ValueError(f"Digest has no title: {path}")
        title = f"{body.splitlines()[0][2:]} — {path.stem}"
        prepared.append((path, title))
    if not prepared:
        return
    _gh("label", "create", "ai-news", "--color", "1D76DB", "--description", "AI news digests", "--force")
    owner_type = _gh("api", f"users/{owner}", "--jq", ".type")
    for path, title in prepared:
        if scheduled:
            existing = json.loads(_gh(
                "issue", "list", "--state", "all", "--search", f'"{title}" in:title',
                "--limit", "100", "--json", "title,url",
            ))
            match = next((issue for issue in existing if issue["title"] == title), None)
            if match:
                log.info("Already published %s: %s", path, match["url"])
                continue
        args = ["issue", "create", "--title", title, "--body-file", str(path), "--label", "ai-news"]
        if owner_type == "User":
            args.extend(["--assignee", owner])
        url = _gh(*args)
        log.info("Published %s: %s", path, url)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    paths = [Path(value) for value in os.environ["DIGEST_PATHS"].split()]
    try:
        publish(paths, os.environ["OWNER"], os.environ["PERIOD"] == "scheduled")
    except (ValueError, OSError) as exc:
        log.error("GitHub issue delivery failed: %s", exc)
        raise SystemExit(1) from exc
    except subprocess.CalledProcessError as exc:
        log.error("GitHub issue delivery failed: %s", exc.stderr.strip())
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
