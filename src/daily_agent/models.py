"""Domain models for personal insight capture and delivery."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Bite(BaseModel):
    """One durable, deduplicated item ready for delivery."""

    dedup_key: str
    subject: str
    kind: str
    content: str


class Insight(BaseModel):
    """A durable learning captured from a coding-agent transcript."""

    key: str
    text: str
    type: str = "general"
    tags: list[str] = Field(default_factory=list)
    score: float = 0.0
    source_session: str = ""
    git_branch: str = ""
    captured_at: datetime
    status: str = "new"
    # When the insight entered the store (≈ when it was published to the web
    # feed). ``captured_at`` is the transcript time, which can be weeks older
    # when a flush catches up on sessions. ``None`` for rows stored before this
    # was tracked; ``InsightStore.add`` stamps it when unset.
    stored_at: datetime | None = None
    # Short newspaper-style headline for the web feed; "" until one is written.
    title: str = ""


class InsightCandidate(BaseModel):
    """Structured LLM candidate before persistence."""

    text: str
    canonical_key: str
    type: str = "general"
    tags: list[str] = Field(default_factory=list)
    score: float = Field(default=0.0, ge=0.0, le=1.0)


class PrepStep(BaseModel):
    title: str
    body: str


class PrepCard(BaseModel):
    """An interview practice card Claude wrote from a video or a study session
    (#139). Lenient on purpose: ``feed.practice_cards.clean_cards`` validates."""

    track: str  # "dsa" | "design"
    question: str
    answer: str = ""
    why_it_matters: str = ""
    steps: list[PrepStep] = Field(default_factory=list)  # a design walkthrough
    tags: list[str] = Field(default_factory=list)


class InsightExtraction(BaseModel):
    candidates: list[InsightCandidate] = Field(default_factory=list)
    # A session spent studying for interviews yields practice cards instead of
    # coding insights (#139).
    study_session: bool = False
    prep_cards: list[PrepCard] = Field(default_factory=list)


class VideoStory(BaseModel):
    """A feed story Claude wrote from a followed source: a YouTube video (#99)
    or an Instagram post (#107)."""

    headline: str
    text: str
    tags: list[str] = Field(default_factory=list)
    # Practice cards from an interview-prep video (#139).
    cards: list[PrepCard] = Field(default_factory=list)
