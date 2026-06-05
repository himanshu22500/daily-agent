"""Team identity mapping: canonical name <-> Huly display name <-> GitHub login.

The mapping lives in a local ``team.json`` (gitignored — it's PII). It powers
person-centric queries: ``brief "Harshit"`` and ``tasks --assignee me``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TeamMember:
    name: str  # canonical display name
    huly: str  # Huly display name (for assignee filtering)
    github: str  # GitHub login (for PR authorship)


def load_team(path: str | Path) -> dict[str, TeamMember]:
    """Load the team map. Returns {} if the file is missing."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    team: dict[str, TeamMember] = {}
    for name, entry in raw.items():
        if name.startswith("_") or not isinstance(entry, dict):
            continue  # skip _comment etc.
        team[name] = TeamMember(
            name=name, huly=entry.get("huly", name), github=entry.get("github", "")
        )
    return team


def resolve_member(
    team: dict[str, TeamMember], query: str, *, me: str = ""
) -> TeamMember | None:
    """Resolve a free-form name/handle to a TeamMember.

    ``me``/``mine`` resolves via the configured ``me`` identity. Matching is
    case-insensitive: first an exact hit on canonical name / Huly name / GitHub
    login, then a substring match on the canonical or Huly name.
    """
    q = query.strip().lower()
    if q in ("me", "mine"):
        if not me:
            return None
        q = me.strip().lower()
    for m in team.values():
        if q in (m.name.lower(), m.huly.lower(), m.github.lower()):
            return m
    for m in team.values():
        if q in m.name.lower() or q in m.huly.lower():
            return m
    return None
