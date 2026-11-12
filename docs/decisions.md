# Decisions

## 2026-11-12 — Follow a YouTube channel even while YouTube's RSS server is down

Following `@anthonywritescode` failed with "no public video feed". The link
was fine; YouTube's RSS server was answering 404 for every channel (#103).
These outages recur, often for hours a day, and YouTube has never
acknowledged them. So a feed 404 now means "try later", never "bad link".
When following, a feed that doesn't answer falls back to the channel's or
playlist's own page to confirm it exists. The source is then followed,
unchecked, and the background loop queues its latest videos once the feed
answers. We kept RSS as the only video list rather than adding a fallback
scraper of the channel page or the YouTube Data API. Outages only delay
stories by hours, which is fine for a reading feed. The Data API would need a
Google key.

## 2026-11-12 — Follow YouTube channels; Claude writes a story per video

The maintainer wanted feed stories from outside the coding sessions: paste a
YouTube channel link, and its latest videos turn into feed stories in the
background, with the video embedded in the reader (issue #99). The
maintainer's calls: one article per video; the latest 5 on follow, then every
new upload; mixed into For you and Latest with a Sources page to manage
channels; and written the same way as all other model work, through Claude
Code (#95).

- **Captions in, not the video.** `claude -p` runs with no tools, so it can't
  open a link. The app fetches captions with `youtube-transcript-api` and sends
  them as text, marked untrusted like transcripts. No YouTube API key: channels
  resolve through their public page and RSS feed. Only `youtube.com` is ever
  fetched, since pasted links are rebuilt on it. A new video without captions
  waits up to 6 hours for auto-captions, then is written from its title and
  description.
- **Stored as insights** (`video:<id>`, type `video`, status `web-only`), so
  saving, hiding, views, revisits, search, and Read next work unchanged. The
  outbox only queues `new` insights, so video stories never reach Telegram
  (Telegram pacing is unchanged). Channel and queue state live in
  `video_sources` and `videos`.
- **Written by the web server**, in a background loop. It is the one process
  that always runs under launchd, and Follow starts it at once. A conditional
  claim keeps it and a manual `videos sync` from writing the same video twice.
  A claim left by a crash expires after 15 minutes.
- **Limits in the JSON schema, not just the prompt.** Sonnet ignored "at most
  75 characters" (headlines ran to 100+). Claude Code validates
  `--json-schema` replies and has the model retry, so `maxLength` holds.
- **YouTube images are loaded from YouTube.** Cards use the video thumbnail
  (`i.ytimg.com`, no referrer), and the reader embeds `youtube-nocookie.com`.
  This is the first thing the feed loads from outside the Mac. It is limited to
  public video content the maintainer chose to follow. Insight covers still
  come from the database.
- Cost: about $0.03-0.04 per video story with Sonnet (measured on 5 videos of
  about 7 minutes each).

## 2026-11-12 — All model work runs through Claude Code in the background

The Vertex AI sign-in for the work account stopped refreshing about 9 hours
after it was created ("Reauthentication is needed"), which looks like a
Workspace re-sign-in policy; an unattended job can't run on it. Claude Code is
always installed and signed in on the Mac, and the maintainer wants every model
call to go through it (issue #95). This supersedes Gemini on Vertex as the
default; Gemini remains available through pydantic-ai model strings.

- **`claude-code:<alias>`** model strings run headless `claude -p` with JSON
  output: extraction uses `--json-schema` structured output, covers return SVG
  text. Defaults: `sonnet` for extraction, headlines, and covers. Covers
  started on `fable` (the subagent-drawn covers the maintainer liked were
  Fable's) but moved to `sonnet` the same day: Fable exhausts the plan's
  limits, so it isn't used for anything (issue #100). The scheduled sweep on
  the Mac currently runs with `--max-covers 0` (maintainer stopped image
  generation).
- **Locked down, because prompts carry transcript text:** `--tools ""` (no
  tools), `--strict-mcp-config` (no MCP), `disableAllHooks` (the `SessionEnd`
  hook would start another flush), `--no-session-persistence` (the sweep would
  otherwise read the call back as a session), a task system prompt instead of
  Claude Code's, an empty working directory, prompt on stdin, and parent-session
  variables (`CLAUDECODE`) stripped. 300 s timeout per call.
- **Headlines and covers are now written automatically** by the sweep after
  capture (up to 100 headlines and 10 covers per run), with the same briefs the
  subagents used, and still never by Gemini. `titles-generate` writes headlines
  on demand; `titles-import` and `covers-import` remain for hand-made batches.
- **Cost:** usage counts against the signed-in Claude plan; the sweep logs the
  per-run total Claude Code reports at API rates.
- Tests can't reach the real CLI: `tests/conftest.py` points
  `DAILY_AGENT_CLAUDE_BIN` at a missing file, and the backend is tested against
  a fake `claude` script.

## 2026-11-12 — A timed sweep reads every project, not just sessions that end

Insights were captured only when a Claude Code session ended: the `SessionEnd`
hook flushes the ended session's folder. Most sessions run inside Zed over ACP,
which keeps each thread's Claude process alive until Zed quits, so they rarely
end. Over 30 days 19 of 41 Zed transcripts were never read, and folders used only
from Zed were never read at all (issue #91).

- **`insights sweep` on a 30-minute launchd timer** reads every
  `<project>/<session>.jsonl` under `~/.claude/projects` that was written to
  since it was last read and has been quiet for 10 minutes. A long conversation
  is therefore read in pieces at its pauses; each piece is extracted on its own.
- **The cursor records when reading started**, so the "written since last read"
  check never skips lines appended mid-read, and unchanged files aren't opened.
- **Shared with the flush:** the same per-file cursors and lock (refreshed per
  transcript so a long sweep isn't reclaimed as stale). The hook stays as the
  fast path for terminal sessions.
- **Web now, Telegram unchanged.** The sweep publishes to the web feed and
  queues for Telegram but never delivers; Telegram's pace stays tied to the
  flush until the maintainer revisits it.
- **Failure handling:** a transcript that fails keeps its cursor and is retried
  next sweep; three failures in a row end the run (a provider outage). The
  extractor now has a 120 s request timeout and 3 attempts so an unattended run
  can't hang.

## 2026-11-12 — Gemini runs on Vertex AI, not the Gemini API key

The Gemini API key's project ran out of quota (`429 ... check your plan and
billing details`), which stopped both insight extraction and cover drawing.
Gemini now runs on Vertex AI in a billed Google Cloud project
(`google-cloud:gemini-3.8-flash`, location `global`). It is the same model
for every use case (maintainer's call: only `gemini-3.8-flash`). No code
changed; Pydantic AI's `google-cloud:` provider and environment settings
cover it.

- **Dedicated credentials.** daily-agent uses its own Application Default
  Credentials in a separate gcloud config, pointed to by
  `GOOGLE_APPLICATION_CREDENTIALS`, signed in as the account with Vertex
  access. The machine's default ADC belonged to a different account with no
  Vertex access, so reusing it failed with 403. It is left untouched for
  other tools.
- **Why Vertex over just adding billing to the API key:** IAM-scoped
  credentials instead of a long-lived key in `.env`, and paid-tier data terms
  for content that includes internal company code. The project and account
  are temporary; the maintainer plans to move to a personal project later,
  which is the same steps with a different project.

## 2026-11-12 — Illustrated covers drawn by subagents replace the gradient covers

The generated gradient covers read as generic (maintainer: "awful"). Each
insight can now have an SVG illustration, a visual metaphor for its specific
idea (issue #87). The first 301 were drawn by 12 parallel Fable 5.2 subagents
from a shared art brief; how new insights get covers (the generation pipeline)
is deliberately left for later.

- **Stored in the main database** (`insight_covers`: key, svg, source,
  updated_at), so backups include them and nothing derived from private insight
  text sits in the repo. They first lived in a gitignored folder and were moved
  in at the maintainer's request. Missing covers fall back to the gradient.
- **Model-drawn covers use the same brief.** `covers-generate` sends the
  subagents' art brief to the configured model (Gemini) for insights without a
  cover, validates each SVG, and retries once with the validator's reasons.
  It's manual and not wired into the flush; the generation pipeline is still
  undecided.
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
