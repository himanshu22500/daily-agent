"""Answer inbound Telegram follow-ups against the bite they replied to."""

from __future__ import annotations

from ..agents.assistant import AssistantGrounding, ask_anything
from ..config import Settings
from ..sources.github import GitHubClient
from ..sources.outline import OutlineClient
from ..team import TeamMember
from .channels import TelegramChannel
from .initiatives_store import InitiativeStore
from .listener import FollowUp


_STORY_SUBJECT_LANES = {"initiative", "ops", "untracked"}


def initiative_key_for_followup(followup: FollowUp) -> str | None:
    """Return the story-state key the replied-to bite belongs to, if known."""
    if ":" in followup.subject:
        lane, key = followup.subject.split(":", 1)
        if lane in _STORY_SUBJECT_LANES and key:
            return key

    # Chapter dedup keys are `chapter:<initiative_key>:<hash>`.
    parts = followup.dedup_key.split(":", 2)
    if len(parts) >= 2 and parts[0] == "chapter" and parts[1]:
        return parts[1]
    return None


def grounding_for_followup(
    followup: FollowUp, initiative_store: InitiativeStore | None
) -> AssistantGrounding:
    """Build assistant grounding from the replied-to bite and story-state."""
    key = initiative_key_for_followup(followup)
    state = initiative_store.get(key) if initiative_store and key else None
    return AssistantGrounding(
        subject=followup.subject,
        bite_text=followup.bite_text,
        initiative_title=state.title if state else None,
        initiative_story_state=state.story_state if state else None,
    )


async def answer_followup(
    followup: FollowUp,
    *,
    model: str,
    github: GitHubClient,
    settings: Settings,
    team: dict[str, TeamMember],
    outline: OutlineClient | None = None,
    initiative_store: InitiativeStore | None = None,
) -> str:
    """Answer a Telegram follow-up with bite/story-state grounding."""
    return await ask_anything(
        model,
        followup.text,
        github,
        settings=settings,
        team=team,
        outline=outline,
        grounding=grounding_for_followup(followup, initiative_store),
    )


def post_followup_answer(
    channel: TelegramChannel, followup: FollowUp, answer: str
) -> int | None:
    """Post ``answer`` threaded under the user's follow-up message."""
    return channel.send_text(answer, reply_to_message_id=followup.message_id)
