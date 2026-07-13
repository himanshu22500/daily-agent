"""Assistant prompt shaping for grounded follow-ups (offline)."""

from __future__ import annotations

from daily_agent.agents import assistant as assistant_mod
from daily_agent.agents.assistant import (
    AssistantGrounding,
    ask_anything,
    person_activity_text,
)
from daily_agent.config import Settings
from daily_agent.sources.github import GitHubError
from daily_agent.team import TeamMember


class _Result:
    output = "grounded answer"


class _FakeAgent:
    def __init__(self) -> None:
        self.prompt = ""
        self.deps = None

    async def run(self, prompt, *, deps):
        self.prompt = prompt
        self.deps = deps
        return _Result()


async def test_ask_anything_includes_followup_grounding(monkeypatch):
    fake = _FakeAgent()
    monkeypatch.setattr(assistant_mod, "build_assistant", lambda model: fake)

    answer = await ask_anything(
        "test-model",
        "what does this mean?",
        github=object(),
        settings=Settings(),
        team={},
        grounding=AssistantGrounding(
            subject="initiative:pm#1",
            initiative_title="Billing Revamp",
            initiative_story_state="Invoices now use the new export pipeline.",
            bite_text="Billing Revamp\n\nReport generation moved to workers.",
        ),
    )

    assert answer == "grounded answer"
    assert "Telegram feed bite" in fake.prompt
    assert "initiative:pm#1" in fake.prompt
    assert "Billing Revamp" in fake.prompt
    assert "Invoices now use the new export pipeline." in fake.prompt
    assert "Report generation moved to workers." in fake.prompt
    assert "what does this mean?" in fake.prompt


async def test_person_activity_returns_github_error_as_context():
    class _GitHub:
        async def search_pull_requests(self, author, since, *, limit):
            raise GitHubError("GET /search/issues -> 422: invalid author")

    text = await person_activity_text(
        _GitHub(),
        Settings(),
        {"Alice": TeamMember(name="Alice", github="alice-missing")},
        "Alice",
    )

    assert "Alice (github: alice-missing)" in text
    assert "GitHub error while searching this person's PRs" in text
    assert "invalid author" in text
