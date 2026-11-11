"""Human-friendly project names for insights: the feed's "publisher" (issue #82).

Claude Code stores each project's transcripts in a directory named after the
project path with separators replaced by ``-``
(``/Users/me/work/tranzact-v2`` -> ``-Users-me-work-tranzact-v2``). That is
lossy (``tranzact-v2`` vs ``tranzact/v2``), so the name is resolved by walking
the real filesystem along the matching prefix. If the directory no longer
exists, a heuristic keeps the last two parts.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

_SEPARATORS = re.compile(r"[^A-Za-z0-9]")
_MAX_DEPTH = 16


def mangle(name: str) -> str:
    """Claude Code's transform for a path or path component."""
    return _SEPARATORS.sub("-", name)


def resolve_project_dir(mangled: str, root: Path) -> Path | None:
    """The real directory under ``root`` whose mangled path is ``mangled``."""
    prefix = mangle(str(root)).rstrip("-")  # "/" -> ""
    if not mangled.startswith(prefix):
        return None
    target = mangled[len(prefix) :].removeprefix("-")

    def walk(base: Path, rest: str, depth: int) -> Path | None:
        if not rest:
            return base
        if depth > _MAX_DEPTH:
            return None
        try:
            children = [c for c in base.iterdir() if c.is_dir()]
        except OSError:
            return None
        # Longest names first, so "tranzact-v2" wins over "tranzact".
        for child in sorted(children, key=lambda c: len(c.name), reverse=True):
            piece = mangle(child.name)
            if rest == piece:
                return child
            if rest.startswith(piece + "-"):
                found = walk(child, rest[len(piece) + 1 :], depth + 1)
                if found is not None:
                    return found
        return None

    return walk(root, target, 0)


@lru_cache(maxsize=512)
def project_label(mangled: str, root: str = "/", home: str | None = None) -> str:
    """Short display name for a transcript directory name."""
    home_path = Path(home) if home else Path.home()
    resolved = resolve_project_dir(mangled, Path(root))
    if resolved is not None:
        return "home" if resolved == home_path else resolved.name
    rest = mangled.removeprefix(mangle(str(home_path))).strip("-")
    if not rest:
        return "home"
    parts = [p for p in rest.split("-") if p]
    return "-".join(parts[-2:])


def session_projects(
    transcript_paths: Iterable[str], *, root: str = "/", home: str | None = None
) -> dict[str, str]:
    """``{session id: project label}`` from transcript file paths."""
    out: dict[str, str] = {}
    for raw in transcript_paths:
        path = Path(raw)
        if path.suffix == ".jsonl":
            out[path.stem] = project_label(path.parent.name, root, home)
    return out
