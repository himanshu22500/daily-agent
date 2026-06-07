"""Storyteller — orchestrate mapping → grouping → chapter rendering.

Ties the pieces together: resolve every PR to its initiative, group the PRs by
initiative, then for each initiative write a chapter (continuing its storyline
from the stored state, if any). Rendering is read-only here; persisting the new
story-state happens when the feed actually delivers a chapter (build 4), so this
is safe to run as a dry-run preview.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..agents.chapter_writer import Chapter, write_chapter
from ..agents.initiative_mapper import pr_key
from ..models import PullRequest
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
