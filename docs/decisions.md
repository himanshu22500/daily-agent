# Decisions

## 2026-11-14 — Merging to main deploys; each machine pulls

The maintainer wanted a merge to `main` to deploy (#128). The repo is public
and the server is reachable only over the tailnet, so the deploy is **pull,
not push**. Neither machine is reachable from GitHub, and GitHub holds no SSH
key, tailnet key or runner on our machines. A self-hosted runner on a public
repo would let any PR run code there.

- **The trigger.** Every 2 minutes (a systemd timer on the server, launchd on
  the Mac), each machine fetches `main`. A new commit is deployed only once
  both CI checks have passed *for that commit*. That is read from GitHub's
  public check-runs API without a token. A failed commit is remembered and
  never deployed.
- **Safety.**
  - Server first, with a `pg_dump` before every deploy (14 kept, on top of
    Lightsail's daily snapshots).
  - The Mac waits for the server's `/version` to show the commit, so the
    backup and any schema change happen once, on the server, before either
    machine runs the new code.
  - `update.sh` is pinned to the commit (`DEPLOY_SHA`), never a newer tip.
  - Health = `/version` reports the commit. Otherwise roll back to the
    previous commit and remember the bad one.
  - A checkout with local edits is left alone.
  - A file-based pause switch.
  - Telegram reports each deploy, failure and rollback.
- **Limits.** Rolling back the code doesn't undo a schema change; the dump is
  there for that. So PRs keep schema changes additive (AGENTS.md).
- **CI hardening, done at the same time.**
  - `permissions: contents: read`.
  - Actions pinned to commit SHAs.
  - `tests (postgres)` is required on `main` too.
  - Workflows from outside contributors' PRs wait for approval.

## 2026-11-14 — Rate items 👍 / 👎, and the extractor learns from it

The maintainer found some generated items useless. They compared four
clickable mockups:
- **A:** swipe
- **B:** ✕ plus reasons
- **C:** rate it
- **D:** a tidy-up mode

They chose **C** (#126).

- **Where it shows.** A 👎 on every card, and "Was this worth reading?
  👍 / 👎" at the end of every item, where the judgment is fresh.
- **👎 is a hide plus a rating.** It reuses `web_feed_state.dismissed_at`, so
  the item leaves For you, Latest and Interview and lands in Hidden. Undo
  clears both. A short sheet asks why. It's optional and multi-select, with
  four fixed reasons plus "Fewer from <source>" and "Fewer <type>". 👍 on a
  hidden item brings it back.
- **A new table, no migration.** One row per item in `feed_ratings` (latest
  rating wins), created with `create_all`. Existing tables are untouched.
- **Teaching the extractor.** `extraction_feedback` turns the latest 20 👎 and
  10 👍 *coding insights* into a note: headline, type, project and reasons,
  plus the projects and types the reader wants fewer of. The note goes into
  the extraction prompt, together with the session's project, so "fewer from
  daily-agent" can apply. The system prompt explains how to use it.
  - Feedback is read once per run, and a failure to read it never blocks
    extraction.
  - Ratings of videos, posts and prep cards don't steer the extractor, which
    doesn't write those.

## 2026-11-14 — A quiet YouTube channel brings one older video a day

The maintainer asked that a followed channel with no new videos go back one
video a day: the 6th, then the 7th, and so on (#124).

- **Per source, per local day.** After each check (every 3 hours), a source
  that has queued nothing today, whether new, initial, or older, queues the
  newest video it hasn't taken. A day with a new upload brings no older one.
  "Today" starts at local midnight on the Mac, so the pace stays at one a day.
  A rolling 24 hours would drift later at every 3-hour check.
- **Where older videos come from.** The RSS feed lists only about 15 videos.
  Beyond that, the channel's public long-form uploads list (`UULF…`, newest
  first, no Shorts) is read 100 at a time through YouTube's own continuation
  requests, up to 10 pages. A playlist source walks the playlist. A video
  older than the feed is read from its watch page, for its exact upload date
  and description. There is still no API key. If YouTube changes these pages,
  going back stops and reports the error. New uploads are unaffected.
- **No schema change.** `videos.found_at` now means "found, or taken from the
  backlog", which is all the "queued today?" test needs. A private or removed
  video stays `skipped` with an `unavailable:` error, so it's never tried
  again. At most 3 are tried per check.
- **Mac only**, like all story writing. `DAILY_AGENT_VIDEO_BACKFILL=false`
  turns it off.

## 2026-11-14 — Every model task runs on Opus 5.5

The maintainer asked for Opus 5.5 for every task. All seven settings now
default to `claude-code:claude-opus-5-5`: extraction, headlines, covers,
YouTube and Instagram stories, prep explainers, and Ask answers. The full
model name is pinned rather than the `opus` alias, so a new Opus release can't
change the model silently. Opus uses the Claude plan faster than Sonnet did.
Fable stays unused.

## 2026-11-13 — The Ask box is a chat (option C)

The first Ask box (#119) looked plain on the phone. The maintainer compared
three clickable mockups:
- **A:** starter chips plus a sticky "Ask Claude" pill
- **B:** a floating button that opens a bottom sheet
- **C:** inline, just polished

They chose **C** (#121). The box stays at the end of the item, with no chips and
no floating button:
- The reader's question is a right-aligned blue bubble.
- Claude's answer is a soft card marked "✦ Claude". Open questions get the same
  card with a shimmering placeholder.
- The composer is a pill that grows as you type, with a round send button.

## 2026-11-13 — Ask Claude about an item; Telegram says when it's answered

The maintainer asked for a way to question any article or interview card from
the feed, with a Telegram message and a feed link once the answer is ready
(#119).

- **Asynchronous by necessity.** The phone reads the feed server, which has no
  `claude` (#111, #114). Asking only stores the question in `feed_questions`,
  in the shared Postgres. The Mac's `daily-agent web` polls for it every 15s
  (gated on `claude_available()` like the other writers), answers with
  `claude -p` (Sonnet), and sends the Telegram message. Questions wait while
  the Mac sleeps. The 15s poll is a single indexed query, so it's cheap.
- **Same queue as stories.** Questions reuse `StoryQueue` (claim, 1h/2h/4h
  retries, give up after 4). It now has an oldest-first option, so questions
  are answered in the order asked and a follow-up sees the earlier answer.
- **Context.** Claude gets the item, the earlier Q&A on it, and, for interview
  prep, that resume point's deep-dive notes. It never invents facts about the
  reader's work. The answer uses the reader's small markdown subset, plus "• "
  lines.
- **Telegram:** a plain-text message to the bot chat (`TELEGRAM_CHAT_ID`), not
  the per-type insight channels. It carries the question, up to 1,200
  characters of the answer, and `DAILY_AGENT_FEED_URL/i/<key>#ask-<id>`. A
  failed send is retried 3 times, 5 minutes apart and growing, and dropped
  after a day.
- **Reader UX:** the box sits under the text, after a flashcard's folded answer.
  While a question is open, the page re-fetches only the thread every 10s, so
  text being typed is kept. Everything works without JS too.
- **Limits:** 1,000 characters per question, 20 waiting at once, and 50 per
  item.

## 2026-11-13 — The Interview tab is a rotating practice deck

The fixed resume-point order from #115 never changed, so the same questions
always came first. The maintainer asked for rotation (#117). They chose a
practice deck over "seen ones go last" and over "fully random":

- Every visit builds a fresh deck with the same builder and view tracking as
  For you: unseen first (weighted shuffle), and seen questions come back after
  1d, 3d, 1w, 3w, then 2mo (sooner if saved).
- A question counts as seen once its card has been on screen, the same rule as
  everywhere else.
- Point chips build a deck for just that resume point.
- The end of a deck says when the next question is due. For you's "next comes
  back" no longer counts prep, and the Interview deck's counts only prep.

## 2026-11-12 — Interview prep gets its own tab, in resume-point order

After the 269 flashcards were imported, they took over For you: 14 of the
first 20 cards. The maintainer asked for a separate section (#115) and chose:

- **Only in the Interview tab.** Flashcards and concept explainers stay out of
  For you and Latest, their type chips, the unseen count and the "new stories"
  banner. Saved, Hidden and search still include them (search now also matches
  headlines, so a question finds its card).
- **Resume-point order, not a shuffled deck.** Points follow the tracker (their
  files sort that way: `01-tranzact/...` before `02-edra-labs/...`). Within a
  point come its questions in order, then its explainers. Chips pick one point.
- Flashcard cards have no cover or time, since the question is the card.
  Read next under prep suggests only prep, and under anything else never prep.
- Concept explainers aren't written automatically on the Mac for now
  (`DAILY_AGENT_WEB_PREP_SYNC=false` in the live `.env`): the maintainer asked
  for the flashcards only. `daily-agent prep write` writes them on demand.

## 2026-11-12 — Interview prep from the resume deep dive: flashcards + explainers

The maintainer keeps interview prep for their resume in a local deep-dive
folder: one file per resume point, in a fixed section format (#108). They asked
for two things in the feed:

- **Flashcards:** the written Q&A, with the answer hidden until tapped.
- **Explainers:** one per "concept to master", written by Claude.

Their constraints were only resume points (the tracker rows under a job, not
skills or general) and no recurring daily drip.

- **A one-time CLI import** (`daily-agent prep import <folder>`), re-runnable
  after editing. There's no folder watcher. Card keys are
  `prep:<point>-q<n>`, so a re-import updates answers in place and keeps the
  reader's seen and saved state. It also removes cards whose question is gone
  and rewrites explainers whose concept text changed.
- **No answer, no card.** Questions whose answer is still `⬜ OPEN` with
  nothing written (98 of 367) get no card. "Reference design — confirm"
  drafts are dropped as unconfirmed.
- **Hidden answers.** The card teaser shows the level, never the answer. The
  reader folds the answer in a native `<details>`, so it needs no JavaScript.
- **Explainers send the whole point file to `claude -p`** (Sonnet). That's how
  they can use the reader's real numbers and incidents, and the prompt forbids
  inventing any. Measured at about $0.048 each, roughly $5 for all 111. They're
  written by the web server's background loop through the shared story queue.
- **All at once.** The cards are web-only and lead For you as fresh items for
  three days, broken up by the existing type-run limit. The maintainer chose
  this over a daily drip. The Interview and Concept type chips filter them.

## 2026-11-12 — One shared Postgres; the feed is served from an always-on server

The phone could only read the feed while the Mac was awake, because the feed
was served from the Mac through `tailscale serve` (issue #111). Decisions:

- **Hosting off the Mac is now allowed.** The maintainer said data privacy is no
  longer a blocker. This supersedes the 2026-11-11 decision to keep the feed on
  the Mac.
- **One database, read and written directly by both machines; no sync layer.**
  The stores open `DAILY_AGENT_DATABASE_URL` when it is set and the SQLite file
  otherwise. The Mac captures, extracts, writes stories and delivers to Telegram;
  the server only serves the feed and records reader state. No table needs
  merging because there is only one copy.
- **Cheapest AWS setup: one Lightsail server running both Postgres and the
  feed** (Mumbai, `micro_3_1`, 1 GB, about $7/month plus about $1 of daily
  snapshots). Managed RDS was about $26–28/month for the same job. The cost of
  self-hosting is doing our own OS updates (unattended-upgrades) and backups
  (Lightsail daily automatic snapshots).
- **Tailnet only.** Postgres and the feed are reachable only over Tailscale; the
  Lightsail firewall has no public ports open. So the feed still needs no
  login, and the database is never exposed to the internet.
- **Stories are still written on the Mac.** The server runs with
  `DAILY_AGENT_WEB_VIDEO_SYNC=false` because `claude -p` needs the Mac's signed-in
  Claude Code. Following a channel on the phone stores and queues it in the
  shared database; the Mac's feed job writes the stories within about 10 minutes
  of waking.
- **When the Mac is offline, nothing is lost.** Writes fail and the transcript
  cursors don't advance, so the next sweep re-reads them. Stale connections are
  replaced (`pool_pre_ping`).
- **Postgres databases use the `C` collation.** The stores compare and sort ISO
  timestamp strings and keys (feed cursors, watermarks). SQLite compares bytes;
  Postgres's default `en_US` collation ignores punctuation and would reorder
  them. `db copy` and the Postgres test run refuse other collations.
- **Inserts report new rows through `RETURNING`, not `rowcount`.** psycopg's
  `rowcount` is -1 for SQLAlchemy INSERTs, which made every insert look like a
  duplicate.
- **The whole offline suite also runs on Postgres** when
  `DAILY_AGENT_TEST_DATABASE_URL` is set (CI job `tests (postgres)`). Each SQLite
  path a test opens maps to its own schema, so tests didn't need rewriting; the
  four that hand-build an old SQLite file are marked `sqlite_only`.

## 2026-11-12 — Follow Instagram accounts through the official Graph API

The maintainer asked to follow Instagram accounts the same way as YouTube
channels (issue #107). Decisions:

- **Official API only, no scraping.** Business Discovery (Instagram API with
  Facebook Login) reads public posts of Business and Creator accounts through
  the maintainer's own professional account. Scrapers break Instagram's terms,
  break often, and can get the account blocked. The cost: personal and private
  accounts can't be followed.
- **A Page token, saved to `.env`.** `daily-agent instagram connect` swaps a
  short-lived Graph API Explorer token for a long-lived user token, then takes
  the linked Page's token from it. A Page token made this way doesn't expire,
  so there's no 60-day renewal. It is sent in an `Authorization` header, never
  in a URL, so it can't reach logs. The connect command never prints it.
- **Claude sees the images.** Carousels carry their content in the slides, so
  `claude -p` gets up to 4 images as base64 image blocks in one
  `--input-format stream-json` message. It still runs with no tools. A reel's
  audio isn't available; its story covers only the caption and cover frame
  (transcribing reels is a possible follow-up).
- **Images are stored.** Instagram's CDN links expire within days, so each
  post's images are downloaded once (only from Instagram/Facebook CDN hosts,
  over https, without the token, at most 8 MB each) and served from
  `/ig/<shortcode>/<n>.jpg`. Expired links on a queued post are renewed by
  re-reading the account.
- **Shared queue.** Claiming, retries and source bookkeeping moved from
  `feed/videos.py` into `feed/story_queue.py`, so both platforms use the same
  code instead of two copies.
- Stories are insights with key `ig:<shortcode>`, type `instagram`, status
  `web-only` (never Telegram), and the account as their source.

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
