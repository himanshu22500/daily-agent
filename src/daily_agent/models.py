"""Domain models shared across sources, storage, and agents."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Raw activity collected from sources
# --------------------------------------------------------------------------- #
class Commit(BaseModel):
    repo: str
    sha: str
    author: str
    message: str
    date: datetime
    url: str


class PullRequest(BaseModel):
    repo: str
    number: int
    title: str
    author: str
    state: str  # "open" | "closed"
    merged: bool
    created_at: datetime
    merged_at: datetime | None = None
    url: str
    body: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0


class RepoActivity(BaseModel):
    """All activity gathered for a single repo within a window."""

    repo: str
    pull_requests: list[PullRequest] = Field(default_factory=list)
    commits: list[Commit] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.pull_requests and not self.commits


# --------------------------------------------------------------------------- #
# Delivery feed
# --------------------------------------------------------------------------- #
class StoryStateUpdate(BaseModel):
    """Pending storyline memory to commit after a feed item is delivered."""

    initiative_key: str = Field(
        description="Initiative key whose story-state advances."
    )
    story_state: str = Field(description="Updated running summary for the initiative.")


class Bite(BaseModel):
    """One bite-sized, deliverable update — the atom of the feed.

    ``dedup_key`` is its stable identity: the same real-world fact always produces
    the same key, so re-running the delta engine never enqueues or delivers it
    twice (e.g. ``pr:api#12@merged``). ``subject`` is what the bite is *about*
    (``repo:api``, ``person:alice``) and is what the rolling-delta watermark
    tracks. ``content`` is the text to deliver.
    """

    dedup_key: str = Field(description="Stable identity, e.g. 'pr:api#12@merged'.")
    subject: str = Field(description="Rolling-delta subject, e.g. 'repo:api'.")
    kind: str = Field(description="Event kind, e.g. 'pr_merged' | 'pr_opened'.")
    content: str = Field(description="Human-readable text of the update.")
    story_state_update: StoryStateUpdate | None = Field(
        default=None,
        description="Story-state to commit only after this bite is delivered.",
    )


# --------------------------------------------------------------------------- #
# LLM outputs
# --------------------------------------------------------------------------- #
class ProjectSummary(BaseModel):
    """What's happening in one project/repo, in plain language."""

    project: str = Field(description="Repo or project name.")
    headline: str = Field(description="One-line summary of the current focus.")
    whats_happening: str = Field(
        description="2-4 sentences describing the work in progress and its intent."
    )
    notable_changes: list[str] = Field(
        default_factory=list,
        description="Bullet points of the most significant merged/ongoing changes.",
    )
    contributors: list[str] = Field(
        default_factory=list, description="People active in this project this period."
    )


class ActivityDigest(BaseModel):
    """Cross-project digest the user reads to know what's going on."""

    period: str = Field(description="Human-readable window, e.g. 'last 24 hours'.")
    overview: str = Field(
        description="A few sentences synthesizing org-wide activity and themes."
    )
    projects: list[ProjectSummary] = Field(default_factory=list)
