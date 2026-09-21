# Decisions

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
