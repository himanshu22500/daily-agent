"""Configuration, loaded from environment / .env.

Everything is namespaced under the ``DAILY_AGENT_`` prefix so it can live
alongside other tools' env vars without clashing.

The LLM is provider-agnostic: ``model`` is a Pydantic AI model string of the
form ``provider:model-name`` (e.g. ``anthropic:claude-sonnet-4-5``,
``openai:gpt-4o``, ``google-gla:gemini-2.0-flash``). The matching provider API
key is read from that provider's own standard env var (``ANTHROPIC_API_KEY``,
``OPENAI_API_KEY``, ...), which Pydantic AI handles for us.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DAILY_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM (provider-agnostic) ---
    model: str = "anthropic:claude-sonnet-4-5"
    # Cheaper/faster model for bulk synthesis (summary, per-person briefs).
    # Empty => use `model`. e.g. anthropic:claude-haiku-4-5
    fast_model: str = ""

    # --- GitHub ---
    github_token: str = ""
    github_org: str = ""
    # Comma-separated allowlist of repo names (e.g. "api,web,infra").
    # Empty => watch all repos in the org.
    github_repos: str = ""
    # Don't bother summarizing repos with no pushes in this many days.
    github_active_within_days: int = 30

    # --- GitHub Projects (v2) — the org's PM board, feeds the initiative model ---
    # Project number from its URL (…/orgs/<org>/projects/<NUMBER>).
    github_project_number: int = 0
    # Org/user that owns the project; empty => use github_org.
    github_project_owner: str = ""
    # Token with the read:project scope; empty => fall back to github_token (which
    # may lack project scope — set this if so).
    github_project_token: str = ""

    # --- Collection ---
    lookback_days: int = 1

    # --- Storage ---
    db_path: str = "daily_agent.db"

    # --- Outline (engineering docs) ---
    outline_url: str = ""
    outline_token: str = ""

    # --- Team identity mapping ---
    # Path to the (gitignored, PII) team.json mapping name <-> github.
    team_path: str = "team.json"
    # Canonical name (or handle) that "me" resolves to in `brief` / `--assignee me`.
    me: str = ""

    # --- Insight feed (personal recall stream from Claude Code sessions) ---
    # Marker that flags a verbatim insight in a chat message (case-insensitive).
    insights_marker: str = "insight:"
    # Where Claude Code stores THIS project's transcripts. Empty => derive from the
    # current working dir (~/.claude/projects/<cwd-with-slashes-as-dashes>).
    insights_transcripts_dir: str = ""
    # Max insight bites delivered per `insights feed` run. Kept separate from the
    # activity feed cap so personal recall never competes with org activity.
    insights_feed_max_per_run: int = 2

    # --- Daily digest ---
    digest_dir: str = "digests"

    # --- Feed delivery ---
    # Default channel for `feed` (and scheduled runs) when no --to-* flag is
    # passed. One of: console | telegram | slack | file. Repo default is the
    # zero-config console; set DAILY_AGENT_FEED_CHANNEL=telegram to push by default.
    feed_channel: str = "console"

    # --- Feed cadence (the pacer) ---
    # Max chapters delivered per `feed` run; run feed periodically so the backlog
    # trickles out instead of flooding. An explicit `--limit` overrides this.
    feed_max_per_run: int = 3
    # Quiet hours [start, end) on a 24h local clock; may wrap midnight. No
    # delivery during quiet hours (bites stay queued). Set equal to disable.
    feed_quiet_start: int = 22
    feed_quiet_end: int = 8
    # Route each notification type to its own auto-provisioned Telegram channel
    # (needs MTProto configured). Off => single channel/DM.
    feed_multi_stream: bool = False
    # `telegram-reap` deletes channels unused for at least this many days.
    channel_reap_idle_days: int = 30

    # --- Slack delivery (feed channel) ---
    # Bot User OAuth token (xoxb-...) with the chat:write scope.
    slack_bot_token: str = ""
    # Where to deliver: a Slack user ID (U.../W...) to DM you (most reliable
    # notification), or a channel ID to post to a channel.
    slack_destination: str = ""

    # --- Telegram delivery (no-approval feed channel) ---
    # Bot token from @BotFather.
    telegram_bot_token: str = ""
    # Your numeric chat ID (DM the bot /start first; get the ID via @userinfobot).
    telegram_chat_id: str = ""

    # --- Telegram MTProto (multi-stream: auto-create per-type channels) ---
    # From my.telegram.org (API development tools). The session file IS your
    # account login — keep it gitignored, never in CI.
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    telegram_session: str = "telegram.session"
    # The bot's @username (without @), added as admin to created channels so it
    # can post — e.g. himanshu_daily_agent_bot.
    telegram_bot_username: str = ""

    # --- Response cache ---
    # Terminal entities (merged PRs) are cached permanently; everything else uses
    # these TTLs (seconds). Docs change rarely -> long TTL.
    cache_enabled: bool = True
    github_cache_ttl: int = 600  # 10 min
    projects_cache_ttl: int = 600  # 10 min (project board)
    outline_cache_ttl: int = 604800  # 7 days

    def repo_allowlist(self) -> list[str]:
        return [r.strip() for r in self.github_repos.split(",") if r.strip()]

    @property
    def bulk_model(self) -> str:
        """Model for high-volume synthesis (summary/briefs) — fast_model or model."""
        return self.fast_model or self.model

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_destination)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def telegram_mtproto_enabled(self) -> bool:
        return bool(
            self.telegram_api_id
            and self.telegram_api_hash
            and self.telegram_bot_username
        )

    @property
    def outline_enabled(self) -> bool:
        return bool(self.outline_url and self.outline_token)

    @property
    def project_owner(self) -> str:
        """The project's owner — explicit override, else the GitHub org."""
        return self.github_project_owner or self.github_org

    @property
    def project_token(self) -> str:
        """Token used for Projects v2 — dedicated one if set, else github_token."""
        return self.github_project_token or self.github_token

    @property
    def projects_enabled(self) -> bool:
        return bool(
            self.project_token and self.github_project_number and self.project_owner
        )

    @property
    def transcripts_path(self) -> str:
        """Claude Code transcript dir for this project (explicit, else derived)."""
        if self.insights_transcripts_dir:
            return self.insights_transcripts_dir
        mangled = str(Path.cwd().resolve()).replace("/", "-")
        return str(Path.home() / ".claude" / "projects" / mangled)


@lru_cache
def get_settings() -> Settings:
    return Settings()
