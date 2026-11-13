"""Configuration for the personal insight capture and resurfacing agent."""

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

    # Insight extraction. `claude-code:<alias>` runs the local Claude Code CLI
    # (`claude -p`); other strings are pydantic-ai models (e.g. google:...).
    model: str = "claude-code:sonnet"
    # Feed headlines (Claude Code only) and cover illustrations.
    headline_model: str = "claude-code:sonnet"
    cover_model: str = "claude-code:sonnet"
    # Stories from followed YouTube channels (Claude Code only), and how often
    # each followed channel is checked for new uploads.
    video_model: str = "claude-code:sonnet"
    video_check_hours: float = 3.0
    # Instagram (issue #107), set by `daily-agent instagram connect`: a Facebook
    # Page token (doesn't expire) and the id of your own Instagram professional
    # account, which Business Discovery reads other accounts through.
    instagram_token: str = ""
    instagram_user_id: str = ""
    instagram_model: str = "claude-code:sonnet"
    instagram_check_hours: float = 3.0
    # Concept explainers for interview prep (`daily-agent prep import`).
    prep_model: str = "claude-code:sonnet"
    # Answers to questions asked under a feed item (issue #119).
    ask_model: str = "claude-code:sonnet"
    # The feed's address on the phone, for links in Telegram messages, e.g.
    # http://daily-agent.<tailnet>.ts.net (a name with a dot, so Telegram
    # makes it a link).
    feed_url: str = ""
    db_path: str = "daily_agent.db"
    # Shared database (issue #111), e.g. postgresql://user:pass@daily-agent/db.
    # When set, every store uses it instead of the SQLite file at db_path; lock
    # and stamp files still live next to db_path because they are per-machine.
    database_url: str = ""

    insights_marker: str = "insight:"
    insights_transcripts_dir: str = ""
    # Root the timed sweep reads (every <project>/<session>.jsonl under it).
    insights_projects_dir: str = ""
    insights_feed_max_per_run: int = 2
    feed_quiet_start: int = 22
    feed_quiet_end: int = 8

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    telegram_session: str = "telegram.session"
    telegram_bot_username: str = ""
    channel_reap_idle_days: int = 30

    # Web feed (`daily-agent web`). Loopback-only by default; set the host to a
    # private interface (e.g. a Tailscale IP) to read it from another device.
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_page_size: int = 20
    # Extra Host names the feed answers to (comma-separated), e.g. a Tailscale
    # MagicDNS name. Loopback names and web_host are always allowed.
    web_allowed_hosts: str = ""
    # Write stories for followed YouTube channels and Instagram accounts in the
    # background while the feed server runs. Off: run `daily-agent videos sync`
    # and `daily-agent instagram sync` instead.
    web_video_sync: bool = True
    # Write queued interview-prep explainers in the background (else:
    # `daily-agent prep write`).
    web_prep_sync: bool = True
    # Answer questions asked in the feed in the background (else: `daily-agent
    # questions answer`), checking for new ones this often. They are usually
    # asked on the feed server, so the Mac has to look for them.
    web_ask_sync: bool = True
    web_ask_poll_seconds: float = 15.0

    @property
    def db(self) -> str:
        """What the stores open: the shared database URL, else the SQLite file."""
        return self.database_url or self.db_path

    @property
    def instagram_enabled(self) -> bool:
        return bool(self.instagram_token and self.instagram_user_id)

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
    def transcripts_path(self) -> str:
        if self.insights_transcripts_dir:
            return self.insights_transcripts_dir
        mangled = str(Path.cwd().resolve()).replace("/", "-")
        return str(Path.home() / ".claude" / "projects" / mangled)

    @property
    def projects_path(self) -> str:
        if self.insights_projects_dir:
            return str(Path(self.insights_projects_dir).expanduser())
        return str(Path.home() / ".claude" / "projects")


@lru_cache
def get_settings() -> Settings:
    return Settings()
