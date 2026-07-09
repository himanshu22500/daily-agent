# Roadmap / Strategy

This is the **strategy & decisions** doc — the arc, the "why", and the design
rationale. **Actionable work lives in GitHub Issues**, not here (see `AGENTS.md`);
durable decisions are logged in `docs/decisions.md`. Use this to understand
*where the project is going*; pick up *what to do next* from the Issues.

## ⭐ NEXT UP: Delivery feed (the big one)

Reframe delivery from **one big batch report** (the current `daily` digest — a
heavy, "haunting", hard-to-read wall) to a **paced feed**: small, readable bites
pushed over time. This is considered the most important part of the project and
must be **robust** (never lose or duplicate a message).

**Decisions made (2026-06-06 brainstorm):**
- **Channel: Telegram is the primary (and intended *only*) channel.** Decided
  after Slack hit a workspace-admin install gate. Reasons it's the right primary,
  not just a fallback:
    - No org/admin approval; one bot token is the entire credential; only
      outbound HTTPS to `api.telegram.org` (no ports, no public URL).
    - **Privacy:** the feed will grow to include personal/leadership notifications
      the user does *not* want on a shared workspace. A dedicated bot + a Telegram
      account used for nothing else keeps it fully isolated. **Do not route this
      feed through Slack or any shared workspace.**
    - More capable for this use case: inline buttons (clean reply-to-expand),
      `disable_notification` (built-in quiet hours), `editMessageText` (in-place
      rolling deltas), slash commands.
  - **Slack:** parked. The `SlackChannel` stays in the tree as an optional/team
    facing channel, but is not the path. (`feed_channel` defaults to `console` in
    the repo; the user sets `DAILY_AGENT_FEED_CHANNEL=telegram`.)
  - Telegram account hygiene (the user keeps this account single-purpose) is done
    by the user in the Telegram app — bot tokens are sandboxed and cannot touch
    account contacts/notifications. NOT something this project automates.
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
 GitHub ─┐        (pure SQL,    (LLM, once     (policy     (Telegram,
 Huly  ──┼─► raw_facts ─► deltas ──► per delta) ─► outbox ──► over outbox) ─► ledger
 Outline ┘  (mirror)   (what's new)  bites          (queue)                  +watermark
```

Why staged, not monolithic — the traps it avoids:
- **Generation must not sit on the delivery retry path.** LLM output is slow,
  costs money, nondeterministic. Render once → persist text → deliver the *stored*
  text. A failed send retries the send, never re-calls the model.
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
1. [x] **Outbox + delivery ledger + delta engine** — channel-agnostic core
   (`feed/`): `Outbox` (at-least-once + dedup via `UNIQUE(dedup_key)` + ledger,
   retry/backoff, dead-letter), `DeltaEngine` (`bites_for_activity`: PR-level
   `@opened`/`@merged` bites with stable keys), console + file channels, and the
   `feed` command. Verified end-to-end: re-running never repeats a bite.
   *(LLM-narrated + per-person rollups and commit bites are deliberately later.)*
   - [ ] **⚠️ NEXT TO BUILD — rich content: the initiative-storyline model**
     (designed 2026-06-07; see the dedicated section below). Phase 1 bites are
     mechanical ("PR opened/merged") — they describe *git*, not *the product*.
     The renderer stage becomes a genuinely helpful agent. **LLM cost is NOT a
     constraint.** Design is settled on the structure; what remains is the bite
     *anatomy* + storyline state shape, then build.
2. [x] **Delivery channels.**
   - [x] **Telegram — the PRIMARY channel.** `TelegramChannel` + `feed
     --to-telegram` + `telegram-check`. No org/admin approval; one bot token;
     same outbox guarantees. `feed_channel` (`DAILY_AGENT_FEED_CHANNEL`) selects
     the default channel when no flag is passed — set to `telegram` so bare
     `feed` and scheduled runs push to Telegram. Config: `telegram_bot_token` +
     `telegram_chat_id`. Live-verified (bot `@himanshu_daily_agent_bot`).
   - [x] **Slack — parked/optional.** `SlackChannel` + `feed --to-slack` +
     `slack-check` exist (bot token, `chat.postMessage` DM) but Slack is **not**
     the path — workspace-admin gate + the feed will carry personal content that
     must not live on a shared workspace. Kept for a possible team-facing variant.
3. [x] **Cadence engine (pacer)** — `feed/pacer.py`: a per-run delivery cap
   (`DAILY_AGENT_FEED_MAX_PER_RUN`, default 3) + quiet hours (`FEED_QUIET_START/
   END`, default 22→8, wraps midnight) wired into `feed`; `--limit` overrides.
   Run periodically (`scripts/run-feed.sh` + `install-feed-launchd.sh`, default
   hourly) so the backlog trickles instead of flooding — replaces the `--limit`
   crutch. *Still future:* morning/EOD checkpoints, event nudges, a real
   "notable enough to interrupt" bar, and Telegram `disable_notification` for
   silent (vs held) quiet-hours delivery — all need real-use feedback first.
4. Reply-to-expand — **on Telegram, via inline keyboard buttons + a long-poll
   loop** (`getUpdates`, outbound-only — no public webhook needed). Attach
   buttons to a bite (`[Dig deeper] [Mute project] [Snooze]`); a tap → a
   `callback_query` → triggers `ask` on that subject and replies in-thread.
   Slash commands (`/brief`, `/ask`, `/mute`) via `setMyCommands`. This is the
   one phase that needs a persistent daemon (launchd KeepAlive); everything up
   to here is fire-and-exit.

### Rich content — the initiative-storyline model (designed 2026-06-07)

**Purpose (decided — the lens for everything below):** this is an **awareness
feed for a busy *silent observer*, NOT a leadership decision/action dashboard.**
The user is heads-down on their own work; the feed's job is to keep them quietly
caught up so they're never blindsided by *"when did that go into the product, and
what even was it?"*. Understanding, not action. Consequences:
- **No health status, no risk/watch flags, no calls-to-action.** The user isn't
  steering these efforts.
- The renderer's central job is **translating technical git/project activity into
  plain-language product understanding** — "what this actually is, in the
  product" — not analysis. The *richness is explanation*.
- **Shipped > in-flight.** The fear is something *went into* the product unseen,
  so **merged/shipped work is the priority signal**; purely in-flight work stays
  minimal (no "coming soon" heads-up wanted).
- This makes the **Untracked lane matter** — work that shipped with no ticket is
  exactly the "I had no idea that happened" case.

**Unit & shape (decided):** the bite unit is the **initiative**, delivered as an
**evolving storyline** — each message is the next *chapter* of an ongoing effort
("here's what landed in the Item Details v3 migration since I last told you"),
not a standalone event. This needs a **stable initiative identity** so chapters
attach to the same subject over time.

**Chapter anatomy (decided):** each delivered chapter is descriptive, no judgment:
1. **Initiative name** — which thread of the product this is.
2. **What happened** — what landed, plainly (lead with what shipped/merged).
3. **What it is, in plain terms** — the technical→product translation that lets
   the user understand it without digging. *This is the core deliverable.*
4. *(thin footer)* — what shipped + timeframe; later a `[Dig deeper]` button
   (reply-to-expand). Detail beyond the footer lives *behind* the button, so the
   message stays bite-sized.

Example:
> **📦 Item Details v3 migration**
> The backend APIs behind the item-details page moved to the v3 stack this week
> (4 PRs merged, Pratik). In plain terms: the page that shows a product's details
> now runs on the new architecture — no visible change for users, but it's the
> groundwork for the Vue3 frontend rebuild coming next.
> _since Tue · 4 merged_  `[Dig deeper]`

> **Update 2026-06-30 — re-sourced from Huly to GitHub Project #86.** The design
> below is the original Huly-anchored rationale, kept as history (see
> `docs/decisions.md` for the migration). The model carries over directly onto the
> GitHub Project board: read **"Huly parent-chain" → the board's native sub-issue
> tree**, **"Huly parent _id" → the issue's `<repo>#<number>`** (e.g. `pm#56`), and
> the **deterministic anchor → `closedByPullRequestsReferences`** (each issue's
> closing PRs) instead of the ~17% `ENG-` text match — so coverage is far higher and
> the build phasing below is superseded by `sources/github_projects.py`. There are
> no QA/`Test ||` buckets on the board (QA is a field), so the resolver just anchors
> on the chain root; the ops lane keys on "Project OnCall [dates]" / incidents.

**Anchor (decided) — normalized Huly parent-chain, identity pinned to Huly ID.**
Grounded in a live investigation of the `ENG` workspace (2026-06-07):
- Only one project (`ENG`); **components unused**; **milestones are monthly
  time-buckets** ("May 2026 Projects") and not set on recent issues → neither is
  an anchor.
- **82% of recent issues hang off a parent** in a *multi-level* tree → the parent
  hierarchy is the de-facto structure and the identity spine.
- But raw parents are noisy: a mix of **real initiatives** ("Item Details v3 -
  Phase 1", "TZ-Agents Phase 2", "Document Create Vue3 Migration", "DB
  Standardization 4") and **process/ops buckets** ("Perform QA Testing" ×many,
  weekly "Project Oncall [date]", vague "Frontend Components", cryptic "TS-80").
  Naming is inconsistent; real initiatives often sit *above* a "Perform QA
  Testing" parent (so walking one level lands on a bucket — must walk to the
  right level).
- The team creates **mirror QA tasks** (`Test || <feature>`, "Perform QA
  Testing") per feature → must collapse so one feature ≠ several storylines.
- Tags are rich (4090 refs: `issue-production-bug`, `ia-tech-debt`,
  `new-feature`, the `project` tag, `ia-*` areas) → use as **notability /
  characterization signals, not identity**.
- After collapsing buckets/phases: ~**10–20 active initiatives** at a time.

So identity = the **Huly parent _id** (stable), but a cached **LLM "initiative
normalizer"** (runs once per cluster, keyed by that id) does what the raw tree
can't: pick the right level in the chain, emit a clean initiative name, **collapse
QA/Test mirror tasks**, and classify *real initiative* vs *process bucket*. The
LLM never invents identity — it normalizes around the stable id. (Hybrid of
options A+C from the brainstorm; pure LLM clustering rejected — names drift,
breaking storyline continuity.)

**Three lanes (decided):**
- **Initiatives** — the real efforts, one evolving storyline each.
- **Ops** — weekly "Project Oncall" + production incidents. A *separate, quiet*
  lane ("what broke / what's being kept alive"), not an initiative.
- **Untracked** — PRs with no `ENG-` ticket (not all branches carry one; **Sharad
  & Faizal don't use Huly at all**). Best-effort LLM-mapped into an existing
  initiative; otherwise surfaced in its own lane so their work isn't invisible.
- **QA folds into its feature's storyline** (not its own lane).

**PR→initiative mapping is LLM-PRIMARY (validated 2026-06-07).** Deterministic
ticket-linkage covers only **~17%** — the team rarely puts `ENG-<n>` in a PR's
title/body *or* branch (1/20 branches), using conventional-commit scopes instead
(`feat(comm-v3)`, `feat(inventory-v3)`). So: a deterministic resolver builds the
**catalog** of real initiatives from Huly's parent tree and anchors the confident
~17% (+ ops); an **LLM mapper** assigns the ticket-less majority onto that catalog
by reading PR scope/title/body. Constrained to the catalog (or `untracked`) — it
**never invents an initiative** (keeps identity stable). Live result on a week of
activity: **17% → 81% initiative coverage** (108/132; 4 ops, 20 genuinely
untracked), clustering onto real efforts ("Document Create Vue3 Migration" 23 PRs,
"Custom Fields V3 Migration" 13, "Communication V3" 7).

**Storyline mechanics (the render loop):**
```
per cycle:
  differ finds new raw deltas (cheap, deterministic — already built)
   → map each delta to an initiative:
        deterministic: PR → ENG-ticket → walk Huly parent chain → initiative id
        LLM: ticket-less PRs assigned onto the Huly catalog (else Untracked)
   → for each initiative WITH new deltas:
        agent reads: [prior story-state] + [new deltas]
                   + [fresh context: PR bodies/diffs, the task + parent, linked
                      Outline docs, recent history]
        agent writes: (a) the next chapter (the bite)
                      (b) an updated story-state to remember
```
The **per-initiative story-state** is the new memory that makes it a storyline:
a running plain-language summary of the effort + what's already been narrated (so
the next chapter only adds what's new). No health/status — the feed doesn't judge.
New table `initiatives(huly_parent_id PK, name, lane, story_state,
last_narrated_at, …)`. The differ + outbox (built) are unchanged; this is the
`bites`/renderer stage. PR→ticket linkage is partial, hence the LLM orphan fallback.

**Still open (delivery-layer, deliberately deferred):** notability *for pushing* a
chapter (vs silently updating state), pacing (bites/day, spacing, quiet hours),
edit-in-place vs new messages. All wait until after rich content.

**Build phasing (rich content):**
1. [x] Extend the Huly bridge to surface the **parent chain + tags** per issue.
2. [x] **Initiative resolver** (`feed/initiative.py`) — deterministic chain walk,
   bucket-skip, lane routing, id-pinned identity.
2b. [x] **Catalog + LLM mapper** (`feed/catalog.py`, `agents/initiative_mapper.py`,
   `feed/mapping.py`) — derive the catalog from Huly, anchor ticketed PRs, LLM
   maps the rest onto the catalog. Validated 81% coverage.
3. [x] **Story-state store + rich renderer** (`feed/initiatives_store.py`,
   `agents/chapter_writer.py`, `feed/storyteller.py`) — per-initiative story-state
   + an agent that writes each chapter (plain-language: what shipped + what it is)
   and an updated state. `feed-preview` CLI dry-runs chapters to the console.
4. [x] **Wired into `feed`** (`storyteller.chapters_to_bites`, `cli.feed`) — the
   feed now delivers initiative **chapters**, not PR events: maps PRs → initiatives,
   narrates only PRs *new since each initiative's last chapter*, enqueues one bite
   per initiative (stable `chapter:<key>:<hash>` dedup), and advances story-state.
   Falls back to mechanical bites if Huly is unconfigured. Live-verified delivering
   chapters to Telegram.
   - *Known tradeoffs to revisit:* story-state advances at build time (not on
     delivery) — a dead-lettered chapter's PRs wouldn't be re-narrated (rare,
     outbox is at-least-once). And with **no pacing yet**, a bare `feed` drains all
     pending chapters at once — use `--limit N` until the cadence engine lands.

**Tuned on first use (2026-06-07):** user found chapters too long and the
Untracked lane unreadable. Fixed: chapter prompt hard-capped to ~2 sentences/
~45 words; the **Untracked lane now renders as terse itemized bullets** (one
plain-language line per shipped change) instead of forced prose — `render_one`
routes by lane (narrative for initiative/ops, itemized for untracked). Keep
observing during real use for further length/format feedback.

**Resolved open questions (from the original brainstorm):**
- Bite model → **per-initiative evolving storyline** (not per-project/per-person).
- Channel → **Telegram, primary & only** (see "Decisions made").
- Initiative anchor → **normalized Huly parent-chain, id-pinned** (above).
- *Still open:* exact "notable enough to interrupt" bar, pacing specifics
  (bites/day, spacing, quiet hours, weekends), edit-in-place vs new messages —
  all **delivery-layer**, deferred until after rich content.

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

## Long-term / needs research

- **Personal insight feed (from Claude Code sessions) — DESIGNED** (#46; decision
      logged 2026-06-30). A *distinct* feed from the org-activity one: a personal
      learning / recall stream (*what did I learn while building?*), not what-shipped.
      **Reuses** the existing outbox + multi-stream channels (#38–41) + inbound
      listener (#49); adds a capture → extract → rank → resurface front-end.
      - **Capture:** `insights collect` mines `~/.claude/projects/<proj>/*.jsonl` since
        a watermark — a **marker lane** (in-chat `insight:`, verbatim) + an **LLM
        extraction lane** (ranked candidates, tight rubric).
      - **Dedup/rank/type:** exact-key dedup on a canonical key; ranked; each insight
        typed (repo / technique / gotcha / architecture) → routes to its own channel.
      - **Resurface (phased):** Phase 1 trickles new insights to **per-type Telegram
        channels** with an in-channel **`stop`** to mute a type; Phase 2 adds
        spaced-repetition + a retention signal.
      - **Build:** Phase 1 (ready) → #60 capture substrate → #61 LLM extraction ·
        #62 resurface to per-type channels → #63 in-channel `stop`. Phase 2 (deferred)
        → #64 spaced-repetition. Full rationale: `docs/decisions.md`.

- [ ] **Voice-note delivery.** Once feed *content* is genuinely good, deliver
      chapters as **voice notes** (not just text). Gated on content quality on
      purpose — a voice note amplifies the writing, so a weak chapter sounds
      worse spoken; do this only after the content has earned it.
      - Telegram fits well: `sendVoice` (voice-note UX) or `sendAudio` (longer
        clips) as another method on the existing `TelegramChannel`. Current Bot
        API docs say `sendVoice` can accept OGG/Opus, MP3, or M4A up to 50 MB;
        verify actual client rendering live before building.
      - xAI TTS is the current candidate from research (#47): simple REST call,
        MP3 output works with the likely Telegram path, and cost is listed at
        $15 / 1M characters. Live quality testing is still blocked by xAI account
        credits / spend limit, so it is not selected yet.
      - Open research questions: voice quality / latency once credits are
        available; per-chapter voice notes vs one short daily audio digest; you
        can't *skim* audio, so prefer testing a spoken headline + text detail
        hybrid; keep clips short (aligns with the bite-sized principle). Capture
        final findings here before building.

## Smaller niceties

- [ ] One-shot `brief` that runs `collect` + `summary` together.
- [ ] Rank projects in the digest by activity volume.
- [ ] Per-command model selection (e.g. a cheaper model for `summary`, a stronger
      one for `ask`) to manage cost.
