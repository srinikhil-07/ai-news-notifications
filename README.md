# ai-news-notifications
built with agents to get ai news aggregated from ai tech company blogs

Collects posts from AI blogs at big tech companies and AI startups. It builds
**daily** and **weekly** Markdown digests split into **Research** and
**Product & Announcements** sections, then posts a GitHub issue as a notification.

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

## Usage

```bash
pip install -r requirements.txt
python -m news_aggregator --period daily            # writes digests/daily/YYYY-MM-DD.md
python -m news_aggregator --period weekly           # writes digests/weekly/YYYY-Www.md
python -m news_aggregator --period weekly --stdout  # print instead of writing
```

## Automation

`.github/workflows/digest.yml` runs every day at 07:00 UTC and every Monday at
07:30 UTC (weekly). You can also run it manually from the Actions tab. Each run
commits the digest to `digests/` and, if there are new posts, opens an issue
labelled `ai-news`. Watch the repository to get these issues as notifications.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
