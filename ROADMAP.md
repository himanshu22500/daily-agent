# Roadmap / Ideas

A running backlog of improvements, captured for later. Not yet built unless it's
in the README's "Done" list.

## ⭐ NEXT UP: Delivery feed (the big one)

Reframe delivery from **one big batch report** (the current `daily` digest — a
heavy, "haunting", hard-to-read wall) to a **paced feed**: small, readable bites
pushed over time. This is considered the most important part of the project and
must be **robust** (never lose or duplicate a message).

**Decisions made (2026-06-06 brainstorm):**
- **Channel:** Slack — a personal feed (DM, or a private just-me channel — TBD).
- **Audience:** just me (leadership overview), tuned to what I care about.
- **Cadence:** *hybrid* — a couple of checkpoints (morning kickoff / EOD wrap)
  that flush queued bites, **plus** an event nudge when something notable lands
  between them (a merge, a task → In-Review, a likely blocker).
- **Bite unit:** **per-project** and **per-person** *rolling deltas* — each
  message says only what's new about that subject since the last time we
  messaged about it. (Not a raw event firehose.)
- **Pacing:** even at a checkpoint, bites trickle (spaced, or grouped under one
  Slack thread) so it never reads as a wall.

**Design sketch:**
- **Robustness core — an outbox** in SQLite:
  `outbox(id, kind, subject, content, dedup_key, status, attempts, created_at, sent_at)`
  plus `delivered_ledger(item_key)` and `watermark(subject -> last_sent_at)`.
  Enqueue bites → a sender drains them → mark `sent` only on success → retry
  with backoff → dedup via `dedup_key` + the ledger of item keys
  (PR `repo#num`, task `ENG-x@status`) so nothing repeats. A crash mid-send
  never double-sends or drops.
- **Delta engine:** diff current activity vs the watermark/ledger to produce only
  what's new per subject. This is what kills the noise.

**Phased plan (small PRs):**
1. Outbox + delivery ledger + delta engine — channel-agnostic, testable with a
   file/console "channel" to verify deduped bites before any Slack setup.
2. Slack delivery — render a bite as a Slack message; threading; quiet hours.
3. Cadence engine — checkpoints + event nudges, wired to the scheduler.
4. Reply-to-expand — react/reply to a bite → triggers `ask` on that subject
   (entry point to the deep-dive tools).

**Open questions to settle before building (user wanted to clarify):**
- Is per-project + per-person delta the right bite model, or a morning narrative
  that drips follow-ups?
- Exact definition of a "notable" event worth interrupting the day.
- Pacing specifics: bites/day, spacing, quiet hours, weekends.
- Slack mechanism: private-channel webhook (simple) vs bot token (true DM,
  enables reactions/reply-to-expand).
- Robustness bar: at-least-once + dedup (assumed); acknowledgements?
  edit-in-place vs new messages?
- Is reply-to-expand core or a later nice-to-have?

## Known gaps / fixes

- [x] **Single-task detail command** — added `task ENG-12345`: prints status,
      assignee, priority, description (markdown), and extracts linked GitHub PR
      URLs from the description.

- [x] **`tasks` default project** — `tasks` now lists issues for
      `DAILY_AGENT_HULY_DEFAULT_PROJECT` (set to `ENG`) by default; pass a project
      explicitly to override, or `--projects` to list projects.

## Conversational / cross-source Q&A

- [x] **Generalized `ask`** — no longer pinned to one repo. A single assistant
      agent (`agents/assistant.py`) with tools across repos/PRs, Huly, Outline,
      the team map, and the latest digest. Answers free-form questions: "ask
      about the report", "double-click on what <person> is working on", etc.
      Optionally pin a repo with `--repo`. (Superseded the repo-only researcher.)
- [x] **Interactive `chat`** — REPL over the same assistant agent that keeps
      conversation history, so follow-ups ("go deeper on that") have context.
      `/reset` clears history; `exit`/`quit` leaves. Optional `--repo` focus.

## Personal / task-centric Q&A

- [x] **"What is <person> working on this week?"** — added the `brief [PERSON]`
      command: pulls a person's Huly tasks (updated in the window) + their GitHub
      PRs. Defaults to "me". Backed by a team identity map (`team.py` +
      gitignored `team.json`) mapping name ↔ Huly ↔ GitHub. `tasks --assignee me`
      / `--assignee <name>` resolve through the same map.
      - [x] LLM synthesis: `brief` now leads with a synthesized summary
        (`agents/person_brief.py`) of the person's themes/intent, with the
        task/PR list kept below. `--no-ai` skips it. Degrades gracefully if the
        model call fails.

## Reliable task ↔ PR mapping

- [ ] **Today the task→PR mapping is LLM-inferred, not deterministic.** During
      `ask`, the agent connects Huly issues and GitHub PRs by reading text. Two
      real signals exist in the data:
        1. Explicit PR URLs inside Huly issue descriptions/comments (added by the
           `gh pr create` hook, `_gh_pr_create_with_huly`).
        2. `ENG-\d+` identifiers in PR titles, branch names, and commit messages.
      → Build a deterministic cross-linker:
        - Parse `ENG-\d+` from PR titles / branches / commit messages (GitHub side).
        - Parse GitHub PR URLs from Huly issue bodies/comments (Huly side).
          *(Partially done: the `task` command already extracts PR URLs from an
          issue's description via `_github_pr_links`.)*
        - Produce a real task ↔ PR(s) map that agents and `brief` can use directly,
          instead of relying on inference.

## Scheduling & delivery

- [x] **Daily job** — `daily` command: collect → cross-project digest →
      per-person briefs → Markdown. (`cli.daily` + `deliver.py`)
- [x] **File delivery & history** — writes `digests/<date>.md` (gitignored, PII).
- [x] **Recurring execution** — macOS launchd job (`scripts/install-launchd.sh`,
      `scripts/run-daily.sh`). Runs while the Mac is awake.
- [ ] More delivery backends — Slack incoming webhook, email (SMTP/Gmail).
- [ ] Always-on cloud scheduling (GitHub Actions) — requires moving secrets
      (incl. the Huly password) into CI + installing Node/the Huly bridge there.

## Caching

- [x] **Response cache** (`cache.py`, SQLite) so we don't re-pull external data
      each time. Policy mirrors what can change: **merged PRs and DONE Huly
      issues cache permanently**; open issues / lists / search use a TTL; Outline
      docs use a long TTL. Also avoids the per-call Huly bridge spawn (warm
      `tasks`/`brief` ~5× faster). `cache` / `cache --clear` to inspect/reset.
  - Possible follow-up: unify with the `pull_requests` store as a single
    read-through layer; a long-lived Huly bridge process.

- [x] **Parallelism** — `collect` fetches repos concurrently; `daily` runs
      per-person briefs concurrently (bounded) instead of sequentially.
- [x] **Prompt caching** — Anthropic system prompt + tool schemas + messages
      cached (`agents/model.py:cache_settings`), wired into all agents.
- [x] **Model tiering** — `DAILY_AGENT_FAST_MODEL` (e.g. Haiku) for summary +
      briefs; `ask`/`chat` stay on the strong `DAILY_AGENT_MODEL`.
  - Possible follow-up: memoize LLM outputs (digest/brief by date) + cache
    `list_repos`.

## Smaller niceties

- [ ] One-shot `brief` that runs `collect` + `summary` together.
- [ ] Rank projects in the digest by activity volume.
- [ ] Per-command model selection (e.g. a cheaper model for `summary`, a stronger
      one for `ask`) to manage cost.
