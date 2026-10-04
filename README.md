# ai-news-notifications
built with agents to get ai news aggregated from ai tech company blogs

Collects posts from AI blogs at big tech companies and AI startups and **emails** you
Markdown/HTML digests split into **Research** and **Product & Announcements** sections:

- **Daily** at 08:00 (your timezone): posts from the previous 24 hours.
- **Weekly** on Sunday at 08:00: posts from the previous 7 days. On Sundays you get both emails.

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

## Email setup

1. In **Settings → Secrets and variables → Actions → Secrets**, add:

   | Secret | Example | Notes |
   |---|---|---|
   | `SMTP_HOST` | `smtp.gmail.com` | Required |
   | `SMTP_PORT` | `587` | Optional. 587 = STARTTLS (default), 465 = SSL |
   | `SMTP_USERNAME` | `you@gmail.com` | Optional, needed if your server requires login |
   | `SMTP_PASSWORD` | Gmail *App Password* | Optional |
   | `EMAIL_FROM` | `you@gmail.com` | Defaults to `SMTP_USERNAME` |
   | `EMAIL_TO` | `you@example.com, team@example.com` | Required, comma-separated |

   For Gmail, turn on 2-Step Verification and create an
   [App Password](https://myaccount.google.com/apppasswords). Your normal Gmail password won't work.

2. Under **Variables**, set `DIGEST_TIMEZONE` to your
   [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones),
   e.g. `Asia/Kolkata` or `America/Los_Angeles`. If it isn't set, UTC is used.

3. To test, open **Actions → AI News Digest → Run workflow** and choose `daily` to send an email
   right away. Manual sends don't affect the scheduled emails.

GitHub's scheduler only runs in UTC, so the workflow runs every hour and checks what's due in your
timezone. Once a digest is emailed, it's committed to `digests/`, so each email is sent exactly
once. If a run is delayed or a send fails, the next hourly run catches up. A daily digest always
covers 08:00 the day before up to 08:00 today; the weekly one covers the previous Sunday 08:00 to
this Sunday 08:00.

## Usage

```bash
pip install -r requirements.txt
python -m news_aggregator --period daily            # writes digests/daily/YYYY-MM-DD.md
python -m news_aggregator --period weekly --stdout  # print instead of writing
python -m news_aggregator --period scheduled --email --timezone Asia/Kolkata  # what the workflow runs
```

Options: `--send-hour` (default 8) and `--weekly-day` (default `sunday`) change the schedule.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
