# Decision log

Durable decisions, in the repo so every agent (which starts with only the repo —
no shared memory) inherits them. Newest first. Keep entries short; link to the
ROADMAP section or PR for detail.

## 2026-07-09 — Voice-note delivery research: xAI TTS candidate (issue #47)
xAI's Text-to-Speech API is a plausible candidate for feed chapter voice notes,
but it is **not chosen yet** because subjective voice quality and live Telegram
rendering still need to be evaluated.

Research findings so far:
- **Integration shape.** `POST https://api.x.ai/v1/tts` returns raw audio bytes.
  Use `voice_id`, `language`, and structured `output_format`; default output is
  MP3 at 24 kHz / 128 kbps. The per-request text cap is 15,000 characters, far
  above the current ~45-word feed chapter target.
- **Formats.** xAI supports MP3/WAV/PCM/telephony codecs, not OGG/Opus directly.
  Telegram `sendVoice` currently accepts OGG/Opus, MP3, or M4A up to 50 MB, so
  the first implementation path can try xAI MP3 directly before adding an ffmpeg
  conversion dependency. Live Telegram rendering still needs to be verified.
- **Live probe.** After adding account credits, a local probe of `rigel`, `sal`,
  and `carina` succeeded for a 200-character feed-like sample. The API returned
  `audio/mpeg` MP3 files at 24 kHz / 128 kbps, with generation latency around
  4.7-5.6 seconds and durations around 11.6-13.5 seconds depending on voice.
- **Telegram delivery.** Uploading those MP3s via Telegram `sendVoice` succeeded
  and Telegram returned `voice` payloads (`mime_type=audio/mpeg`, durations
  11-13s). This validates the no-transcoding MP3 path at the Bot API level;
  maintainer still needs to confirm the client UX and voice preference.
- **Cost.** xAI lists Text to Speech at $15.00 / 1M characters. At the current
  short-chapter length, cost is not the limiting factor: a 200-character sample
  costs about $0.003 per voice, and a 250-450 character bite should be roughly
  $0.004-$0.007.
- **UX hypothesis.** Prefer a hybrid default for any future build: keep text as
  the source of truth and send a short spoken headline/summary, not necessarily
  a full narration. Audio is not skimmable, so the clip should stay short and the
  text chapter should remain available.

No production code should be added until (1) feed chapter quality has earned the
extra delivery surface, (2) the maintainer has listened to candidate voices and
chosen a default, and (3) the maintainer confirms the Telegram client rendering
is acceptable.

## 2026-06-30 — Personal insight feed: design (issue #46)
A second, **distinct** feed — a personal **learning/recall stream** from Claude Code
pairing sessions (#46), separate from the org-activity feed (*what shipped?* vs *what
did I learn while building?*). It **reuses the delivery half** (outbox + multi-stream
channels + pacer + inbound listener) and adds a capture → extract → rank → resurface
front-end. Decisions from the design session:

- **Capture (local-only, batch).** An `insights collect` CLI mines
  `~/.claude/projects/<proj>/*.jsonl` since a watermark (mirrors `collect`). **Two
  lanes** into one `insights` store: a **marker lane** (a configurable in-chat marker,
  default `insight:`, kept verbatim, top rank) and an **LLM extraction lane** (a
  per-session pass proposing ranked candidates under a tight rubric — durable
  repo/architecture facts, gotchas, non-obvious techniques; **not** transient
  debugging/chatter).
- **Signal / dedup / rank.** Each insight gets a concise **canonical key**; dedup is
  **exact-key** on it (honors the "exact-key, not similarity" architecture), so the same
  gotcha across sessions collapses. Rank = durability × non-obviousness × reusability.
  Each insight carries a **type** (repo-specific / technique / gotcha / architecture —
  tunable) that drives channel routing.
- **Resurface (phased).** **Phase 1:** new insights trickle via the **existing outbox**
  to **per-type Telegram channels** (one channel per type, via the multi-stream registry
  #38–41) on a **quiet pacer** independent of the activity feed; the user can **reply
  `stop` in a channel to mute that type** (extends the #49 inbound listener — control
  lives where the messages arrive). **Phase 2 (deferred):** spaced-repetition
  resurfacing (re-show on 1d/3d/1w/1mo) + a retention signal, once capture quality is
  proven in real use.
- **Why per-type channels + in-channel stop (maintainer):** each insight type stays
  isolated and individually mutable from its own channel, so a noisy type can be silenced
  without touching the rest — the use case multi-stream (#38–41) was built for.

Build: Phase 1 = **#60** (capture substrate) → **#61** (LLM extraction) · **#62**
(resurface to per-type channels) → **#63** (in-channel `stop`); Phase 2 = **#64**
(blocked). See `ROADMAP.md` → "Personal insight feed".

## 2026-06-30 — PM source migrated from Huly to GitHub Project #86
The org stopped using Huly; project management now lives in **GitHub Project #86**
(`fcbtech`, "Engineering"). The feed's initiative model is re-sourced onto it and
**all Huly code is removed** (client, Node bridge, `tasks`/`task` commands, config).
**Supersedes the two Huly-anchored entries below** (2026-06-07, 2026-06-06).

The board fits better than Huly did:
- **Native sub-issues** form the parent chain; the **root issue is the initiative**.
  There are no "Perform QA Testing"/"Test ||" buckets to skip (QA is a project
  *field* now), so the resolver just anchors on the chain root.
- **`closedByPullRequestsReferences`** links each issue to the PRs that close it.
  Inverting that graph gives a **deterministic** PR→initiative map — far better than
  Huly's ~17% `ENG-` text match — with the LLM mapper handling only the unlinked tail.

Read over the GraphQL API (`sources/github_projects.py`), which needs the
**`read:project`** token scope (`DAILY_AGENT_GITHUB_PROJECT_TOKEN` if the main token
lacks it). Initiative identity is now `<repo>#<number>` (e.g. `pm#56`); the ops lane
keys on "Project OnCall [dates]" / incidents (NOT a bare "oncall" — that's a real
product, "CX - Oncall Helper"). `brief`/`daily` are PR-only until a project-backed
task source lands (follow-up), along with GitHub-Issues `tasks`/`task` commands. The
downstream chapter/story-state machinery is unchanged.

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

## 2026-06-09 — Inbound feed follow-ups arrive as Telegram `channel_post`
The feed gains an inbound path: reply to any bite to ask a grounded follow-up
(issue #49). A live `getUpdates` test (2026-06-09) settled the architecture:
because the maintainer is a **channel admin**, an in-channel reply arrives as a
**`channel_post`** update via plain `getUpdates` — **no linked discussion group,
no DM, no Telethon listener needed** (the bot's group-privacy flag gates *groups*,
not channels). The one wrinkle: in a broadcast channel both the bot's posts and
the maintainer's replies are attributed to the channel (`sender_chat`), so an
inbound post can't be told from our own by sender. Disambiguation: **persist every
`message_id` the bot sends**, keyed to its bite — an inbound reply whose id is in
that set is our own post (ignore); one that replies to a stored bite is a human
follow-up to answer, grounded on that bite's initiative. Phase 1 (this PR) adds
the `sent_messages(chat_id, message_id → dedup_key, subject)` store; the
long-poll listener + grounded reply land in later PRs.

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
> **Superseded 2026-06-30** (Huly → GitHub Project #86): the catalog is now built
> from the board's sub-issue tree, and the deterministic anchor is the native
> closing-PR link (`closedByPullRequestsReferences`), not the `ENG-` text match. The
> LLM-onto-catalog fallback for unlinked PRs is unchanged.

Only ~17% of PRs cite an `ENG-` ticket (not in title/body or branch). So: a
deterministic resolver builds the initiative **catalog** from Huly's parent tree
and anchors the ticketed minority; an **LLM maps the rest onto that catalog** by
PR scope/title/body. It is constrained to the catalog or `untracked` — it never
invents an initiative (keeps storyline identity stable). Validated ~81% coverage.

## 2026-06-06 — Initiative model: evolving storyline, Huly-anchored
> **Superseded 2026-06-30** (Huly → GitHub Project #86): identity is now the GitHub
> issue `<repo>#<number>`; the parent chain comes from native sub-issues; there are
> no QA/`Test ||` mirror tasks to collapse. The evolving-storyline model and the
> three lanes (Initiatives / Ops / Untracked) carry over unchanged.

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
