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
| Insight extraction | `DAILY_AGENT_MODEL` | `claude-code:claude-opus-5-5` |
| Feed headlines | `DAILY_AGENT_HEADLINE_MODEL` | `claude-code:claude-opus-5-5` |
| Cover illustrations | `DAILY_AGENT_COVER_MODEL` | `claude-code:claude-opus-5-5` |

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
♡ save, share, 👎 not useful, and ⋮ (hide, copy). Tap a card to open it like an article. It
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

**Prep mode** (the switch at the top of For you, on by default) mixes
interview prep into For you: about every other card is a flashcard or concept
explainer, spread evenly through the edition (`DAILY_AGENT_PREP_MIX`, default
`0.5`). The prep cards rotate exactly like the Interview tab's deck (unseen
first; seen cards come back when due) and behave the same: the answer stays
hidden until you tap it. To make room, each edition keeps only the best work
stories (higher score first, then unseen before revisits). The rest wait for a
later mix. An edition is as long as it would be without prep, and at least
30 cards while there's prep to show, so being caught up on work still leaves
a practice run. A type chip still shows just that type. The switch is stored
on the server, so the phone and the Mac agree. Turn it off and For you is
exactly as described above.

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

On a day a channel brings nothing new, it brings one older video instead: the
6th, then the 7th, and so on, one a day, back through the channel's uploads
(for a playlist, further down the playlist). A day with a new upload brings no
older video. Days start at local midnight on the machine that writes the
stories. Videos older than YouTube's feed reaches (about 15) are read from the
channel's public uploads list, up to about 1,000 back. Private or removed
videos are skipped. Set `DAILY_AGENT_VIDEO_BACKFILL=false` to turn this off.

While `daily-agent web` runs, it writes queued videos in the background, one
at a time: it fetches the video's captions (no API key) and has Claude Code
(`DAILY_AGENT_VIDEO_MODEL`, default `claude-code:claude-opus-5-5`) write a headline and
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

Claude Code (`DAILY_AGENT_INSTAGRAM_MODEL`, default `claude-code:claude-opus-5-5`) writes
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
- **The Interview tab:** all of it lives in its own tab, next to For you and
  Latest. It mixes into For you only while Prep mode is on, never into Latest
  (Saved, Hidden and search still include it). It's a practice deck: every visit shuffles a fresh deck with
  unseen questions first. A question you've seen leaves the deck and comes
  back after 1 day, then 3 days, a week, 3 weeks and 2 months (sooner if
  saved). Chips at the top narrow the deck to one resume point.
- **Flashcards:** each answered question becomes a card. The question is the
  headline, and the card shows only its level ("Deep dive question · tap to
  answer"), with no cover. The reader keeps the answer behind **Show answer**.
  - Answers still marked `⬜ OPEN` with nothing written are skipped. Partial
    ones show what's written plus a **Still open** note. "Reference design —
    confirm" drafts are left out.
- **Explainers:** each concept is queued. While `daily-agent web` runs, it has
  Claude Code (`DAILY_AGENT_PREP_MODEL`) write an explainer for it, after
  that point's questions. An explainer teaches the concept and ties it to your own
  work using only facts from that point's notes. That costs about $0.05 each,
  since the whole point file goes along. Set
  `DAILY_AGENT_WEB_PREP_SYNC=false` to write them only with `prep write`.
- **Running it again:** nothing watches the folder, so run `prep import` again
  after editing. Changed answers are updated in place, keeping what you've
  seen or saved. Removed questions disappear, and changed concepts are
  rewritten.

### Prep tracks: Resume, DSA and System design

Interview prep has three tracks (#130): **Resume** (the flashcards and
explainers above), **DSA** (`dsa` problems and `drill` pattern quizzes) and
**System design** (`design` walkthroughs and recall cards). Code that adds
prep cards goes through `PrepItems.put` in `feed/prep_tracks.py`, under a
labelled source. A card's text can reveal in steps: one tap per `### <title>`
section after the prompt. Grading a card Got it, Shaky or Missed
(`feed/prep_grades.py`) reschedules it: tomorrow, in 3 days, or in 7, 21 and
then 60 days.

Everything stays in the web feed; only the morning drill (below) goes to
Telegram. The Sources page
shows how many explainers are left to write.

### Practice packs and talk-track notes

Study material you already wrote can go straight into the DSA and System design
tracks (#135). No model is called.

```bash
uv run daily-agent prep import-pack ~/path/to/blinkit-dsa-round1 \
  --source dsa-blinkit-r1 --label "Blinkit DSA pack"
uv run daily-agent prep import-notes ~/path/to/rate_limiter_notes.md \
  --tags "rate-limiting,redis" --label "Rate limiter notes"
```

- **A DSA pack** is a folder with a `README.md` index table (`#`, problem link,
  pattern, target complexity), `problems/NN-*.md` and `answers/NN-*.md`. Each
  problem becomes one `dsa` card: its name is the headline, its statement the
  prompt, and its answer reveals in steps: **Pattern** (with the target
  complexity), **Key idea**, **Brute force**, **Optimal solution**,
  **Walkthrough**, **Edge cases & follow-ups**. Answer sections are matched to
  those steps by heading ("Intuition", "Translating the wording", "Naive",
  "Optimal A", "Dry run", "Common mistakes", ...). A section the importer
  doesn't recognise ("Faster reads") stays in the step before it. Code fences
  are kept as written.
- **Tags:** the index's pattern column maps to the shared slugs in
  `DSA_PATTERNS` (`sources/practice_pack.py`), e.g. "two heaps (Meeting Rooms
  III)" becomes `heap` and `intervals`. The pack's company comes from the README
  title (`blinkit`), and the "do these first" problems get `must-do`. Add more
  with `--tags`. Problems with no known pattern, missing answers and unmatched
  index rows are listed after the import.
- **Talk-track notes:** one markdown file becomes one card (`--track design` by
  default). The `# ` title is the headline, the prompt is the interview question
  ("Design a rate limiter. Talk me through it.", or `--question`), and each
  `## ` section is one step, in order. Tags are `--tags` plus the topic.
- **Running it again** after editing updates changed cards in place, keeping
  what you've seen and graded, and drops problems you removed. Each `--source`
  is replaced only by its own import; the defaults are `dsa-<folder>` and
  `notes-<file>`.

### Practice cards from videos and study sessions

Interview-prep videos and study sessions with Claude Code add practice cards
to the DSA and System design tracks (#139). No extra model call is made for
either.

- **Videos.** When Claude writes a video's story, it also writes 0–5 cards,
  and none at all for a video that isn't about interview prep, DSA or system
  design. A card is a question that tests recall or understanding, with
  **Answer** and, when useful, **Why it matters** revealed one tap at a time.
  DSA cards are pattern drills tagged with `DSA_PATTERNS` slugs; design cards
  are tagged with topics such as `caching` or `sharding`. The channel is the
  source ("▶ Hello Interview"). Writing a video again replaces its cards
  rather than adding more.
- **Videos written before this** get their cards from the stored story. No
  captions are fetched, and videos already done are skipped. Run it on the
  Mac, since the cards are written by Claude Code (`DAILY_AGENT_VIDEO_MODEL`):

  ```bash
  uv run daily-agent prep cards-from-videos --limit 5             # newest first
  uv run daily-agent prep cards-from-videos --channel "hello interview"
  ```

- **Study sessions.** A Claude Code session is interview study when it runs
  in a study folder (`DAILY_AGENT_PREP_STUDY_PATHS`, comma-separated folder
  names or paths, default `interview-prep`), or when the extractor recognises
  one from the conversation: a mock interview, working through an interview
  question, or learning DSA patterns or design concepts for interviews. Such
  a session yields Q&A cards, plus a step-by-step walkthrough card when a
  whole design was worked through. They appear under "From your study
  sessions" and the session yields no coding insights; `insight:` notes are
  still captured. The same question studied twice stays one card. Work
  sessions are unchanged.

### Morning prep drill and the Progress page

A daily rhythm for the DSA and System design rounds (#136). No model is
called.

- **Morning drill on Telegram:** at `DAILY_AGENT_PREP_DRILL_HOUR` (default 9)
  the bot sends one DSA card and one design card, prompt only, each with a
  link to the card in the feed (`DAILY_AGENT_FEED_URL`). Due or unseen cards
  on your weak tags come first, and the same card never comes two days
  running.
- **Answers in the evening:** at `DAILY_AGENT_PREP_ANSWER_HOUR` (default 20)
  it sends the answers to the same two cards: a DSA card's Pattern and Key
  idea, a design card's 30-second recap (else its first step). Grade them in
  the feed.
- **Where it runs:** inside `daily-agent web`, on whichever machine has
  Telegram configured. Each slot goes out once a day, even if the Mac and the
  server both run it. A Mac that slept through 9 am sends the drill when it
  wakes, until the evening hour, and the answers only follow a drill. Hours
  are in `DAILY_AGENT_TIMEZONE` (default `Asia/Kolkata`), and the feed's
  quiet hours apply. Turn it off with `DAILY_AGENT_PREP_DRILL_TELEGRAM=false`.
- **`/progress`** (the **Progress** link in the Interview tab's header): a
  countdown to `DAILY_AGENT_INTERVIEW_DATE` (e.g. `2026-12-18`; hidden when
  unset), your streak of days with a grade, and today's grades. Per track, it
  shows cards, seen, graded and the got-it rate. A grid has a tile per DSA
  pattern (gaps where there are no cards yet) and per design topic, each
  opening that pattern in the Interview tab. Then your 5 weakest tags and the
  last 7 days as bars. It only reads the shared database, so the server shows
  it too.

### Daily system design cards

Each day, Claude writes one system design **case study** and one **building
block** for the System design track (#134), pitched at a mid-level interview.
They come from a curated list of about 45 topics in `feed/design_topics.py`:
quick-commerce case studies (dark-store inventory, rider dispatch, delivery
ETA, flash sale, ...), the classics (URL shortener, rate limiter, news feed,
chat, ...) and building blocks (caching, sharding, Kafka, idempotency, ...).
A hand-written latency and throughput cheat sheet goes in once, with no model
call.

```bash
uv run daily-agent prep design status          # written, today's picks, next up (--all: every topic)
uv run daily-agent prep design write           # write today's cards now
uv run daily-agent prep design write --limit 3 # ...plus the next topics, ahead of the daily pace
uv run daily-agent prep design write --topic url-shortener   # one topic, again if written
```

- **A case study** ("Design a URL shortener") opens with the interviewer's
  question, what a mid-level answer must cover and "Try it for 10 minutes
  before revealing". Then it reveals in steps: Requirements, Core entities,
  API, High-level design, Deep dives, Senior extras (optional) and a
  30-second recap.
- **A building block** ("Consistent hashing") asks an interview question,
  then reveals Answer, How it works, Trade-offs and Where it comes up in
  interviews.
- **Order:** quick-commerce topics and topics that touch your weak design tags
  (cards you graded Missed or Shaky) come first, then the classics, then the
  rest. A written topic never comes back unless you ask with `--topic`.
- **Pace:** `DAILY_AGENT_PREP_DESIGN_PER_DAY` case studies and
  `DAILY_AGENT_PREP_BLOCKS_PER_DAY` building blocks (1 each) per local day.
  `daily-agent web` writes them in the background on the Mac. It checks every
  15 minutes, so after the Mac sleeps it writes today's cards on waking;
  missed days aren't made up. `--limit` and `--topic` are extras and don't use
  up a day. Set `DAILY_AGENT_WEB_PREP_DESIGN_SYNC=false` to write them only
  by hand. Hosts without the claude CLI (the feed server) never write them.
- **Model:** `DAILY_AGENT_PREP_MODEL` through `claude -p`, with no tools. The
  JSON schema carries the length limits. A failed card is retried after 1, 2
  and 4 hours, then waits until every other topic is done.
- Cards are `design` type, `web-only`, from the source "Daily system design",
  tagged with the topic and the building blocks it uses.

### Daily DSA problems and "which pattern?" drills

Every local day Claude (`prep_model`) adds new cards to the DSA track (#138):
one Blinkit-flavoured, LeetCode-medium problem (source "Daily DSA") and three
"which pattern?" drills (source "Pattern drills").

```bash
uv run daily-agent prep dsa status                  # written so far, today's jobs, next pattern
uv run daily-agent prep dsa write                   # today's cards now, if not written yet
uv run daily-agent prep dsa write --pattern heap    # one extra problem on a pattern
uv run daily-agent prep dsa write --limit 2 --drills 5   # extra problems and drills
```

- **Aimed at weak spots:** the problem goes to your weakest pattern from your
  grades (tags like `blinkit` or `must-do` don't count). With nothing weak yet,
  it goes to the pattern with the fewest problems. About two thirds of the
  drills go to patterns you missed lately or are weak on, and the rest to the
  least-drilled ones, so every pattern comes up over time.
- **A problem card** reveals like a practice-pack card: the prompt (scenario,
  task, signature, constraints, examples, questions to ask), then **Pattern**,
  **Key idea**, **Brute force**, **Optimal solution**, **Walkthrough** and
  **Edge cases & follow-ups**. Claude is told the titles you already have on
  that pattern, and a title already in the feed is sent back.
- **The solution is run before you see it.** It must pass the test cases
  written with it. They run in a separate Python with no site-packages, no
  network and no files outside the standard library. A failure goes back to
  Claude once, with the failing tests. A second failure rejects the problem,
  and the day's job is retried later.
- **A drill** is 2-4 scenario lines, a question, and options `A)` to `D)`;
  **Answer** reveals the right option and why, then **Why not the others**.
  It's tagged with the right pattern and `drill`.
- **Pacing:** `daily-agent web` on the Mac writes them in the background (it
  needs the claude CLI; the feed server never does). After the Mac sleeps, it
  writes one day's worth, not one per missed day. Set
  `DAILY_AGENT_PREP_DSA_PER_DAY` / `DAILY_AGENT_PREP_DRILLS_PER_DAY` to change
  the counts, or `DAILY_AGENT_WEB_PREP_DSA_SYNC=false` to write only with
  `prep dsa write`.

### Rate items: 👍 / 👎

Every card has a 👎, and every item ends with "Was this worth reading? 👍
Useful / 👎 Not useful".

- **👎 removes the item.** It moves to Hidden, with an Undo. A sheet then asks
  why. The answer is optional: too basic, not relevant, wrong or outdated,
  repeats another, fewer from this project or channel, or fewer of this type.
- **👍 marks it useful.** It also brings back an item hidden earlier.
- **Claude learns from both.** Each extraction run reads the latest 20 👎 and
  10 👍 coding insights, with their reasons, into the prompt. It also tells
  Claude which project the session is from. So Claude writes fewer items like
  the 👎 ones and more like the 👍 ones, and is stricter for projects you asked
  for fewer of. Ratings on videos, Instagram posts and interview cards only
  hide or keep them; those items aren't written by the extractor.

Ratings live in `feed_ratings`.

### Ask Claude about an item

Every article and interview card ends with an **Ask about this** box. Type a
question ("why does this matter?", "what would they ask next?") and Claude
answers it below the item. When the answer is ready, Telegram sends you the
question, the start of the answer, and a link that opens the item at the
answer.

- **Where it's answered:** on the Mac, by `daily-agent web` (Claude Code,
  `DAILY_AGENT_ASK_MODEL`). The feed server has no `claude`, so it only stores the
  question. The Mac checks for new ones every 15 seconds
  (`DAILY_AGENT_WEB_ASK_POLL_SECONDS`), and while it's asleep they wait.
  `uv run daily-agent questions answer` answers them by hand, and
  `questions list` shows them. Set `DAILY_AGENT_WEB_ASK_SYNC=false` to answer
  only by hand.
- **What Claude sees:** the item's headline and text and any earlier questions
  about it, with their answers. For interview cards and concept explainers it
  also sees that resume point's notes. Facts about your own work come only from
  those; general knowledge fills in the rest.
- **Telegram:** the message goes to the bot chat (`DAILY_AGENT_TELEGRAM_CHAT_ID`)
  from the Mac. Set `DAILY_AGENT_FEED_URL` to the feed's address on the phone,
  e.g. `http://daily-agent.<tailnet>.ts.net`, for the link. Use the full name
  with dots, since Telegram doesn't link a bare `http://daily-agent/`. A message
  that fails to send is tried again a few times; the answer is in the feed
  either way.
- **Limits:** a question can be up to 1,000 characters, and at most 20 can be
  waiting at once.

### Mock interview ("Interview me")

Every interview-prep card (resume, DSA, system design) has **Interview me**
under its Ask box. Claude plays a mid-level interviewer about that card, in the
same chat:

- **One question at a time.** System design starts with requirements, then the
  design, then probes ("what happens when the cache node dies?"). DSA goes
  clarify, approach, complexity, edge cases. A resume card gets a probing
  follow-up on the claim; facts about your work come only from the card.
- **After each answer:** a line or two of feedback, then the next question.
- **The verdict:** after `DAILY_AGENT_INTERVIEW_QUESTIONS` questions (default
  4), or when you tap **End & grade**: what was strong, what was missing, a
  short model-answer outline, and a suggested self-grade (Got it / Shaky /
  Missed). Senior-only points are listed as optional and don't count against
  you.
- **Where it runs:** like Ask, on the Mac (`DAILY_AGENT_ASK_MODEL`). The feed
  server only stores each turn (`feed_interview_turns`). `questions answer`
  works through waiting turns by hand too.
- **Telegram stays quiet** while you're answering. It pings once the verdict is
  ready, or when a question took more than 2 minutes to come (the Mac was
  asleep).
- **Start over** drops a turn still waiting for the Mac and opens a new
  interview. It waits while Claude is writing a reply or grading.

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
