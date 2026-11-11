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


class InsightCandidate(BaseModel):
    """Structured LLM candidate before persistence."""

    text: str
    canonical_key: str
    type: str = "general"
    tags: list[str] = Field(default_factory=list)
    score: float = Field(default=0.0, ge=0.0, le=1.0)


class InsightExtraction(BaseModel):
    candidates: list[InsightCandidate] = Field(default_factory=list)
