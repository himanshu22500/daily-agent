# Roadmap / Ideas

A running backlog of improvements, captured for later. Not yet built unless it's
in the README's "Done" list.

## Known gaps / fixes

- [x] **Single-task detail command** — added `task ENG-12345`: prints status,
      assignee, priority, description (markdown), and extracts linked GitHub PR
      URLs from the description.

- [x] **`tasks` default project** — `tasks` now lists issues for
      `DAILY_AGENT_HULY_DEFAULT_PROJECT` (set to `ENG`) by default; pass a project
      explicitly to override, or `--projects` to list projects.

## Personal / task-centric Q&A

- [x] **"What is <person> working on this week?"** — added the `brief [PERSON]`
      command: pulls a person's Huly tasks (updated in the window) + their GitHub
      PRs. Defaults to "me". Backed by a team identity map (`team.py` +
      gitignored `team.json`) mapping name ↔ Huly ↔ GitHub. `tasks --assignee me`
      / `--assignee <name>` resolve through the same map.
      - Possible follow-up: an optional LLM synthesis ("X is focused on …")
        rather than the current deterministic listing.

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
          *(Partially done: the `task` command already extracts PR URLs from an
          issue's description via `_github_pr_links`.)*
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
