# Product direction

`daily-agent` started as a personal coding-insight memory loop. It grew into a
news-style feed (YouTube and Instagram stories), and then into an interview-prep
app: the Interview tab's practice deck, daily DSA and system design cards, mock
interviews, and a morning drill. It is installed on the phone as a web app.

**Focus since 2026-11-25 (#162): interview prep.** Work insights from coding
sessions went unread (757 stored, none saved), so they are off by default. The
sweep reads sessions only for interview study, which becomes practice cards. The
feed hides the stored work insights, and the app opens on the Interview tab. New
work should make prep better. The insight pipeline below stays in the code,
behind `DAILY_AGENT_WORK_INSIGHTS`, but isn't where effort goes.

## Product principles

- **Private and local first.** Transcript input and SQLite state stay on the
  maintainer's computer. Only selected transcript excerpts are sent to the model.
- **Durable lessons only.** Keep facts, constraints, gotchas, and techniques that
  remain useful weeks later.
- **Explicit capture always works.** An `insight:` marker is stored without an LLM.
- **Extraction is best effort.** Gemini complements the marker lane and retries
  transient failures with bounded backoff.
- **Reading is pull, notifying is a trickle.** The web feed shows every insight
  as soon as it is stored. Telegram notifications are paced by quiet hours and
  per-run limits, and stay durable and deduplicated.
- **Destinations stay optional.** Console and file delivery work locally. Telegram
  may create private channels by insight type when requested.

## Current scope

- Claude Code JSONL transcript parsing and per-file cursors
- explicit marker capture
- structured Gemini extraction and scoring
- exact-key deduplication in SQLite
- durable outbox with retry and dead-letter handling
- local web feed: immediate, newest-first, filters/search/save/hide
- paced console, file, and private Telegram delivery
- filesystem-triggered macOS automation

## Next useful work

- Improve extraction quality using a small reviewed fixture set.
- Add a lightweight review command to edit, merge, or discard captured insights.
- Use resurfacing feedback to adjust ranking and cadence.
- Revisit Telegram pacing now that the web feed carries the full stream.
- Let more reader signals shape For you ranking. Saved already shortens revisit
  gaps; next could be per-type affinity, or "less like this".
- Add retention and export controls for transcript-derived data.

Work is tracked in GitHub Issues. This file records product direction rather than
claimable implementation tasks.
