# Roadmap / Ideas

A running backlog of improvements, captured for later. Not yet built unless it's
in the README's "Done" list.

## Known gaps / fixes

- [ ] **No command to fetch a single task's details.** The Node bridge already
      supports it (`node bridges/huly/index.js issue ENG-12345`, returns the full
      issue incl. markdown description), and `sources/huly.py` exposes
      `HulyClient.issue(identifier)` — it just isn't surfaced as a CLI command.
      → Add a `task ENG-12345` command that prints the issue detail (status,
      assignee, priority, description, linked PRs).

- [ ] **`tasks` (with no project) is redundant.** All work lives in a single
      Huly project (`ENG`), so listing projects always returns just `ENG`.
      → Make `tasks` default to listing `ENG` issues directly. Consider a
      `DAILY_AGENT_HULY_DEFAULT_PROJECT=ENG` setting so the default project is
      configurable, and keep an explicit `--projects` flag only if multi-project
      ever happens.

## Personal / task-centric Q&A

- [ ] **"What am I working on this week?"** No current command answers a
      person-centric, time-windowed question well. `tasks ENG` lists everything;
      `ask` is repo-first; `howto` is Outline-only.
      → Add a `brief` / `mine` command (or a Huly-aware Q&A agent like `howto`
      but over tasks) that pulls *my* Huly issues (open / in-review, modified
      this week) and attaches their GitHub PRs.
      → Needs an **identity mapping**: Huly "Himanshu Mishra" ↔ GitHub
      `himanshu22500`. Ideally a small team-wide name↔login map for accuracy.

## Reliable task ↔ PR mapping

- [ ] **Today the task→PR mapping is LLM-inferred, not deterministic.** During
      `ask`, the agent connects Huly issues and GitHub PRs by reading text. Two
      real signals exist in the data:
        1. Explicit PR URLs inside Huly issue descriptions/comments (added by the
           `gh pr create` hook, `_gh_pr_create_with_huly`).
        2. `ENG-\d+` identifiers in PR titles, branch names, and commit messages.
      → Build a deterministic cross-linker:
        - Parse `ENG-\d+` from PR titles / branches / commit messages (GitHub side).
        - Parse GitHub PR URLs from Huly issue bodies/comments (Huly side).
        - Produce a real task ↔ PR(s) map that agents and `brief` can use directly,
          instead of relying on inference.

## Scheduling & delivery

- [ ] **Recurring execution** — run `collect` + a digest on a schedule
      (cron / launchd / GitHub Actions — deferred pending a delivery target).
- [ ] **Digest delivery & history** — write dated digests somewhere durable:
      terminal, a markdown file, email, or Slack.

## Smaller niceties

- [ ] One-shot `brief` that runs `collect` + `summary` together.
- [ ] Rank projects in the digest by activity volume.
- [ ] Per-command model selection (e.g. a cheaper model for `summary`, a stronger
      one for `ask`) to manage cost.
