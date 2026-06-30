# Contributing

The full working agreement is in [`AGENTS.md`](AGENTS.md); decisions are logged in
[`docs/decisions.md`](docs/decisions.md). The short version:

## Flow
1. Take a GitHub Issue labelled **`ready`**; self-assign + label **`in-progress`**.
2. Branch `feat/<issue#>-slug`. **One feature per PR.**
3. Write code **+ offline tests**; make `uv run pytest` green.
4. Open a PR: `Closes #N`, what you did, and **what's verified offline vs needs
   live verification**. Label `needs-local-verification` if it touches GitHub
   Projects / Telegram / live data / real LLM output.
5. **Don't merge.** CI gates the PR; the maintainer live-verifies and merges.

## Local setup
```bash
uv sync                      # install deps (incl. dev: pytest)
uv run pytest                # the offline suite — needs no secrets
cp .env.example .env         # then fill in for live use (GitHub/Projects/Telegram/LLM)
```
- GitHub Projects (the `feed`'s initiative source) needs a token with the
  `read:project` scope — see `DAILY_AGENT_GITHUB_PROJECT_*` in `.env.example`.
- Live commands (`feed`, `feed-preview`, `collect`, `telegram-check`) need a
  populated `.env`; tests never do.

## Conventions
- Match the surrounding code's style and comment density.
- End commits with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- Record durable decisions in `docs/decisions.md` / `ROADMAP.md` — never only in
  chat. A cloud agent starts with only the repo.
