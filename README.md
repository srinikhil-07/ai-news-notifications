# ai-news-notifications
built with agents to get ai news aggregated from ai tech company blogs

Collects posts from AI blogs at big tech companies and AI startups and builds Markdown digests
split into **Research** and **Product & Announcements** sections:

- **Weekly** on Sunday at 08:00: posts from the previous 7 days.

Each digest with new posts is opened as a GitHub **issue** (label `ai-news`) and assigned to the
repository owner, so GitHub emails it to you.

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

## Summarise with Copilot

Every article in the digest has a **✨ Summarise with Copilot** link. It opens
[GitHub Copilot Chat](https://github.com/copilot) with a prompt asking for a summary of the
article: key points and why it matters. Links first visit this repository's GitHub Pages
landing page, which forwards the prompt to Copilot in the browser.

- It runs on the Copilot plan of whoever clicks it. The repository calls no API and needs no key.
- Issues can't show real buttons, so it's a link.
- The link can't choose a model. Copilot Chat uses your current model picker selection, which is
  **Auto** unless you changed it.
- Copilot needs web access (Bing search) to read the article. Otherwise it may only answer from
  the URL or the title.

### iPhone and iPad

New digest links use `srinikhil-07.github.io` instead of `github.com` so GitHub Mobile
can open them as external browser links, like article links. The page automatically
forwards to the supported `https://github.com/copilot?prompt=…` URL. The prompt is
passed to Pages in the URL fragment (not sent to the Pages server); the page loads
no third-party scripts and only redirects to Copilot.

**Enable the landing page:** in **Settings → Pages → Build and deployment → Source**,
select **GitHub Actions**. After merging, run **Actions → Deploy Copilot landing page → Run workflow**
on `main` if it hasn't deployed automatically. Confirm
`https://srinikhil-07.github.io/ai-news-notifications/copilot.html` loads before generating new digests.
Without this deployment, new summary links will return 404.

The redirect cannot guarantee Safari stays open: iOS and GitHub Mobile still control
Universal Links. If the app opens or shows "organization not found", copy the
**Summarise with Copilot** link and paste it into **Safari's address bar**, then sign in.
The landing page also provides a continuation link and a selectable prompt if you return
to it after a redirect. Links in existing issues and archived digests are unchanged;
paste those older direct Copilot links into Safari.

GitHub also documents [managing Universal Links on iOS](https://docs.github.com/en/get-started/using-github/github-mobile#managing-universal-links-for-github-mobile-on-ios):
long-press a GitHub link and choose **Open** to open subsequent GitHub links in Safari.
Choose **Open in GitHub** to switch back. This preference affects other GitHub links too.
Issue Markdown cannot force Safari or fix the GitHub app's internal routing. The landing
page is a best-effort browser workaround, not a change to GitHub Mobile.

## Usage

```bash
pip install -r requirements.txt
python -m news_aggregator --period weekly          # writes digests/weekly/YYYY-Www.md
python -m news_aggregator --period daily --stdout  # generate a one-off daily digest for inspection
python -m news_aggregator --period scheduled --timezone Asia/Kolkata  # what the workflow runs
```

Options: `--send-hour` (default 8) and `--weekly-day` (default `sunday`) change the schedule.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
