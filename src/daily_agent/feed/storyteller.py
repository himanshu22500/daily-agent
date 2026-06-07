"""Storyteller — orchestrate mapping → grouping → chapter rendering.

Ties the pieces together: resolve every PR to its initiative, group the PRs by
initiative, then for each initiative write a chapter (continuing its storyline
from the stored state, if any). Rendering is read-only here; persisting the new
story-state happens when the feed actually delivers a chapter (build 4), so this
is safe to run as a dry-run preview.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from ..agents.chapter_writer import Chapter, write_chapter
from ..agents.initiative_mapper import pr_key
from ..models import Bite, PullRequest
from .initiative import Initiative
from .initiatives_store import InitiativeStore
from .mapping import resolve_initiatives


@dataclass
class RenderedChapter:
    initiative: Initiative
    chapter: Chapter
    prs: list[PullRequest]

    @property
    def merged(self) -> int:
        return sum(1 for p in self.prs if p.merged)

    @property
    def opened(self) -> int:
        return sum(1 for p in self.prs if not p.merged)


def group_by_initiative(
    prs: list[PullRequest], mapping: dict[str, Initiative]
) -> list[tuple[Initiative, list[PullRequest]]]:
    """Group PRs by their resolved initiative, most-active first."""
    groups: dict[str, list[PullRequest]] = {}
    inits: dict[str, Initiative] = {}
    for pr in prs:
        init = mapping[pr_key(pr)]
        groups.setdefault(init.key, []).append(pr)
        inits[init.key] = init
    ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
    return [(inits[key], prs_) for key, prs_ in ordered]


async def render_chapters(
    model: str,
    prs: list[PullRequest],
    issues: list[dict],
    *,
    store: InitiativeStore | None = None,
    limit: int | None = None,
) -> list[RenderedChapter]:
    """Map PRs → initiatives, group, and render a chapter per initiative.

    Read-only w.r.t. story-state (uses prior state if a store is given, but does
    not persist) — safe for preview.
    """
    mapping = await resolve_initiatives(model, prs, issues)
    grouped = group_by_initiative(prs, mapping)
    if limit is not None:
        grouped = grouped[:limit]

    rendered: list[RenderedChapter] = []
    for init, group in grouped:
        prior = None
        if store and (state := store.get(init.key)):
            prior = state.story_state
        chapter = await write_chapter(model, title=init.title, prior_state=prior, prs=group)
        rendered.append(RenderedChapter(initiative=init, chapter=chapter, prs=group))
    return rendered


# --------------------------------------------------------------------------- #
# Live feed: incremental, story-state-persisting chapters → outbox bites
# --------------------------------------------------------------------------- #
def _activity_ts(pr: PullRequest) -> datetime:
    """When a PR became news: when it merged, else when it opened."""
    return pr.merged_at or pr.created_at


def _new_since(prs: list[PullRequest], since: datetime | None) -> list[PullRequest]:
    if since is None:
        return list(prs)
    return [pr for pr in prs if _activity_ts(pr) > since]


def _chapter_dedup_key(initiative_key: str, new_prs: list[PullRequest]) -> str:
    """Stable key for a chapter covering this exact set of new PRs.

    Same new-PR set → same key (re-runs collapse in the outbox); one more PR →
    a new key → a new chapter. Keeps generation + delivery idempotent.
    """
    ident = "|".join(sorted(f"{p.repo}#{p.number}:{int(p.merged)}" for p in new_prs))
    digest = hashlib.sha1(ident.encode()).hexdigest()[:12]
    return f"chapter:{initiative_key}:{digest}"


def _format_chapter(title: str, chapter: Chapter, new_prs: list[PullRequest]) -> str:
    merged = sum(1 for p in new_prs if p.merged)
    opened = len(new_prs) - merged
    footer = f"{merged} merged" + (f" · {opened} in flight" if opened else "")
    return f"📦 {title}\n\n{chapter.chapter}\n\n{footer}"


async def chapters_to_bites(
    model: str, prs: list[PullRequest], issues: list[dict], store: InitiativeStore
) -> list[Bite]:
    """Build deliverable chapter bites, advancing each initiative's storyline.

    For each initiative, narrate only the PRs that are *new since its last
    chapter*, then persist the updated story-state so the next run continues
    rather than repeats. Story-state is advanced at build time (the outbox is
    at-least-once); the rare dead-letter case is a known tradeoff to revisit.
    """
    mapping = await resolve_initiatives(model, prs, issues)
    grouped = group_by_initiative(prs, mapping)

    bites: list[Bite] = []
    for init, group in grouped:
        state = store.get(init.key)
        store.upsert(init.key, init.lane, init.title)
        since = (
            datetime.fromisoformat(state.last_narrated_at)
            if state and state.last_narrated_at
            else None
        )
        new_prs = _new_since(group, since)
        if not new_prs:
            continue
        prior = state.story_state if state else None
        chapter = await write_chapter(model, title=init.title, prior_state=prior, prs=new_prs)
        bites.append(
            Bite(
                dedup_key=_chapter_dedup_key(init.key, new_prs),
                subject=init.subject,
                kind="chapter",
                content=_format_chapter(init.title, chapter, new_prs),
            )
        )
        store.record_chapter(init.key, chapter.story_state)
    return bites
