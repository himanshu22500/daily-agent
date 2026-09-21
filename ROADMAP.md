# Product direction

`daily-agent` is a personal coding-insight memory loop. Its job is to notice or
accept durable lessons during coding sessions, save them without duplicates, and
resurface a few at useful intervals.

## Product principles

- **Private and local first.** Transcript input and SQLite state stay on the
  maintainer's computer. Only selected transcript excerpts are sent to the model.
- **Durable lessons only.** Keep facts, constraints, gotchas, and techniques that
  remain useful weeks later.
- **Explicit capture always works.** An `insight:` marker is stored without an LLM.
- **Extraction is best effort.** Gemini complements the marker lane and retries
  transient failures with bounded backoff.
- **Resurfacing is a trickle.** Quiet hours and per-run limits prevent a backlog
  dump. Delivery remains durable and deduplicated.
- **Destinations stay optional.** Console and file delivery work locally. Telegram
  may create private channels by insight type when requested.

## Current scope

- Claude Code JSONL transcript parsing and per-file cursors
- explicit marker capture
- structured Gemini extraction and scoring
- exact-key deduplication in SQLite
- durable outbox with retry and dead-letter handling
- paced console, file, and private Telegram delivery
- filesystem-triggered macOS automation

## Next useful work

- Improve extraction quality using a small reviewed fixture set.
- Add a lightweight review command to edit, merge, or discard captured insights.
- Use resurfacing feedback to adjust ranking and cadence.
- Add retention and export controls for transcript-derived data.

Work is tracked in GitHub Issues. This file records product direction rather than
claimable implementation tasks.
