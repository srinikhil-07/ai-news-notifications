"""Article ids, keyword topic tags and cross-post merging for the ranked digest."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Iterable

from .aggregator import Article, normalize_link

TOPIC_KEYWORDS = {
    "agents": r"agents?|agentic|tool use|computer use|autonomous",
    "coding": r"coding|code generation|developers?|programming|copilot|software engineering",
    "evals": r"evals?|evaluations?|benchmarks?|leaderboards?",
    "safety": r"safety|alignment|interpretability|red[- ]teaming|jailbreaks?|misuse|security|distillation attacks?",
    "infra": r"inference|gpus?|tpus?|datacenters?|data centers?|chips?|serving|latency|compute|clusters?|kernels?",
    "multimodal": r"multimodal|images?|video|audio|speech|vision|voice",
    "policy": r"policy|regulation|government|governance|law|economy|economic|employment|jobs",
    "open-models": r"open[- ]source|open[- ]weights?|open models?",
    "robotics": r"robots?|robotics|embodied",
    "science": r"biology|protein|chemistry|materials|quantum|math(ematics)?|medical|health|climate|weather",
    "training": r"pre-?training|post-?training|fine-?tun\w*|reinforcement learning|rlhf|datasets?|scaling laws?",
    "enterprise": r"enterprise|customers?|partnerships?|business",
}
_TOPIC_RES = {tag: re.compile(rf"\b({pattern})\b", re.IGNORECASE) for tag, pattern in TOPIC_KEYWORDS.items()}


def article_id(article: Article) -> str:
    return hashlib.sha1(normalize_link(article.link).encode("utf-8")).hexdigest()[:10]


def keyword_tags(text: str, limit: int = 4) -> tuple[str, ...]:
    counts = {tag: len(rx.findall(text or "")) for tag, rx in _TOPIC_RES.items()}
    ranked = sorted((tag for tag, n in counts.items() if n), key=lambda t: -counts[t])
    return tuple(ranked[:limit])


def article_tags(article: Article) -> tuple[str, ...]:
    return keyword_tags(f"{article.title} {article.summary}")


def _title_tokens(title: str) -> frozenset[str]:
    title = re.split(r"\s+[|–—-]\s+", title.lower())[0]
    return frozenset(re.findall(r"[a-z0-9]+", title))


def merge_crossposts(articles: Iterable[Article], threshold: float = 0.85) -> list[Article]:
    """Merge the same announcement posted on several blogs into one article.

    Titles are compared by word overlap (ignoring a trailing ``| Site`` suffix).
    The first (newest) article is kept and lists the others in ``also_in``.
    """
    kept: list[Article] = []
    tokens: list[frozenset[str]] = []
    for article in articles:
        words = _title_tokens(article.title)
        match = None
        if len(words) >= 3:
            for i, other in enumerate(tokens):
                if len(other) >= 3 and len(words & other) / len(words | other) >= threshold:
                    match = i
                    break
        if match is None:
            kept.append(article)
            tokens.append(words)
            continue
        first = kept[match]
        sources = [s for s in (*first.also_in, article.source, *article.also_in) if s != first.source]
        kept[match] = replace(first, also_in=tuple(dict.fromkeys(sources)))
    return kept
