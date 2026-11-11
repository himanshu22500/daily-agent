# Decisions

## 2026-11-12 — Illustrated covers drawn by subagents replace the gradient covers

The generated gradient covers read as generic (maintainer: "awful"). Each
insight can now have an SVG illustration, a visual metaphor for its specific
idea (issue #87). The first 301 were drawn by 12 parallel Fable 5.2 subagents
from a shared art brief; how new insights get covers (the generation pipeline)
is deliberately left for later.

- **Files, not database rows.** One `<slug>.svg` per insight in a gitignored
  `covers/` directory. They are derived from private insight text, so they stay
  off GitHub. Missing covers fall back to the gradient.
- **Model-drawn files are untrusted.** `insights covers-import` accepts only a
  static SVG subset (allow-listed elements, fixed viewBox, no scripts, external
  references, images, foreignObject, event handlers, DTDs; 0.6-12 KB). They are
  served with `default-src 'none'` and shown via `<img>`, where SVG scripts
  never run.
- **No text in the art.** The headline is real text below the cover, and an
  illustrated cover keeps only the project badge overlay.

## 2026-11-12 — Discover-style cards and an article reader; model-written headlines

The web feed now looks and reads like Google Discover (issue #82, maintainer's
screenshots): a cover, headline, teaser, and source row per card, and tapping a
card opens an article-style reader.

- **Headlines are written in batches by Claude subagents, not by Gemini**
  (maintainer's call). `insights titles-export` lists insights without a
  headline. Subagents write one per insight from its text, and
  `insights titles-import` validates and stores them, keeping existing headlines
  unless told to overwrite. No model call is made for titles at capture time, so
  new insights show their first sentence until the next batch. Gemini headlines
  were tried first and dropped: the model was returning 503s, and a slow request
  could stall a flush. The first 301 were titled by 6 parallel subagents.
- **Generated covers, not images** (maintainer's call). A deterministic gradient
  per insight, with the hue family by type, the topic (first tag) in large type,
  and the project as a badge. Nothing is fetched and nothing leaves the Mac.
- **Source = project**, recovered from Claude Code's transcript directory name.
  That name is lossy (`/` and `-` both become `-`), so it is resolved by walking
  the filesystem, falling back to the last two name parts.
- **The reader is an overlay with history**, so ✕ or Back returns to the exact
  feed position. A full navigation would rebuild a new For you edition. `/i/<key>`
  also works as a standalone page. Opening an insight counts as a view.
- Static assets carry a content-hash query (`?v=`) so phones never mix a new page
  with a stale cached script.

## 2026-11-12 — "For you": a fresh, non-repeating feed every open, with spaced revisits

A strictly newest-first feed shows the same top posts on every open, which
quickly makes it boring (issue #80). The web feed's default tab is now **For
you**. Each open builds a new *edition*: unseen insights from the last 3 days
first, then the unseen backlog weighted-randomly by score. Seen insights are
mixed in about every 4th slot once their spaced-repetition gap has elapsed: 1d,
3d, 1w, 3w, then 2mo; saved insights use 1d, 2d, 4d, 10d, 1mo; hidden never.
The chronological view remains as **Latest**.

- **No short-term repeats.** Seen insights that aren't due are excluded
  outright, so an edition ends with "caught up" instead of padding with repeats.
- **Seen means on screen.** The page reports cards that were about 60% visible
  (or filled half the screen) for about 1s. A view counts once per edition. The
  #77 rule (rendered = seen) also counted cards that were only prefetched; those
  marks were reset once when the view-tracking columns were added (maintainer's
  call).
- **Editions are frozen.** The order is stored (`web_feed_sessions`, pruned after
  3 days), so infinite scroll stays stable while views and saves change state.
- **Diversity is a hard constraint where feasible.** There are never three of
  one type in a row, and same-session insights are not adjacent. Each slot takes
  the earliest fitting item from the whole queue; a short look-ahead let skipped
  gotchas (about half the corpus) pile up and force runs.
- This is the web-feed half of #64. Telegram's pacing is unchanged.

## 2026-11-11 — A local web feed shows every insight immediately; Telegram stays paced

Insights are read on a scrollable web page (`daily-agent web`, issue #77) as well
as in Telegram. The page reads the `insights` table directly, so an insight is
visible as soon as it is stored. Telegram keeps draining the outbox at its
existing pace, and its cadence will be revisited separately.

- **Ordered by publish time, not transcript time.** New `insights.stored_at`
  records when an insight entered the store; older rows fall back to
  `captured_at`. Catch-up flushes often store lessons from weeks-old sessions.
  Sorting by transcript time buried those; sorting by publish time puts them at
  the top.
- **Reader state is separate.** Seen, saved and hidden live in `web_feed_state`,
  not `insights.status`, which belongs to the delivery pipeline.
- **Local and private by default.** Server-rendered Starlette pages with a little
  vanilla JS and no frontend build. Binds to `127.0.0.1` with no login. Hosting off
  this machine was deferred because insights include internal code details.
- **Phone access goes through `tailscale serve` (tailnet-only), not a wider bind.**
  The server stays on loopback and Tailscale relays it to the signed-in devices.
  The tailnet is the access boundary. Binding to the Tailscale IP breaks loopback
  access and fails while Tailscale is down; binding to `0.0.0.0` would expose the
  feed to the LAN.
- **Starlette, not FastAPI.** Current FastAPI pulls an OpenTelemetry upgrade that
  forces major bumps to logfire and mistralai. Starlette was already installed
  and covers everything this page needs.

## 2026-09-21 — Focus exclusively on personal coding insights

The repository now has one product purpose: capture durable lessons from local
coding transcripts and resurface them later. The previous product areas were
removed, along with their commands, adapters, agents, tests, configuration,
scheduled services, and app-created Telegram channels.

The retained pipeline is transcript capture, explicit markers, Gemini extraction,
SQLite storage, durable queuing, pacing, and console/file/Telegram delivery.

## 2026-09-21 — Gemini is the default model provider

The default is `google:gemini-3.8-flash`, authenticated with `GEMINI_API_KEY`.
Model calls retry transient HTTP and transport failures with bounded exponential
backoff. Permanent failures such as invalid credentials fail immediately.

## 2026-06-30 — Explicit and inferred capture are separate lanes

Text following an `insight:` marker is captured verbatim without a model. A
structured extractor independently finds durable unmarked lessons. Both lanes use
stable semantic keys so SQLite can enforce deduplication.

## 2026-06-30 — Resurfacing uses the durable outbox

New insights become outbox items. Successful delivery records a permanent ledger
entry; failures are retried with backoff and eventually become dead letters.
Quiet hours and a per-run allowance keep delivery paced.

## 2026-06-30 — Telegram channels are owned by the application

When Telegram delivery is requested, the application provisions private channels
by insight type and records their IDs locally. It may reap only channels in that
registry. Unrelated Telegram channels are outside its authority.

## Collaboration

Implementation starts from a `ready` GitHub Issue, uses one branch and feature per
PR, and includes offline tests. Changes involving real model output, Telegram, or
the live database carry `needs-local-verification`. Only the maintainer merges.
