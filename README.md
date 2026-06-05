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
   across repos (code + PRs), Huly tasks, and Outline docs, resolving people via
   the team map. It finds the right sources itself; pin a repo with `--repo` if
   you want.
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

Optional, to enable the Huly task tracker (`tasks` command + task context in deep dives).
Huly has no Python SDK, so this uses a small Node bridge — install it once:

```bash
cd bridges/huly && yarn install && cd -
```

- `DAILY_AGENT_HULY_WORKSPACE` — your workspace name (from the workspace URL)
- `DAILY_AGENT_HULY_EMAIL` + `DAILY_AGENT_HULY_PASSWORD` — or `DAILY_AGENT_HULY_TOKEN`
- `DAILY_AGENT_HULY_URL` — defaults to `https://huly.app`

Optional, to enable person-centric queries (`brief`, `tasks --assignee me`):

```bash
cp team.example.json team.json   # then edit: maps name -> Huly name + GitHub login
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

# Ask anything — the agent finds the right repos/tasks/docs/people itself
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

# List Huly issues (defaults to DAILY_AGENT_HULY_DEFAULT_PROJECT)
uv run daily-agent tasks
uv run daily-agent tasks ENG --limit 50   # a specific project
uv run daily-agent tasks --projects       # list projects instead

# Filter by status / assignee / priority (combinable)
uv run daily-agent tasks --status "In Review"
uv run daily-agent tasks --assignee "Himanshu" --priority high

# Show one task's details (status, assignee, description, linked PRs)
uv run daily-agent task ENG-16845

# Run the full daily job: collect -> digest -> per-person briefs -> digests/<date>.md
uv run daily-agent daily

# What someone is working on lately — a synthesized briefing + their tasks/PRs
uv run daily-agent brief                # me
uv run daily-agent brief "Harshit"      # any teammate by name/handle
uv run daily-agent brief "Sharad" --no-ai   # skip the LLM summary, list only
uv run daily-agent tasks --assignee me  # filters also accept "me" / a name
```

### Commands

| Command | What it does | Hits the LLM? |
|---|---|---|
| `repos` | List the org repos being watched (active in the last N days) | No |
| `collect --days N` | Fetch recent PRs + commits from GitHub into the local store (idempotent; run on a schedule) | No |
| `summary --days N` | Synthesize accumulated activity into a cross-project digest | Yes |
| `ask "question"` | Ask anything; the agent investigates across repos, PRs, Huly tasks, docs, and people (optionally pin `--repo`) | Yes |
| `chat` | Interactive session over the same tools; follow-ups keep context (`/reset` to clear, `exit` to leave) | Yes |
| `docs "query"` | Fast full-text search of the Outline knowledge base (titles + links) | No |
| `howto "question"` | Reads the relevant Outline docs and synthesizes a cited, step-by-step answer | Yes |
| `tasks [PROJECT]` | List Huly issues (defaults to configured project; filter by `--status`/`--assignee`/`--priority`; `--projects` lists projects) | No |
| `task ID` | Show one Huly task's details + linked GitHub PRs | No |
| `brief [PERSON]` | Synthesized briefing of what a person is working on + their Huly tasks/PRs (defaults to "me"; `--no-ai` to skip the summary) | Yes (unless `--no-ai`) |
| `daily` | The full daily job: collect → cross-project digest → per-person briefs → writes `digests/<date>.md` | Yes |
| `cache [--clear]` | Inspect or clear the response cache | No |

`collect` and `summary` are split on purpose: `collect` only touches GitHub and
*accumulates* history in SQLite, so you can `summary` over any window later.
`ask`/`docs`/`howto` query their sources live (through the cache) and need no
prior `collect`.

### Caching

Source responses are cached in SQLite so repeated calls don't re-pull. The TTL
policy mirrors what can actually change:

- **Terminal entities cache forever** — a **merged PR** and a **DONE Huly issue**
  never change, so they're stored permanently.
- **Everything else uses a TTL** — open issues, lists, and search results
  (`DAILY_AGENT_HULY_CACHE_TTL` / `GITHUB_CACHE_TTL`, default 10 min).
- **Outline docs use a long TTL** (`DAILY_AGENT_OUTLINE_CACHE_TTL`, default 7
  days) since they rarely change.

This also sidesteps the per-call Huly bridge spawn — a warm `tasks`/`brief` is
several times faster. Inspect with `daily-agent cache`; reset with
`daily-agent cache --clear`; disable with `DAILY_AGENT_CACHE_ENABLED=false`.

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
current `PATH` into the job so `uv` and `node` (the Huly bridge) resolve under
launchd. Uninstall with `launchctl unload ~/Library/LaunchAgents/com.daily-agent.daily.plist`.

> Runs only while your Mac is awake. For always-on cloud scheduling you'd move
> secrets into a CI provider — deferred for now.

## Architecture

```
src/daily_agent/
  config.py            env-driven settings (DAILY_AGENT_* prefix)
  models.py            Pydantic models: PullRequest, Commit, RepoActivity, ActivityDigest
  storage.py           SQLite store (idempotent upsert; activity accrues over time)
  sources/
    github.py          GitHub REST collection (real)
    outline.py         engineering docs — Outline API (real)
    huly.py            task tracker — shells out to the Node bridge (real)
  agents/
    summarizer.py      Pydantic AI agent -> cross-project digest
    assistant.py       tool-using agent -> ask anything (all sources + people); powers `ask` + `chat`
    docs_qa.py         docs-first Q&A agent -> answers from Outline
    person_brief.py    synthesizes what one person is working on (for `brief`)
  team.py              team identity map (name <-> Huly <-> GitHub); powers `brief`
  cache.py             SQLite response cache (terminal entities permanent; else TTL)
  deliver.py           render a digest to Markdown + write digests/<date>.md
  cli.py               collect / summary / ask / chat / repos / docs / howto / tasks / task / brief / daily
scripts/
  run-daily.sh         wrapper the scheduler calls
  install-launchd.sh   install the macOS launchd job
bridges/
  huly/                Node bridge: reads Huly via @hcengineering SDK, emits JSON

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
- [x] Huly (task tracking) — Node bridge (`@hcengineering` SDK) + `tasks` command,
      wired into the deep dive for issue/status context
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
