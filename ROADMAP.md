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

**Architecture — a staged pipeline over SQLite (2026-06-06 brainstorm):**

The data-flow is `pull → store → detect-new → generate → pace → deliver`, but the
key decision is to make it a **pipeline of durable, idempotent stages**, NOT one
linear job. Each arrow is a table; each stage only reads the previous table and
writes its own; any stage can crash and resume without redoing the others.

```
 collectors        differ        renderer       pacer        sender
 GitHub ─┐        (pure SQL,    (LLM, once     (policy      (Slack,
 Huly  ──┼─► raw_facts ─► deltas ──► per delta) ─► outbox ──► over outbox) ─► ledger
 Outline ┘  (mirror)   (what's new)  bites          (queue)                  +watermark
```

Why staged, not monolithic — the traps it avoids:
- **Generation must not sit on the delivery retry path.** LLM output is slow,
  costs money, nondeterministic. Render once → persist text → deliver the *stored*
  text. A failed Slack send retries the send, never re-calls the model.
- **"Generate from all pulled info" duplicates everything.** The **differ** (pure,
  cheap, no LLM) is what decides *what's new* — diff `raw_facts` vs
  watermark/ledger. This is the difference between a feed and a digest.
- **One stable dedup key flows end-to-end** (`PR repo#num@merged`,
  `ENG-1234@in-review`) so any stage re-running collapses onto the same row.
- **Per-source isolation:** each collector advances its own watermark; Huly down →
  GitHub bites still flow.

Tables:
- `raw_facts` — normalized source mirror, upserted by natural key
  (`source, external_id, updated_at`). The existing `pull_requests`/`commits`
  store is the seed of this.
- `source_state` — `source → last_polled, cursor, last_error` (watermark +
  isolation + visible failure).
- `deltas` — candidate bites: `(dedup_key UNIQUE, subject, kind, payload_json,
  detected_at, status)`. Re-running the differ is a no-op via the unique key.
- `bites` — rendered text: `(dedup_key, content, content_hash, model,
  rendered_at)`. Memoized by content hash → never pay the LLM twice.
- `outbox` — `(id, dedup_key, channel, content, status, attempts, not_before,
  created_at, sent_at, last_error)`. Queue + retry/backoff (`not_before` =
  next-attempt time). Status state machine:
  `pending → rendered → released → sent | failed → dead`.
- `delivered_ledger(item_key PRIMARY KEY, sent_at)` — exact "already sent" guard.
- `watermark(subject PRIMARY KEY, last_sent_at)` — what the differ diffs against.

**Control:** a single scheduler tick runs the stages in order
(`collect → diff → render → pace → send`), each draining its input table. Because
every stage is idempotent + durable, the tick can crash anywhere and the next tick
resumes correctly. Start with this **single sequenced tick**; split into
independent per-stage loops later (more robust, more moving parts) *without changing
the tables*.

**Can it all be done in SQL? — Yes, and it should be.** This is durable queues +
state machines + dedup + watermarks, exactly what a transactional relational store
is for. SQLite is ideal (single user, single host, zero ops, ACID):
- queue+status → `status` column + `UPDATE … WHERE status=…`
- dedup / never-duplicate → `UNIQUE(dedup_key)` + `INSERT … ON CONFLICT DO NOTHING`
- at-least-once → mark `sent` **and** insert ledger row in the *same transaction*
- retry/backoff → `attempts` + `not_before`; sender picks
  `status='pending' AND not_before <= now`
No broker (Kafka/Rabbit/Redis) — those solve multi-consumer, multi-host, high
throughput; we have one consumer, one host, low volume. Keep the sender
single-threaded (or add a `claimed_by`/`claimed_at` lease) so two ticks can't
drain the same row.

**Is Qdrant / a vector DB relevant? — Not for delivery. No.** Delivery is *exact,
deterministic identity* (dedup by `repo#num@merged`), the opposite of vector
*similarity*. Fuzzy matching would make "never duplicate / never lose" worse and
add an embedding-model + server dependency for zero gain on the delivery path.
Vector search, *if ever*, belongs in the **Q&A / retrieval layer**, not here:
semantic search over Outline docs (only if Outline's own search proves too weak),
recall over months of historical `raw_facts`, or semantic anti-repeat (exact-key
ledger already covers the real cases). **Defer Qdrant until there's a concrete
retrieval problem keyword search can't solve** — adding it now is complexity
shopping for a problem we don't have.

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
