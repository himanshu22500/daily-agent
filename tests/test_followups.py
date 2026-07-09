"""Grounded Telegram follow-up answering (offline)."""

from __future__ import annotations

from daily_agent.config import Settings
from daily_agent.feed import followups as followups_mod
from daily_agent.feed.followups import (
    answer_followup,
    grounding_for_followup,
    initiative_key_for_followup,
    post_followup_answer,
)
from daily_agent.feed.initiatives_store import InitiativeStore
from daily_agent.feed.listener import FollowUp


def _followup(
    *,
    subject: str = "initiative:pm#1",
    dedup_key: str = "chapter:pm#1:abc",
    bite_text: str = "Billing Revamp\n\nReport generation moved to workers.",
) -> FollowUp:
    return FollowUp(
        chat_id="-100",
        message_id=29,
        text="what does that mean?",
        reply_to_message_id=28,
        dedup_key=dedup_key,
        subject=subject,
        bite_text=bite_text,
    )


def test_initiative_key_comes_from_subject_first():
    assert initiative_key_for_followup(_followup()) == "pm#1"


def test_initiative_key_falls_back_to_chapter_dedup_key():
    f = _followup(subject="repo:api", dedup_key="chapter:pm#2:def")
    assert initiative_key_for_followup(f) == "pm#2"


def test_grounding_includes_bite_and_story_state(tmp_path):
    store = InitiativeStore(tmp_path / "f.db")
    store.upsert("pm#1", "initiative", "Billing Revamp")
    store.record_chapter("pm#1", "Invoices now use the worker export path.")

    grounding = grounding_for_followup(_followup(), store)

    assert grounding.subject == "initiative:pm#1"
    assert grounding.initiative_title == "Billing Revamp"
    assert (
        grounding.initiative_story_state == "Invoices now use the worker export path."
    )
    assert "Report generation moved to workers" in grounding.bite_text


def test_grounding_uses_bite_text_when_no_story_state_store():
    grounding = grounding_for_followup(_followup(), None)

    assert grounding.subject == "initiative:pm#1"
    assert grounding.initiative_title is None
    assert grounding.initiative_story_state is None
    assert "Report generation moved to workers" in grounding.bite_text


async def test_answer_followup_passes_grounding_to_assistant(tmp_path, monkeypatch):
    store = InitiativeStore(tmp_path / "f.db")
    store.upsert("pm#1", "initiative", "Billing Revamp")
    store.record_chapter("pm#1", "Invoices now use the worker export path.")
    seen = {}

    async def fake_ask(model, question, github, **kwargs):
        seen["model"] = model
        seen["question"] = question
        seen["github"] = github
        seen.update(kwargs)
        return "answer"

    monkeypatch.setattr(followups_mod, "ask_anything", fake_ask)
    github = object()

    answer = await answer_followup(
        _followup(),
        model="test-model",
        github=github,
        settings=Settings(),
        team={},
        initiative_store=store,
    )

    assert answer == "answer"
    assert seen["model"] == "test-model"
    assert seen["question"] == "what does that mean?"
    assert seen["github"] is github
    assert seen["grounding"].initiative_title == "Billing Revamp"
    assert seen["grounding"].initiative_story_state.startswith("Invoices now")


def test_post_followup_answer_threads_under_user_message():
    class _Channel:
        def __init__(self) -> None:
            self.text = ""
            self.reply_to_message_id = None

        def send_text(self, text, *, reply_to_message_id=None):
            self.text = text
            self.reply_to_message_id = reply_to_message_id
            return 30

    channel = _Channel()
    receipt = post_followup_answer(channel, _followup(), "grounded answer")

    assert receipt == 30
    assert channel.text == "grounded answer"
    assert channel.reply_to_message_id == 29
