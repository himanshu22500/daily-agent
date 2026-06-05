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

The LLM is **provider-agnostic** (built on [Pydantic AI](https://ai.pydantic.dev)):
pick any model via a `provider:model` string.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then fill in GitHub token/org and your LLM provider key
```

In `.env`, set at minimum:

- `DAILY_AGENT_MODEL` — e.g. `anthropic:claude-sonnet-4-5` (and the matching
  provider key, e.g. `ANTHROPIC_API_KEY`)
- `DAILY_AGENT_GITHUB_TOKEN` — token with read access to the org's repos
- `DAILY_AGENT_GITHUB_ORG` — your org login
- `DAILY_AGENT_GITHUB_REPOS` — optional comma-separated allowlist (empty = all)

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

🚧 Early but working: GitHub collection, summarization, and deep-dive run today.

- [x] Choose framework/runtime — Python + Pydantic AI (provider-agnostic)
- [x] First agent: GitHub activity collector + summarizer
- [x] Deep-dive researcher (business-logic layer)
- [x] Outline (engineering docs) integration — search + read, wired into deep dive
- [ ] Huly (task tracking) integration — *needs a small Node bridge (@hcengineering/api-client)*
- [ ] Scheduling / recurring execution (deferred — decide cron vs CI vs other)

## License

Personal project.
