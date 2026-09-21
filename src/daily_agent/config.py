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

    model: str = "google:gemini-3.8-flash"
    db_path: str = "daily_agent.db"

    insights_marker: str = "insight:"
    insights_transcripts_dir: str = ""
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
