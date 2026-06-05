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

    # --- GitHub ---
    github_token: str = ""
    github_org: str = ""
    # Comma-separated allowlist of repo names (e.g. "api,web,infra").
    # Empty => watch all repos in the org.
    github_repos: str = ""
    # Don't bother summarizing repos with no pushes in this many days.
    github_active_within_days: int = 30

    # --- Collection ---
    lookback_days: int = 1

    # --- Storage ---
    db_path: str = "daily_agent.db"

    # --- Outline (engineering docs) ---
    outline_url: str = ""
    outline_token: str = ""

    # --- Huly (task tracking, via the Node bridge) ---
    huly_url: str = "https://huly.app"
    huly_workspace: str = ""
    huly_email: str = ""
    huly_password: str = ""
    huly_token: str = ""
    # All work often lives in one project; set this so `tasks` lists its issues
    # by default instead of the (single-item) project list.
    huly_default_project: str = ""
    node_bin: str = "node"

    # --- Team identity mapping ---
    # Path to the (gitignored, PII) team.json mapping name <-> huly <-> github.
    team_path: str = "team.json"
    # Canonical name (or handle) that "me" resolves to in `brief` / `--assignee me`.
    me: str = ""

    # --- Daily digest ---
    digest_dir: str = "digests"

    # --- Response cache ---
    # Terminal entities (merged PRs, DONE Huly issues) are cached permanently;
    # everything else uses these TTLs (seconds). Docs change rarely -> long TTL.
    cache_enabled: bool = True
    github_cache_ttl: int = 600       # 10 min
    huly_cache_ttl: int = 600         # 10 min (non-DONE issues / lists)
    outline_cache_ttl: int = 604800   # 7 days

    def repo_allowlist(self) -> list[str]:
        return [r.strip() for r in self.github_repos.split(",") if r.strip()]

    @property
    def outline_enabled(self) -> bool:
        return bool(self.outline_url and self.outline_token)

    @property
    def huly_enabled(self) -> bool:
        has_auth = bool(self.huly_token or (self.huly_email and self.huly_password))
        return bool(self.huly_workspace and has_auth)


@lru_cache
def get_settings() -> Settings:
    return Settings()
