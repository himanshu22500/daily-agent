# daily-agent

`daily-agent` is a private memory loop for lessons found while coding. It reads
local Claude Code JSONL transcripts, captures explicit `insight:` notes, has
Claude (through the local Claude Code CLI) extract other durable lessons, and
stores them in SQLite. Every stored
insight shows up immediately on a local, scrollable **web feed**. Telegram
separately gets a small paced batch.

The useful output is a concise fact, gotcha, architecture constraint, or reusable
technique. Transcript chatter, progress updates, and temporary debugging steps are
filtered out. Exact semantic keys prevent the same lesson from being queued twice.

## Setup

```bash
uv sync
cp .env.example .env
uv run pytest
```

### Models: Claude Code in the background

All model work runs through the Claude Code CLI already signed in on the Mac
(`claude -p`), so there are no API keys or cloud credentials to expire:

| Work | Setting | Default |
|---|---|---|
| Insight extraction | `DAILY_AGENT_MODEL` | `claude-code:sonnet` |
| Feed headlines | `DAILY_AGENT_HEADLINE_MODEL` | `claude-code:sonnet` |
| Cover illustrations | `DAILY_AGENT_COVER_MODEL` | `claude-code:sonnet` |

`claude-code:<alias>` takes any model `claude --model` accepts (`haiku`,
`sonnet`, `opus`, or a full model name). Fable isn't used by default: it uses up the
plan's limits fastest. Every call is locked down
because prompts contain transcript text: no tools, no MCP servers, hooks off
(so it can't trigger the `SessionEnd` flush again), no saved session (so the
sweep never reads it back), Claude Code's agent prompt replaced by the task's,
and run from an empty directory. `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
are not passed on, so a key in `.env` can't override Claude Code's own sign-in.
Usage counts against the signed-in Claude plan. Set `DAILY_AGENT_CLAUDE_BIN` if `claude` isn't on `PATH`.

Gemini still works for extraction and covers with a pydantic-ai model string
(`google:gemini-3.8-flash` with `GEMINI_API_KEY`, or `google-cloud:...` on
Vertex AI with `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=global` and
`GOOGLE_APPLICATION_CREDENTIALS`). Headlines are Claude-only.

By default, transcripts are read from the Claude Code project directory matching
this checkout. Set `DAILY_AGENT_INSIGHTS_TRANSCRIPTS_DIR` when the transcripts live
elsewhere.

## Use

```bash
# Capture marker notes and model-extracted lessons.
uv run daily-agent insights collect

# Capture marker notes without calling an LLM.
uv run daily-agent insights collect --no-extract

# Queue new insights and resurface a paced batch in the terminal.
uv run daily-agent insights feed

# Deliver to a file or to private per-type Telegram channels.
uv run daily-agent insights feed --to-file digests/insights.md
uv run daily-agent insights feed --to-telegram

# Collect and deliver after transcript files settle.
uv run daily-agent insights flush --to-telegram

# Read every project's transcripts, ended sessions or not (see Automation).
uv run daily-agent insights sweep --dry-run   # what it would read, no model calls
uv run daily-agent insights sweep

# Inspect captured and queued counts.
uv run daily-agent insights status

# Hand off insights that need a web-feed headline, then store what was written.
uv run daily-agent insights titles-export --out untitled.json
uv run daily-agent insights titles-import titles.json   # {"insight:key": "Headline"}
```

Add an explicit note during a coding session like this:

```text
insight: SQLite upserts should enforce the deduplication key in the database.
```

The extractor can also identify durable lessons without a marker. It assigns one
of `repo`, `technique`, `gotcha`, `architecture`, or `general`, plus a stable
key and tags. Telegram delivery creates one private channel per type on demand.

## Web feed

Read every insight like a news feed, laid out like Google Discover. Each card
has a generated cover (colored by type, showing the main topic and the project
it came from), a headline, a two-line teaser, and a source row: project, time,
♡ save, share, and ⋮ (hide, copy). Tap a card to open it like an article. It
shows the full text, the project and branch it was learned on, and the date,
with bookmark, text size (Aa), and share. ✕ or Back returns to the same spot in
the feed. An insight appears the moment it is stored. It doesn't wait for the
Telegram pacer, and reading it doesn't change Telegram delivery.

Covers are illustrations when an insight has one: an SVG per insight, stored
in the main database (`insight_covers`). They are drawn outside capture, either
by illustrator subagents and brought in with
`uv run daily-agent insights covers-import <dir> --label subagent:fable`, or by
the cover model with `uv run daily-agent insights covers-generate` (only
insights without a cover; `--model` to choose). The sweep draws up to 10 missing
covers per run (`--max-covers`). Every SVG must pass a strict validator
first: static and self-contained, fixed 800×400 viewBox, no scripts, external
references, embedded images, or event handlers. Insights without an
illustration keep the generated gradient cover.

Headlines aren't written at capture time. The sweep writes them in batches for
untitled insights (up to 100 per run, `--max-titles`) with the headline model,
and `insights titles-generate` does the same on demand. By hand,
`insights titles-export` lists the insights without one and
`insights titles-import` cleans and stores a written batch: unknown keys are
reported, and existing headlines are kept unless you pass `--overwrite`. Until
an insight has a headline, its first sentence is shown.
Sharing uses the phone's share sheet over HTTPS; on the plain-`http` tailnet URL
it copies the text to the clipboard instead.

```bash
uv run daily-agent web            # http://127.0.0.1:8765/
scripts/install-web-launchd.sh    # keep it running in the background (macOS)
```

**For you** (the default) is different every time you open it. It leads with
unseen insights from the last few days, then the rest of your unseen backlog in
a fresh random order. Higher-scored insights tend to come earlier, and there are
never more than two of one type in a row. Insights you've already seen stay out
until they're due for a revisit: after 1 day, 3 days, 1 week, 3 weeks, then
every 2 months. Saved ones come back sooner and hidden ones never do. Revisits
are marked "↻ Revisit". When nothing is left, it says you're caught up instead
of repeating itself.

An insight counts as **seen** only after it has been on screen for about a
second; loading it isn't enough. A blue dot marks insights you haven't seen.

**Latest** is the chronological view, ordered by when an insight was stored. A
flush that catches up on older sessions still lands its insights at the top,
marked "from <date>". Search and tag links open in Latest. If the page is left
open, a pill appears when new insights arrive.

It binds to `127.0.0.1` and has **no login**, so don't expose it on a LAN or the
internet.

### Video stories (YouTube)

Follow a YouTube channel and its videos become stories in the feed. Open
**Sources** (top right of the feed), paste a channel link (`youtube.com/@name`,
a `/channel/` link, a playlist, or any video from the channel), and tap Follow.
The latest 5 videos are queued at once. After that the channel is checked every
3 hours (`DAILY_AGENT_VIDEO_CHECK_HOURS`) and each new upload is queued. Shorts
are skipped.

While `daily-agent web` runs, it writes queued videos in the background, one
at a time: it fetches the video's captions (no API key) and has Claude Code
(`DAILY_AGENT_VIDEO_MODEL`, default `claude-code:sonnet`) write a headline and
a short article from them. A new video without captions waits up to 6 hours for
YouTube's auto-captions, then is written from its title and description.
Failures are retried after 1, 2, and 4 hours. After 4 tries the video is shown
as failed on the Sources page; following the channel again retries it.

Video stories mix into For you and Latest under a **Video** type chip, with the
channel as the source and the YouTube thumbnail as the cover. The reader embeds
the video (`youtube-nocookie.com`) above the article. They never go to Telegram.
Unfollowing stops new stories and keeps the ones already written.

```bash
uv run daily-agent videos add youtube.com/@name   # same as the Sources page
uv run daily-agent videos list
uv run daily-agent videos sync                    # one pass now (5 stories max)
uv run daily-agent videos remove channel:UC...
```

Set `DAILY_AGENT_WEB_VIDEO_SYNC=false` to stop the server from writing stories,
and run `videos sync` yourself instead.

### Instagram stories

Follow an Instagram **Business or Creator** account and its posts become stories
in the feed. Paste a profile link (`instagram.com/name`) on the Sources page. This
uses Instagram's official Graph API (Business Discovery), not scraping, so
personal and private accounts can't be followed. It works like video stories:
the latest 5 posts are queued at once, then each account is checked every 3
hours (`DAILY_AGENT_INSTAGRAM_CHECK_HOURS`).

Claude Code (`DAILY_AGENT_INSTAGRAM_MODEL`, default `claude-code:sonnet`) writes
each story from the caption and the post's images. It reads a carousel's slides,
where these posts usually put their content. A reel's audio isn't available, so
a reel's story only covers its caption and cover frame. The images (up to 4 per
post) are saved in the database, because Instagram's image links expire. Cards
show the first image with the @username, and the reader shows the slides and a
link that opens the post in the Instagram app. These stories never go to
Telegram either.

One-time setup: an Instagram professional account linked to a Facebook Page, a
Meta app, and `daily-agent instagram connect`. See
[docs/instagram-setup.md](docs/instagram-setup.md). The command saves a Page
token, which doesn't expire, to `.env`. Restart the feed server afterwards.

```bash
uv run daily-agent instagram connect                   # once
uv run daily-agent instagram add instagram.com/name    # same as the Sources page
uv run daily-agent instagram list
uv run daily-agent instagram sync                      # one pass now (5 stories max)
uv run daily-agent instagram remove instagram:name
```

### Interview prep (resume deep dive)

Put interview prep for your resume in the feed: flashcards for your drafted
answers, and an explainer for every concept you want to master. The input is a
"deep dive" folder. It has a `README.md` whose progress tracker lists one
markdown file per resume point, grouped by job. Each point file has
`## 4. Likely interview questions` (`**Q1. ...**` followed by
`- **Answer:** ...`) and `## 5. Concepts to master`.

```bash
uv run daily-agent prep import ~/path/to/deep-dive   # once; run again after editing
uv run daily-agent prep status                        # flashcards and explainer progress
uv run daily-agent prep write                         # write queued explainers now (5 max)
```

- **Which points:** only points under a job in the tracker are imported. Add
  `--all` to include skills and general points.
- **Flashcards:** each answered question becomes a card under an **Interview**
  type chip. The question is the headline, and the card shows only its level
  ("Deep dive question · tap to answer"). The reader keeps the answer behind
  **Show answer**.
  - Answers still marked `⬜ OPEN` with nothing written are skipped. Partial
    ones show what's written plus a **Still open** note. "Reference design —
    confirm" drafts are left out.
- **Explainers:** each concept is queued. While `daily-agent web` runs, it has
  Claude Code (`DAILY_AGENT_PREP_MODEL`) write an explainer for it, under a
  **Concept** chip. An explainer teaches the concept and ties it to your own
  work using only facts from that point's notes. That costs about $0.05 each,
  since the whole point file goes along. Set
  `DAILY_AGENT_WEB_PREP_SYNC=false` to write them only with `prep write`.
- **Running it again:** nothing watches the folder, so run `prep import` again
  after editing. Changed answers are updated in place, keeping what you've
  seen or saved. Removed questions disappear, and changed concepts are
  rewritten.

Everything stays in the web feed; none of it goes to Telegram. The Sources page
shows how many explainers are left to write.

### On your phone (Tailscale)

Keep the server on loopback and let `tailscale serve` relay it to devices signed
into your tailnet only. This is not Funnel, so nothing is public:

```bash
tailscale serve --bg --http=80 http://127.0.0.1:8765   # persists across restarts
```

Then allow the Mac's tailnet names in `.env` and restart the job:

```bash
DAILY_AGENT_WEB_ALLOWED_HOSTS=<mac>.<tailnet>.ts.net,<mac>,<tailscale-ip>
launchctl kickstart -k gui/$(id -u)/com.daily-agent.web
```

On the phone (Tailscale app signed in to the same account), open
`http://<mac>/` and use "Add to Home Screen". WireGuard encrypts the traffic
end to end, so HTTP is fine inside the tailnet. If you enable HTTPS certificates
for the tailnet, use `tailscale serve --bg 8765` for an `https://` URL instead.
Remove the relay with `tailscale serve --http=80 off`. The Mac must be awake and
connected to Tailscale.

To read the feed while the Mac is asleep, run the feed on a small always-on
server instead and point the Mac at the same Postgres database; see
[docs/server.md](docs/server.md).

Avoid binding `DAILY_AGENT_WEB_HOST` to the Tailscale IP or `0.0.0.0`. The Tailscale
IP breaks `127.0.0.1` access and fails while Tailscale is down. `0.0.0.0` exposes
the feed to your LAN. The Host allowlist only guards against DNS rebinding; it is
not access control. In the phone's browser, "Add to Home Screen" opens it like an
app.

## Telegram

Telegram is optional. Configure the bot token, API ID, API hash, and bot username
from `.env.example`, then authorize the local account session once:

```bash
uv run daily-agent telegram-auth
uv run daily-agent telegram-check
```

`telegram-check` sends a direct test message. Insight delivery creates channels
only when `--to-telegram` is used. Remove idle app-created channels with:

```bash
uv run daily-agent telegram-reap --idle-days 30
```

## Automation

On macOS, install the transcript watcher:

```bash
scripts/install-insights-flush-launchd.sh
```

It watches the configured transcript directory, waits for file activity to become
quiet, then runs `insights flush --to-telegram`. The flush lock and debounce stamp
prevent overlapping or duplicate runs.

### When new insights appear

A flush reads only one project's folder: the Claude Code `SessionEnd` hook (or
the watcher above) runs it when a session ends. Sessions inside an editor over
ACP, such as Claude in Zed, almost never end (the editor keeps each thread's
process alive), so their transcripts would wait indefinitely. Install the sweep
to cover them:

```bash
scripts/install-insights-sweep-launchd.sh        # every 30 minutes; pass minutes to change
```

Every run reads, across all of `~/.claude/projects` (override with
`DAILY_AGENT_INSIGHTS_PROJECTS_DIR`), each transcript written to since it was
last read, skipping any written to in the last 10 minutes (`--settle-minutes`)
so a live conversation is read once it pauses. It shares the flush's cursors and
lock, so nothing is read twice and the two never run at once. New insights show
on the web feed immediately and are queued for Telegram; only the flush delivers,
so Telegram's pace is unchanged. After capture it writes headlines and draws
covers for insights that lack them. Logs go to `digests/sweep.{out,err}.log`; a
run missed while the Mac sleeps happens on wake.

Keep the web feed running with `scripts/install-web-launchd.sh`. Logs go to
`digests/web.{out,err}.log`; uninstall with
`launchctl unload ~/Library/LaunchAgents/com.daily-agent.web.plist`.

## Design

The pipeline is intentionally small:

```text
local transcripts -> marker/LLM extraction -> SQLite insight store
                  |-> web feed (reads the store directly; immediate)
                  `-> durable outbox -> pacer -> console/file/Telegram
```

All tests are offline. They use temporary SQLite databases, mocked HTTP, and fake
model agents. Secrets, transcripts, Telegram sessions, databases, and generated
output remain ignored by Git.
