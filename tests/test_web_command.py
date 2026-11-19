"""`daily-agent web` only writes in the background where Claude Code exists."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from daily_agent import cli


@pytest.fixture
def started(monkeypatch, tmp_path):
    """Run `daily-agent web` with create_app and uvicorn faked; return its kwargs."""
    seen: dict = {}

    def fake_create_app(db, **kwargs):
        seen.update(kwargs)
        return object()

    import uvicorn

    import daily_agent.web.app as web_app

    monkeypatch.setattr(web_app, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "w.db"))

    def run() -> tuple[dict, str]:
        seen.clear()
        cli.get_settings.cache_clear()
        try:
            result = CliRunner().invoke(cli.app, ["web"])
        finally:
            cli.get_settings.cache_clear()
        assert result.exit_code == 0, result.output
        return dict(seen), result.output

    return run


def test_without_claude_nothing_is_written_in_the_background(started, tmp_path):
    # conftest points DAILY_AGENT_CLAUDE_BIN at a path that doesn't exist.
    kwargs, output = started()
    assert kwargs["video_loop"] is False and kwargs["prep_loop"] is False
    assert "No claude CLI here" in output


def test_with_claude_the_settings_decide(started, tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("DAILY_AGENT_CLAUDE_BIN", str(fake))
    kwargs, output = started()
    assert kwargs["video_loop"] is True and kwargs["prep_loop"] is True
    assert "No claude CLI" not in output
    monkeypatch.setenv("DAILY_AGENT_WEB_PREP_SYNC", "false")
    kwargs, _ = started()
    assert kwargs["video_loop"] is True and kwargs["prep_loop"] is False
