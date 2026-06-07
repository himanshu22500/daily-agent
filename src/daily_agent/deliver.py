"""Render a daily digest to Markdown and deliver it (currently: a dated file).

Rendering once to Markdown keeps delivery backends simple — a file today, a
Slack webhook or email later all consume the same string.
"""

from __future__ import annotations

from pathlib import Path

from .agents.person_brief import PersonBrief
from .models import ActivityDigest
from .team import TeamMember


def render_markdown(
    date_str: str,
    digest: ActivityDigest,
    briefs: list[tuple[TeamMember, PersonBrief]],
) -> str:
    out: list[str] = [
        f"# Daily digest — {date_str}",
        "",
        f"_Window: {digest.period}_",
        "",
    ]

    out += ["## Overview", "", digest.overview, ""]

    if digest.projects:
        out.append("## Projects")
        out.append("")
        for p in digest.projects:
            out += [
                f"### {p.project}",
                "",
                f"**{p.headline}**",
                "",
                p.whats_happening,
                "",
            ]
            if p.notable_changes:
                out += [f"- {c}" for c in p.notable_changes] + [""]
            if p.contributors:
                out += [f"_Contributors: {', '.join(p.contributors)}_", ""]

    if briefs:
        out += ["## People", ""]
        for member, pb in briefs:
            out += [f"### {member.name}", "", f"**{pb.headline}**", "", pb.summary, ""]
            if pb.themes:
                out += [f"- {t}" for t in pb.themes] + [""]

    return "\n".join(out).rstrip() + "\n"


def write_file(content: str, digest_dir: str | Path, date_str: str) -> Path:
    d = Path(digest_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{date_str}.md"
    path.write_text(content)
    return path
