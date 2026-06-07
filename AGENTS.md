# Working agreement for agents & collaborators

This file is the contract every contributor follows — human or AI, local or
cloud. Claude Code reads it automatically. Read it before starting work.

## The collaboration model

```
groomed Issue ─▶ agent claims it ─▶ branch + code + offline tests
   ─▶ PR (links the issue) ─▶ CI runs pytest ─▶ 🟢
   ─▶ maintainer live-verifies on the main computer ─▶ merge.
   main stays protected the whole time.
```

There are two execution environments, and the split is real — it's baked into
the test suite (everything is mocked, so it runs anywhere):

| | A cloud / remote agent | The maintainer's main computer |
|---|---|---|
| Write code + offline tests | ✅ | ✅ |
| Run `uv run pytest` (all mocked) | ✅ | ✅ |
| Verify against **live Huly / Telegram / real data** | ❌ no secrets | ✅ |
| Merge to `main`; run the scheduled feed | ❌ | ✅ |

A cloud agent's whole job is: **pick a `ready` issue → do it → open a PR → stop.**
It never merges. The maintainer merges after any needed live verification.

## Picking up work

- Work lives in **GitHub Issues**, not `ROADMAP.md`. `ROADMAP.md` is the strategy
  / vision / decisions doc; Issues are the claimable units.
- Take an issue labelled **`ready`**. Self-assign it and add **`in-progress`** so
  no one duplicates it. If it's unclear or underspecified, ask / refine the issue
  first — don't guess.
- Prefer issues in an `area:` you won't collide with others on. Keep parallel
  work in different modules where possible.

## Definition of Done (every PR)

1. **One feature per PR.** Branch name `feat/<issue#>-slug` (or `fix/`, `chore/`,
   `docs/`).
2. **Offline tests for new logic**, and `uv run pytest` is green. Tests must not
   need network, secrets, or a live LLM — mock those (see existing tests for the
   patterns: `httpx.MockTransport`, monkeypatched agent calls, `tmp_path` SQLite).
3. **PR description** links the issue (`Closes #N`), says what you did, and
   **separates what you verified offline from what still needs live verification**.
4. If the change touches **Huly, Telegram, the live DB, or real LLM output**, add
   the **`needs-local-verification`** label so the maintainer knows to test it
   live before merging.
5. **Durable decisions go into the repo** — `ROADMAP.md` for strategy, `docs/
   decisions.md` for the log. Never leave a decision only in chat or an agent's
   private memory; the next agent starts with only the repo.
6. End commit messages with the co-author trailer:
   `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
7. **Do not merge to `main`.** Open the PR and stop. The maintainer merges.

## Guardrails

- `main` is protected: PRs only, CI must be green, no direct pushes.
- CI runs the **offline** suite only (no secrets in CI, ever — especially not the
  company Huly credentials).
- Keep PRs small; small blast radius, fewer conflicts, easier live-verification.

## Project map (where things live)

- `src/daily_agent/feed/` — the delivery feed pipeline (outbox, delta, initiative
  resolver/catalog/mapping, story-state, storyteller, pacer, channels).
- `src/daily_agent/agents/` — the LLM agents (summarizer, person brief, chapter
  writer, initiative mapper, assistant, docs Q&A) + model wiring.
- `src/daily_agent/sources/` — GitHub, Huly (via `bridges/huly/` Node bridge),
  Outline clients.
- `src/daily_agent/{cli,config,storage,cache,models,team,deliver}.py` — CLI and
  core plumbing.
- `tests/` — all offline. `ROADMAP.md` — strategy. `docs/decisions.md` — decision log.

See `CONTRIBUTING.md` for the short version and local setup.
