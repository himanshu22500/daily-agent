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
3. **Deep-dives on demand.** Point it at one project and ask a question; a
   tool-using agent inspects the repo (README, structure, key files, recent PRs)
   and your Outline engineering docs — and, once connected, Huly tasks — to
   explain the domain and the *why* behind recent changes.
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
- `DAILY_AGENT_GITHUB_TOKEN` — token with read access to the org's repos.
  Tip: if you use the `gh` CLI, `gh auth token` prints a usable token.
- `DAILY_AGENT_GITHUB_ORG` — your org login
- `DAILY_AGENT_GITHUB_REPOS` — optional comma-separated allowlist (empty = all)

Optional, to enable the docs commands (`docs`, `howto`) and doc-grounded deep dives:

- `DAILY_AGENT_OUTLINE_URL` — your Outline base URL (e.g. `https://outline.yourco.com`)
- `DAILY_AGENT_OUTLINE_TOKEN` — an Outline API token (`ol_api_…`)

## Usage

```bash
# See which repos are being watched
uv run daily-agent repos

# Gather recent activity into the store (run on a schedule)
uv run daily-agent collect --days 1

# Read a cross-project digest of accumulated activity
uv run daily-agent summary --days 7

# Deep-dive into one project's business logic
uv run daily-agent ask payments-service "How does refund handling work, and what changed recently?"

# Search the engineering docs (Outline) directly
uv run daily-agent docs "settings v3 migration"

# Ask a how-to / setup question answered from the docs (reads + synthesizes steps)
uv run daily-agent howto "how do I set up the comms service?"
```

### Commands

| Command | What it does | Hits the LLM? |
|---|---|---|
| `repos` | List the org repos being watched (active in the last N days) | No |
| `collect --days N` | Fetch recent PRs + commits from GitHub into the local store (idempotent; run on a schedule) | No |
| `summary --days N` | Synthesize accumulated activity into a cross-project digest | Yes |
| `ask REPO "question"` | Code-first deep dive into one project (repo + PRs + Outline docs) | Yes |
| `docs "query"` | Fast full-text search of the Outline knowledge base (titles + links) | No |
| `howto "question"` | Reads the relevant Outline docs and synthesizes a cited, step-by-step answer | Yes |

`collect` and `summary` are split on purpose: `collect` only touches GitHub and
*accumulates* history in SQLite, so you can `summary` over any window later.
`ask`/`docs`/`howto` query their sources live and need no prior `collect`.

## Architecture

```
src/daily_agent/
  config.py            env-driven settings (DAILY_AGENT_* prefix)
  models.py            Pydantic models: PullRequest, Commit, RepoActivity, ActivityDigest
  storage.py           SQLite store (idempotent upsert; activity accrues over time)
  sources/
    github.py          GitHub REST collection (real)
    outline.py         engineering docs — Outline API (real)
    huly.py            task tracker (stub — pending access)
  agents/
    summarizer.py      Pydantic AI agent -> cross-project digest
    researcher.py      tool-using Pydantic AI agent -> repo deep dive
    docs_qa.py         docs-first Q&A agent -> answers from Outline
  cli.py               collect / summary / ask / repos / docs / howto
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
- [x] OpenAI codex / Responses-API support (`openai-responses:` model prefix)
- [x] Offline test suite (storage + Outline client via httpx mock)

### Left

- [ ] **Huly (task tracking)** — needs a small Node bridge using
      `@hcengineering/api-client` (no Python SDK exists); will add sprint/issue
      context to digests and deep dives
- [ ] **Scheduling** — recurring `collect` + digest delivery (cron / CI / other —
      deferred pending a delivery target: terminal, file, email, Slack…)
- [ ] **Digest delivery & history** — write dated digests to a file/channel
- [ ] Optional niceties — a one-shot `brief` (collect + summary), repo activity
      ranking, per-command model selection (cheaper model for `summary`)

## License

Personal project.
