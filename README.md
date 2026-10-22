# ai-news-notifications
built with agents to get ai news aggregated from ai tech company blogs

Collects posts from AI blogs at big tech companies and AI startups and builds Markdown digests
split into **Research** and **Product & Announcements** sections:

- **Weekly** on Sunday at 08:00: posts from the previous 7 days.

Each digest with new posts is opened as a GitHub **issue** (label `ai-news`) and assigned to the
repository owner, so GitHub emails it to you.

The digest is **personalised**. Your 👍/👎 feedback on the issue decides which articles are
ranked as top picks next week, and every article has a **✨ Summarise with Copilot** link for an
on-demand summary (see [Summaries and feedback](#summaries-and-feedback)).

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

1. **Timezone:** in **Settings → Secrets and variables → Actions → Variables**, set
   `DIGEST_TIMEZONE` to your [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones),
   e.g. `Asia/Kolkata` or `America/Los_Angeles`. If it isn't set, UTC is used.
2. **Get the issue emails:** in [GitHub notification settings](https://github.com/settings/notifications),
   make sure **Email** is turned on for "Participating, @mentions and custom". You're assigned to
   each issue, so this works even if you stop watching the repository.
3. **Test it:** open **Actions → AI News Digest → Run workflow** and choose `weekly` to open an
   issue right away. Manual runs don't affect the schedule.

GitHub's scheduler only runs in UTC, so the workflow checks whether Sunday 08:00 in your timezone
is due. Once a digest is delivered, it's committed to `digests/`, so each digest is sent exactly
once. If a run is delayed or fails, the next scheduled run catches up. The weekly digest covers the
previous Sunday 08:00 to this Sunday 08:00. If a period has no new posts, the digest is still
committed but no issue is opened.

## Summaries and feedback

Set the `DIGEST_RANKED` repository variable to `false` for the original unranked digest without
feedback (default `true`). If the same announcement was posted on several blogs, the ranked digest
shows it once with "also on …".

**Tiers.** Each article gets keyword topic tags (agents, evals, infra, …) and is scored from your
feedback (source, topic tags, category and rules). Articles are split into three tiers:

- **⭐ Top picks** (12 by default): the feed's excerpt.
  - Up to 2 of these are "🔍 exploring" slots for sources and topics you haven't rated yet.
- **Research / Product & Announcements**: the feed's excerpt.
- **Collapsed list**: title and link only.

Nothing is dropped. The footer says what the ranking used, for example _Ranked using 14 feedback
signals; boosted: agents; muted: Databricks_. If a digest is longer than an issue allows, the
issue is shortened and links to the full digest in `digests/`.

**Summarise with Copilot.** Every article, in both the ranked and the unranked digest, has a
**✨ Summarise with Copilot** link. It opens [GitHub Copilot Chat](https://github.com/copilot) with
a prompt asking for a high-level summary of the article: what's new, 3–5 key points and why it
matters. This needs no API key and uses your own Copilot plan.

- Issues can't show real buttons, so it's a link.
- The link can't choose a model. Copilot Chat uses your current model picker selection, which is
  **Auto** unless you changed it.
- Copilot needs web access (Bing search) to read the article. Otherwise it may only answer from
  the URL or the title.
- When an issue has to be shortened, middle-tier excerpts are dropped first. Then collapsed-list
  articles lose this link, together with their checkboxes.

**Giving feedback** on an `ai-news` issue:

- **Tick boxes.** Tick **👍 more like this** or **👎 less like this** under any article. This
  sends no notifications.
- **Comment commands.** Comment `/more agents`, `/less source:Databricks`, `/mute topic:hiring`,
  `/reset topic:agents`, or `/reset` to clear all feedback. A bare word means a topic.
- **Format labels.** Add the label `too-long` or `too-short` to change the number of top picks,
  or `too-much-product` / `too-much-research` to adjust the balance.

The [`AI News Feedback`](.github/workflows/feedback.yml) workflow records every change in
`feedback/events.jsonl` and rebuilds `feedback/profile.json`. Each digest run also re-reads the
last 4 weeks of issues first, in case a feedback run was missed.

Only the repository owner's feedback counts. To allow more users, set the `FEEDBACK_USERS`
variable to a comma-separated list of logins, for example if the repository belongs to an
organisation.

Feedback fades by 20% per week, so newer feedback counts more. `/more`, `/less` and `/mute`
override what was learned from the boxes.

The profile and feedback log are committed, so they are public if the repository is public.

## Usage

```bash
pip install -r requirements.txt
python -m news_aggregator --period weekly          # writes digests/weekly/YYYY-Www.md
python -m news_aggregator --period daily --stdout  # generate a one-off daily digest for inspection
python -m news_aggregator --period scheduled --timezone Asia/Kolkata  # what the workflow runs
```

Options: `--send-hour` (default 8) and `--weekly-day` (default `sunday`) change the schedule.

- `--ranked` builds the personalised digest with tiers and feedback boxes.
- `--top-n` sets the number of top picks.
- `--issue-dir` also writes a version of each digest that fits in an issue.

```bash
python -m news_aggregator --period weekly --ranked --stdout       # ranked digest
python -m news_aggregator.feedback sync --repo owner/name         # pull feedback from issues (needs GH_TOKEN)
python -m news_aggregator.feedback rebuild                        # rebuild profile.json from events.jsonl
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
