# ai-news-notifications
built with agents to get ai news aggregated from ai tech company blogs

Collects posts from AI blogs at big tech companies and AI startups and builds Markdown digests
split into **Research** and **Product & Announcements** sections:

- **Weekly** starting Sunday at 01:00 IST (Asia/Kolkata): posts from the previous 7 days.

Each digest with new posts is opened as a GitHub **issue** (label `ai-news`) and assigned to the
repository owner, so GitHub emails it to you. Articles have a concise takeaway and two key
points generated with OpenAI, with clickable titles for reading the originals.

## Sources

All sources are configured in [`feeds.toml`](feeds.toml):

- **Big tech:** OpenAI, Google DeepMind, Google Research, Google AI, Google Cloud,
  Microsoft Research, Microsoft AI, Meta AI, Meta Engineering, NVIDIA, AWS ML,
  Apple ML Research, IBM Research, Salesforce AI Research, GitHub.
- **AI labs and startups:** Anthropic (news and research), Mistral AI, xAI,
  Stability AI, Cohere, Hugging Face, Ai2, Databricks, Together AI, LangChain, Replicate.

Feed options:

| Field | Meaning |
|---|---|
| `type` | `rss` (default) for RSS/Atom feeds, or `html` for blogs without a feed (Anthropic, Meta AI, Mistral, …). |
| `link_pattern` | For `html` feeds: a regex that article URLs on the listing page must fully match. Each matching article page is fetched, and its title, summary and date are read from its metadata (Open Graph / JSON-LD / `<time>`). |
| `max_articles` | For `html` feeds: the maximum number of article pages fetched per run (default 15). |
| `ai_only` | Set to `false` for mixed-topic blogs (e.g. AWS ML, NVIDIA Technical Blog, Databricks) to keep only posts that match AI keywords. |
| `category` | `research`, `product`, or `auto` (default). `auto` classifies each post by keywords. |

If a feed fails to load, a warning is logged and the feed is skipped, so the
rest of the digest is still built.

## Setup

1. **Timezone:** the workflow uses `Asia/Kolkata` and a Sunday 01:00 cutoff explicitly.
   The repository `DIGEST_TIMEZONE` variable does not override this workflow schedule.
2. **Get the issue emails:** in [GitHub notification settings](https://github.com/settings/notifications),
   make sure **Email** is turned on for "Participating, @mentions and custom". You're assigned to
   each issue, so this works even if you stop watching the repository.
3. **OpenAI:** enable API billing or available credits in your OpenAI account, create an API key,
   and store it as the repository Actions secret `OPENAI_API_KEY`. A ChatGPT subscription does
   not include API usage, and slowing requests does **not** create a free API tier.
4. **Test it:** open **Actions → AI News Digest → Run workflow** and choose `weekly` to open an
   issue right away. Manual runs don't affect the schedule.

GitHub's scheduler only runs in UTC, so the workflow runs at Saturday 19:30 UTC,
which is Sunday 01:00 IST, and checks whether that cutoff is due. GitHub may delay
scheduled starts, and fetching and summarizing articles takes additional time before delivery.
Once a digest is delivered, it's committed to `digests/`, so each digest is sent exactly
once. If a run is delayed or fails, the next scheduled run catches up. The weekly digest covers the
previous Sunday 01:00 IST to this Sunday 01:00 IST. If a period has no new posts, the digest is still
committed but no issue is opened.

## Concise article summaries

The weekly job collects, filters, and deduplicates posts **before** making any model requests.
It uses full RSS article content when supplied; otherwise it downloads the article and extracts
readable text with Trafilatura. The title or publisher description alone is never used to
generate an AI summary.

The default model is the inexpensive `gpt-5-nano`. Each summary has one takeaway sentence and
exactly two key points, totaling **at most 60 words**, validated before rendering. Requests
use structured JSON output, minimal reasoning, a 2,048-token completion limit (including
reasoning tokens), and at most 24,000 source characters. Longer sources are explicitly labeled
**AI summary (partial article)**. Article text is sent to OpenAI; API response storage is disabled.
Do not configure feeds containing private or sensitive material.

Requests are sequential, starting at least **60 seconds apart**, including retries. Set repository
Actions variables `OPENAI_MODEL` or `SUMMARY_INTERVAL_SECONDS` to override these defaults
(interval must be at least 1 second; model must support Chat Completions, strict JSON schemas,
and `minimal` reasoning). A hundred uncached readable articles takes roughly
100 minutes plus fetch/request time; the workflow has a six-hour limit. Pacing helps with
rate limits, not token costs. This uses ordinary API requests, **not** OpenAI's asynchronous
Batch API: it avoids waiting up to 24 hours and publishes the digest in the same workflow.
Check [OpenAI pricing](https://openai.com/api/pricing/) and your project's rate limits and
billing budget before running.

Successful summaries are cached in `.summary-cache/`, keyed by URL, title, full source hash,
model, and prompt version. Actions restores and saves this cache, including after failed runs,
so retries reuse completed summaries while changed articles are regenerated. The cache
contains generated summaries, not API credentials or full article bodies, and is not committed.
GitHub may evict old caches; in that case summaries are regenerated.

Unreadable or very short sources, refused/incomplete responses, and invalid summaries show a
clearly labeled publisher excerpt or **Full summary unavailable**, with warnings in the job log.
Authentication, model-access, exhausted-quota, or persistent API failures stop delivery rather
than silently publishing an entirely unsummarized digest. Transient connection errors,
rate limits, and server errors get up to three retries with pacing and backoff.

Issues are limited to **60,000 characters**, below GitHub's 65,536-character body limit.
Oversized digests are split between complete articles into **Part 1 of N**, **Part 2 of N**, etc.
The first file retains its usual name; subsequent files use `-part-2.md`, `-part-3.md`, etc.
Every part preserves the Research/Product sections and original article links.
Scheduled retries reuse issues with the same exact title, so a partially published digest
does not create duplicate parts. Digests are committed only after every part is published.
Manual runs intentionally create new issues and do not mark the scheduled digest as delivered.

## Usage

```bash
pip install -r requirements.txt
python -m news_aggregator --period weekly          # writes digests/weekly/YYYY-Www.md
python -m news_aggregator --period daily --stdout  # generate a one-off daily digest for inspection
python -m news_aggregator --period scheduled --timezone Asia/Kolkata --send-hour 1 --summarize  # weekly workflow
python -m news_aggregator --period weekly --summarize  # requires OPENAI_API_KEY
```

Options: `--send-hour` (default 8) and `--weekly-day` (default `sunday`) change the schedule.
Local runs use `--timezone`, or `DIGEST_TIMEZONE` if set, falling back to UTC.
The workflow explicitly sets the timezone to `Asia/Kolkata` and `--send-hour 1`.
Local runs omit model calls unless `--summarize` is supplied. Summary options:
`--summary-model`, `--summary-interval-seconds`, and `--summary-cache`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
