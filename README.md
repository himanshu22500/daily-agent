# daily-agent

AI agents that watch your organization's repos and tell you what's being worked
on — plus an on-demand deep-dive that explains the **business-logic layer** of
any project by pulling together code, tasks, and docs.

## What it does

1. **Watches your org's GitHub repos.** Collects recent pull requests and
   commits and accumulates them in a local SQLite store, so you build up a
   running history of activity over time.
2. **Summarizes what's happening.** An LLM agent turns that raw activity into a
   plain-language, cross-project digest: what's shipping, what's in flight, who's
   driving each project.
3. **Ask anything, on demand.** A tool-using agent answers free-form questions —
   about a person, a project, a topic, or the daily report — by investigating
   across repos (code + PRs) and Outline docs, resolving people via the team map.
   It finds the right sources itself; pin a repo with `--repo` if you want.
4. **Answers from your docs.** Ask a how-to / setup / "how does X work" question
   and a docs-first agent searches Outline, reads the relevant documents, and
   synthesizes a cited, step-by-step answer (and tells you honestly when the
   docs don't cover it).

The LLM is **provider-agnostic** (built on [Pydantic AI](https://ai.pydantic.dev)):
pick any model via a `provider:model` string.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then fill in GitHub token/org and your LLM provider key
```

In `.env`, set at minimum:

- `DAILY_AGENT_MODEL` — e.g. `anthropic:claude-sonnet-4-6` (and the matching
  provider key, e.g. `ANTHROPIC_API_KEY`). Any provider works via a
  `provider:model` string; OpenAI codex / gpt-5 reasoning models use the
  `openai-responses:` prefix (e.g. `openai-responses:gpt-5-codex`).
- `DAILY_AGENT_FAST_MODEL` — optional cheaper/faster model for bulk synthesis
  (`summary` + per-person briefs), e.g. `anthropic:claude-haiku-4-5`. Deep
  `ask`/`chat` keep using `DAILY_AGENT_MODEL`.
- `DAILY_AGENT_GITHUB_TOKEN` — token with read access to the org's repos.
  Tip: if you use the `gh` CLI, `gh auth token` prints a usable token.
- `DAILY_AGENT_GITHUB_ORG` — your org login
- `DAILY_AGENT_GITHUB_REPOS` — optional comma-separated allowlist (empty = all)

Optional, to enable the docs commands (`docs`, `howto`) and doc-grounded deep dives:

- `DAILY_AGENT_OUTLINE_URL` — your Outline base URL (e.g. `https://outline.yourco.com`)
- `DAILY_AGENT_OUTLINE_TOKEN` — an Outline API token (`ol_api_…`)

Optional, to enable the **rich initiative feed** — `feed` / `feed-preview` deliver
per-initiative storyline chapters (grouped by the org's GitHub Project board)
instead of mechanical per-PR bites. Point it at the project:

- `DAILY_AGENT_GITHUB_PROJECT_NUMBER` — the project number from its URL (`…/projects/<N>`)
- `DAILY_AGENT_GITHUB_PROJECT_OWNER` — defaults to `DAILY_AGENT_GITHUB_ORG`
- The board is read over GitHub's GraphQL API, which needs the **`read:project`**
  scope. If `DAILY_AGENT_GITHUB_TOKEN` lacks it, set `DAILY_AGENT_GITHUB_PROJECT_TOKEN`
  to a project-scoped token (`gh auth token` prints one if you use the `gh` CLI).

Optional, to enable person-centric queries (`brief`):

```bash
cp team.example.json team.json   # then edit: maps name -> GitHub login
```

`team.json` is **gitignored** (it holds names/handles — PII). Set
`DAILY_AGENT_ME` to the canonical name that "me" should resolve to.

## Usage

```bash
# See which repos are being watched
uv run daily-agent repos

# Gather recent activity into the store (run on a schedule)
uv run daily-agent collect --days 1

# Read a cross-project digest of accumulated activity
uv run daily-agent summary --days 7

# Ask anything — the agent finds the right repos/docs/people itself
uv run daily-agent ask "How does refund handling work, and what changed recently?"
uv run daily-agent ask "double-click on what Sharad is working on in Routing V2"
uv run daily-agent ask "summarize the report and tell me who's blocked"
uv run daily-agent ask "explain the v3 migration" --repo tranzact-v2   # optionally pin a repo

# Interactive session — follow-ups keep context ("go deeper on that")
uv run daily-agent chat

# Search the engineering docs (Outline) directly
uv run daily-agent docs "settings v3 migration"

# Ask a how-to / setup question answered from the docs (reads + synthesizes steps)
uv run daily-agent howto "how do I set up the comms service?"

# Preview the rich initiative feed — groups recent PRs into per-initiative
# storyline chapters by the GitHub Project board (read-only; nothing delivered)
uv run daily-agent feed-preview --days 14

# Run the full daily job: collect -> digest -> per-person briefs -> digests/<date>.md
uv run daily-agent daily

# What someone is working on lately — a synthesized briefing + their PRs
uv run daily-agent brief                # me
uv run daily-agent brief "Harshit"      # any teammate by name/handle
uv run daily-agent brief "Sharad" --no-ai   # skip the LLM summary, list only
```

### Commands

| Command | What it does | Hits the LLM? |
|---|---|---|
| `repos` | List the org repos being watched (active in the last N days) | No |
| `collect --days N` | Fetch recent PRs + commits from GitHub into the local store (idempotent; run on a schedule) | No |
| `summary --days N` | Synthesize accumulated activity into a cross-project digest | Yes |
| `ask "question"` | Ask anything; the agent investigates across repos, PRs, docs, and people (optionally pin `--repo`) | Yes |
| `chat` | Interactive session over the same tools; follow-ups keep context (`/reset` to clear, `exit` to leave) | Yes |
| `docs "query"` | Fast full-text search of the Outline knowledge base (titles + links) | No |
| `howto "question"` | Reads the relevant Outline docs and synthesizes a cited, step-by-step answer | Yes |
| `brief [PERSON]` | Synthesized briefing of what a person is working on + their PRs (defaults to "me"; `--no-ai` to skip the summary) | Yes (unless `--no-ai`) |
| `feed` / `feed-preview` | Deliver (or dry-run) the paced initiative feed: recent PRs grouped into storyline chapters by the GitHub Project board | Yes |
| `daily` | The full daily job: collect → cross-project digest → per-person briefs → writes `digests/<date>.md` | Yes |
| `cache [--clear]` | Inspect or clear the response cache | No |

`collect` and `summary` are split on purpose: `collect` only touches GitHub and
*accumulates* history in SQLite, so you can `summary` over any window later.
`ask`/`docs`/`howto` query their sources live (through the cache) and need no
prior `collect`.

### Caching

Source responses are cached in SQLite so repeated calls don't re-pull. The TTL
policy mirrors what can actually change:

- **Terminal entities cache forever** — a **merged PR** never changes, so it's
  stored permanently.
- **Everything else uses a TTL** — open PRs, project lists, and search results
  (`DAILY_AGENT_GITHUB_CACHE_TTL` / `DAILY_AGENT_PROJECTS_CACHE_TTL`, default 10 min).
- **Outline docs use a long TTL** (`DAILY_AGENT_OUTLINE_CACHE_TTL`, default 7
  days) since they rarely change.

Inspect with `daily-agent cache`; reset with `daily-agent cache --clear`; disable
with `DAILY_AGENT_CACHE_ENABLED=false`.

### Other performance

- **Parallel collection & briefs** — `collect` fetches repos concurrently, and
  `daily` runs the per-person briefs concurrently (bounded), so the report isn't
  gated by N sequential LLM calls.
- **Prompt caching** — for Anthropic models, the system prompt + tool schemas +
  conversation are cached, cutting cost/latency on repeated `ask`/`chat` turns.
- **Model tiering** — `DAILY_AGENT_FAST_MODEL` runs the high-volume synthesis
  (summary/briefs) on a cheaper model while `ask`/`chat` stay on the strong one.

## Scheduling (daily, via launchd)

Run the daily job automatically each morning on macOS:

```bash
scripts/install-launchd.sh 9      # run at 09:00 (default 9)
launchctl start com.daily-agent.daily   # test it now
```

The digest lands in `digests/<date>.md` (gitignored — it contains names + work
summaries). Logs go to `digests/launchd.{out,err}.log`. The installer bakes your
current `PATH` into the job so `uv` resolves under launchd. Uninstall with
`launchctl unload ~/Library/LaunchAgents/com.daily-agent.daily.plist`.

> Runs only while your Mac is awake. For always-on cloud scheduling you'd move
> secrets into a CI provider — deferred for now.

### Inbound follow-ups (reply to the feed)

Reply to any bite in your Telegram feed channel and a long-poll listener picks it
up. It's a persistent process (it long-polls `getUpdates`), so it runs under a
`KeepAlive` launchd job rather than on an interval:

```bash
scripts/install-listen-launchd.sh           # keeps the listener running
daily-agent telegram-listen                  # or run it in the foreground
launchctl unload ~/Library/LaunchAgents/com.daily-agent.listen.plist   # stop
```

It identifies which replies are genuine follow-ups (a reply to a bite we sent,
not one of the bot's own posts) using the message ids the outbox records, grounds
the answer in the replied-to bite plus its initiative story-state, and posts the
answer threaded under your reply. The listener survives restarts via a durable
offset. Logs go to `digests/listen.{out,err}.log`.

## Architecture

```
src/daily_agent/
  config.py            env-driven settings (DAILY_AGENT_* prefix)
  models.py            Pydantic models: PullRequest, Commit, RepoActivity, ActivityDigest
  storage.py           SQLite store (idempotent upsert; activity accrues over time)
  sources/
    github.py          GitHub REST collection (real)
    outline.py         engineering docs — Outline API (real)
    github_projects.py GitHub Projects v2 board — GraphQL (real); feeds initiatives
  agents/
    summarizer.py      Pydantic AI agent -> cross-project digest
    assistant.py       tool-using agent -> ask anything (all sources + people); powers `ask` + `chat`
    docs_qa.py         docs-first Q&A agent -> answers from Outline
    person_brief.py    synthesizes what one person is working on (for `brief`)
  team.py              team identity map (name <-> GitHub); powers `brief`
  cache.py             SQLite response cache (terminal entities permanent; else TTL)
  deliver.py           render a digest to Markdown + write digests/<date>.md
  cli.py               collect / summary / ask / chat / repos / docs / howto / feed / brief / daily
scripts/
  run-daily.sh         wrapper the scheduler calls
  install-launchd.sh   install the macOS launchd job

```

## Status

🚧 Early but working end-to-end: `repos`, `collect`, `summary`, `ask`, `docs`,
and `howto` all run against real data today.

### Done

- [x] Framework/runtime — Python (uv) + Pydantic AI, provider-agnostic LLM
- [x] GitHub source — list org repos, recent PRs/commits, README/tree/file readers
- [x] SQLite store — idempotent upserts so activity accrues over time (tested)
- [x] Summarizer agent — cross-project `ActivityDigest`
- [x] Repo deep-dive researcher — business-logic layer (code + PRs + docs)
- [x] Outline integration — search + read, wired into the deep dive
- [x] Docs Q&A agent (`howto`) — finds, reads, and synthesizes answers from docs
- [x] GitHub Projects (v2) source — the feed's initiative model (sub-issue parent
      chains + closing-PR links, read over GraphQL)
- [x] OpenAI codex / Responses-API support (`openai-responses:` model prefix)
- [x] Offline test suite (storage + Outline client via httpx mock)

- [x] Person briefs (`brief`) + team identity mapping + synthesized summary
- [x] **Daily job + scheduling** — `daily` (collect → digest → per-person briefs →
      `digests/<date>.md`), scheduled via launchd (`scripts/install-launchd.sh`)

### Left

- [ ] More delivery backends — Slack webhook / email (file delivery shipped)
- [ ] Always-on cloud scheduling (CI) — needs secrets moved off this machine
- [ ] Optional niceties — repo activity ranking, per-command model selection (cheaper model for `summary`)

## License

Personal project.
