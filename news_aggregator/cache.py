"""Small JSON file cache for article text and LLM summaries.

The cache lives outside the repository's committed files (``.cache/`` is
git-ignored) and is persisted between workflow runs with ``actions/cache``.
Full article text is never committed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 60


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


class JsonCache:
    """A thread-safe ``key -> JSON value`` store backed by one JSON file.

    Passing ``path=None`` gives an in-memory cache that is never saved.
    """

    def __init__(self, path: str | Path | None = None, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> None:
        self.path = Path(path) if path else None
        self.max_age = max_age_days * 86400
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = {k: v for k, v in loaded.items() if isinstance(v, dict) and "value" in v}
            except (OSError, ValueError):
                log.warning("Ignoring unreadable cache file %s", self.path)

    def get(self, key: str):
        with self._lock:
            entry = self._data.get(key)
        return None if entry is None else entry["value"]

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = {"ts": int(time.time()), "value": value}

    def __len__(self) -> int:
        return len(self._data)

    def save(self) -> None:
        if not self.path:
            return
        cutoff = time.time() - self.max_age
        with self._lock:
            data = {k: v for k, v in self._data.items() if v.get("ts", 0) >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
