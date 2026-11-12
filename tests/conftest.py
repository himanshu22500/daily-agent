"""Suite-wide guards: tests never reach a real model."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch, tmp_path_factory):
    # Any `claude-code:` call that a test forgot to fake fails fast instead of
    # running the signed-in Claude Code CLI on the developer's machine.
    missing = tmp_path_factory.getbasetemp() / "no-claude-in-tests"
    monkeypatch.setenv("DAILY_AGENT_CLAUDE_BIN", str(missing))
