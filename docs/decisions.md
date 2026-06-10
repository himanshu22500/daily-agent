# Decision log

Durable decisions, in the repo so every agent (which starts with only the repo —
no shared memory) inherits them. Newest first. Keep entries short; link to the
ROADMAP section or PR for detail.

## 2026-06-10 — DB access standardized on SQLModel (reverses "no ORM")
All five stores (`Store`, `Cache`, `Outbox`, `InitiativeStore`, `ChannelRegistry`)
now use **SQLModel** (SQLAlchemy + Pydantic) over a shared engine/session helper
(`daily_agent/db.py`) instead of hand-rolled `sqlite3` + raw SQL. This **reverses
the earlier implicit "no ORM, raw SQLite" stance** — chosen to fit the
Pydantic-heavy stack and stop hand-writing SQL. The staged-pipeline-over-SQLite
architecture is unchanged (still SQLite, still exact-key dedup, no broker/vector
DB). Constraints honored so it's behavior-preserving with **no migration system**:
on-disk schema is untouched (`create_all` is `IF NOT EXISTS`; timestamps stay ISO
strings in TEXT columns, cache `fetched_at` stays epoch REAL); the public store
API and return types are unchanged; upserts use the SQLite dialect `insert()`
(incl. the watermark's forward-only conditional update); `drain` still commits
per item. `session_scope` uses `expire_on_commit=False` so a fetched row maps to
its return type after the scope closes. Verified offline that a pre-migration DB
opens, round-trips, and keeps a byte-identical schema. Issue #51.

## 2026-06-08 — Multi-stream Telegram delivery via MTProto channels
The feed will carry several notification *types* (org-activity, insights, alerts,
…); one channel for all would be poor UX. The tool will route each type to its
own Telegram **channel** and **create/delete channels on its own**. A bot can't
create channels, so this uses an **MTProto user-client (Telethon)** under the
maintainer's account for provisioning, while the existing **bot** does the
posting. (Forum topics — bot-doable — were the recommended alternative but the
maintainer chose real channels.) Consequences: `api_id`/`api_hash` + the session
are the most sensitive creds — **gitignored, never in CI**; the MTProto code is
**local-only / `needs-local-verification`** (CI + cloud agents have no session)
and offline tests mock Telethon; the one-time phone-login is run locally by the
maintainer. Built provisioner-agnostic first (registry + ensure/reap). Issues
#38–#41.

## 2026-06-07 — Agent collaboration model
Work is tracked in **GitHub Issues** (`ready`/`in-progress`/`needs-local-verification`
labels); `ROADMAP.md` is the strategy/vision doc, not a task tracker. **CI**
(GitHub Actions) runs the offline test suite on every PR; **`main` is protected**
(PR + green CI, no direct pushes). Cloud/remote agents open PRs only; the
maintainer live-verifies and merges on the main computer. Contract: `AGENTS.md`.

## 2026-06-07 — Feed cadence: a simple pacer
Delivery is paced by a per-run cap (`FEED_MAX_PER_RUN`, default 3) + quiet hours
(default 22:00–08:00, local), run periodically via launchd so the backlog
trickles. Richer cadence (checkpoints, event nudges, a "notable enough to
interrupt" bar, silent quiet-hours delivery) is deferred until there's real-use
feedback on the right rhythm.

## 2026-06-07 — Feed content is an awareness feed (not a dashboard)
The reader is a busy **silent observer**, not a decision-maker. Chapters
**describe**, never judge: no health/status, no risk/watch flags, no
calls-to-action. The renderer's core job is **translating technical activity into
plain-language product understanding**; **shipped > in-flight**. Chapters are kept
short (~2 sentences); the Untracked lane is itemized bullets, not prose. Feed
content is the heart of the project and will keep iterating with real use.

## 2026-06-07 — PR→initiative mapping is LLM-primary onto a Huly catalog
Only ~17% of PRs cite an `ENG-` ticket (not in title/body or branch). So: a
deterministic resolver builds the initiative **catalog** from Huly's parent tree
and anchors the ticketed minority; an **LLM maps the rest onto that catalog** by
PR scope/title/body. It is constrained to the catalog or `untracked` — it never
invents an initiative (keeps storyline identity stable). Validated ~81% coverage.

## 2026-06-06 — Initiative model: evolving storyline, Huly-anchored
The feed's unit is the **initiative**, delivered as an **evolving storyline**
(chapters that only add what's new). Identity is anchored to the **Huly parent
issue** (stable forever). A cached LLM normalizer picks the right level in the
parent chain, names it, collapses QA/`Test ||` mirror tasks, and classifies
initiative-vs-process-bucket. Three lanes: Initiatives, Ops (oncall/incidents),
Untracked. (Huly structure: only the `ENG` project; components unused; milestones
are monthly buckets; the parent tree is the spine.)

## 2026-06-06 — Delivery channel: Telegram, primary and only
Telegram is the primary and intended-only feed channel (one bot token, no admin
approval, supports inline buttons / silent sends / edits). **Do not route the feed
through Slack or any shared workspace** — it will carry personal/leadership
content that must stay private. `SlackChannel` remains only as a parked,
possible team-facing variant.

## (ongoing) — Architecture: a staged pipeline over SQLite
The feed is a pipeline of durable, idempotent stages over SQLite (raw activity →
differ → initiative mapping → chapter render → outbox → channel), not one job.
The outbox guarantees at-least-once delivery + dedup. No message broker, no vector
DB — exact-key dedup, not similarity. See `ROADMAP.md` for the full rationale.
