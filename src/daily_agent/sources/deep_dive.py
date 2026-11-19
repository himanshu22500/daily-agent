"""Interview-prep notes for a resume, as feed material (issue #108).

Reads a "resume deep dive" folder: a ``README.md`` whose progress tracker
lists one markdown file per resume point, grouped by job, and the point files
themselves, each in the same numbered sections. Two sections are used:

- ``## 4. Likely interview questions``: ``**Q<n>. question**`` followed by
  ``- **Answer:** ...``, under ``###`` level headings (surface, deep dive,
  curveballs). These become flashcards.
- ``## 5. Concepts to master``: one bullet per concept. These become
  explainers that Claude writes.

Answers still marked ``⬜ OPEN`` have no answer yet. A partial one keeps its
written part plus a "still open" note, and one with nothing written is
skipped. Bullets marked "Reference design — confirm" are unconfirmed drafts,
so they're left out. Only files listed in the tracker, inside the folder, are
read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..feed.headlines import plain

_GROUP_ROW = re.compile(
    r"^\|\s*\*\*(?P<name>[^*|]+)\*\*\s*(?P<details>\([^|]*\))?\s*\|"
)
_POINT_ROW = re.compile(
    r"^\|\s*(?P<id>[A-Z]\d{1,2})\s*\|\s*(?P<name>[^|]+?)\s*\|"
    r"\s*\[[^\]]*\]\((?P<path>[^)\s]+\.md)\)"
)
_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_RESUME_LINE = re.compile(r"^>\s*\*\*Resume line[^*]*:\*\*\s*(.+)$", re.MULTILINE)
_SECTION = re.compile(r"^##\s+(\d+)\.\s*(.*)$")
# A trailing note may follow the question: ``**Q12. ...** _(adjacent fundamentals)_``.
_QUESTION = re.compile(r"^\*\*Q(\d+)\.\s+(.+?)\*\*(?:\s+_\([^)]*\)_)?\s*$")
# ``*worst*``-style emphasis inside a question (a card headline is plain text).
# A ``*`` touching a word character (``QA_*``, ``2*n``) is left alone.
_EMPHASIS = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_ANSWER = re.compile(r"^-\s+\*\*Answer:\*\*\s*(.*)$")
_OPEN = re.compile(r"⬜\s*OPEN(?:\s*\(partial\))?\.?\s*[—–-]?\s*")
_DISCUSS = re.compile(r"_Discuss:\s*(.+?)_(?=\s|$)")
_DRAFT = re.compile(r"^-\s+_Reference design", re.IGNORECASE)
# What's left of an OPEN line once the marker and the "Discuss" part are gone:
# shorter than this, it only names what's open ("impact and status.").
_LABEL_ONLY = 60


class DeepDiveError(ValueError):
    """The folder isn't a resume deep dive this can read."""


@dataclass(frozen=True)
class Point:
    id: str  # "T5"
    group: str  # the job, e.g. "TranZact"
    name: str  # the tracker's short name
    title: str  # the file's own heading
    resume_line: str
    path: str  # relative to the folder
    notes: str  # the whole file

    @property
    def label(self) -> str:
        return f"{self.id} · {self.name}"


@dataclass(frozen=True)
class Flashcard:
    point_id: str
    number: int
    level: str  # "surface", "deep-dive", "curveball", or another slug
    question: str
    answer: str  # the inline-markdown subset the feed renders
    still_open: str = ""


@dataclass(frozen=True)
class Concept:
    point_id: str
    number: int
    text: str


@dataclass
class DeepDive:
    points: list[Point] = field(default_factory=list)
    cards: list[Flashcard] = field(default_factory=list)
    concepts: list[Concept] = field(default_factory=list)
    unanswered: int = 0  # questions skipped: nothing written yet


@dataclass(frozen=True)
class TrackerRow:
    id: str
    name: str
    path: str
    group: str
    job: bool  # under a job heading (``**Name** (role, dates)``)


def tracker_rows(readme: str) -> list[TrackerRow]:
    """The point rows of the README's progress tracker, with their group."""
    rows: list[TrackerRow] = []
    group, job = "", False
    for line in readme.splitlines():
        group_match = _GROUP_ROW.match(line)
        if group_match and not _POINT_ROW.match(line):
            group = group_match.group("name").strip()
            job = bool(group_match.group("details"))
            continue
        point = _POINT_ROW.match(line)
        if point:
            rows.append(
                TrackerRow(
                    id=point.group("id"),
                    name=plain(point.group("name")),
                    path=point.group("path"),
                    group=group,
                    job=job,
                )
            )
    return rows


def _level(heading: str) -> str:
    text = heading.lower()
    if "surface" in text:
        return "surface"
    if "deep" in text:
        return "deep-dive"
    if "curveball" in text or "probing" in text:
        return "curveball"
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "general"


def _sections(text: str) -> dict[int, list[str]]:
    sections: dict[int, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        match = _SECTION.match(line)
        if match:
            current = sections.setdefault(int(match.group(1)), [])
            continue
        if current is not None:
            current.append(line)
    return sections


def _answer(lines: list[str]) -> tuple[str, str]:
    """``(answer, still open)`` from the lines under one question."""
    paragraphs: list[list[str]] = []
    open_notes: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or _DRAFT.match(line.strip()):
            continue
        answer = _ANSWER.match(line.strip())
        if answer:
            body = answer.group(1).strip()
            if _OPEN.match(body):
                body = _OPEN.sub("", body, count=1)
                notes = [m.group(1).strip() for m in _DISCUSS.finditer(body)]
                rest = _DISCUSS.sub("", body).strip(" —–-.;")
                if rest and len(rest) < _LABEL_ONLY:
                    notes.insert(0, rest)
                elif rest:
                    paragraphs.append([rest])
                open_notes.extend(n.rstrip(".") for n in notes if n)
            elif body:
                paragraphs.append([body])
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            item = stripped[2:].strip()
            if _OPEN.match(item):
                item = _OPEN.sub("", item, count=1)
                open_notes.extend(
                    m.group(1).strip().rstrip(".") for m in _DISCUSS.finditer(item)
                )
                continue
            line_text = f"• {item}"
        else:
            line_text = stripped
        if paragraphs:
            paragraphs[-1].append(line_text)
        else:
            paragraphs.append([line_text])
    answer_text = "\n\n".join("\n".join(p) for p in paragraphs if p).strip()
    still_open = "; ".join(dict.fromkeys(n for n in open_notes if n))
    return answer_text, still_open


def parse_point(text: str, point_id: str) -> tuple[list[Flashcard], list[Concept], int]:
    """A point file's flashcards, concepts, and how many questions had no answer."""
    sections = _sections(text)
    cards: list[Flashcard] = []
    unanswered = 0
    level = "general"
    question: tuple[int, str] | None = None
    body: list[str] = []

    def close() -> None:
        nonlocal unanswered
        if question is None:
            return
        answer, still_open = _answer(body)
        if not answer:
            unanswered += 1
            return
        cards.append(
            Flashcard(
                point_id=point_id,
                number=question[0],
                level=level,
                question=plain(_EMPHASIS.sub(r"\1", question[1])),
                answer=answer,
                still_open=still_open,
            )
        )

    for line in sections.get(4, []):
        if line.startswith("### "):
            close()
            question, body = None, []
            level = _level(line[4:])
            continue
        match = _QUESTION.match(line.strip())
        if match:
            close()
            question, body = (int(match.group(1)), match.group(2)), []
            continue
        if question is not None:
            body.append(line)
    close()

    concepts: list[Concept] = []
    for line in sections.get(5, []):
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:].strip():
            concepts.append(
                Concept(
                    point_id=point_id,
                    number=len(concepts) + 1,
                    text=stripped[2:].strip(),
                )
            )
    return cards, concepts, unanswered


def read_deep_dive(root: str | Path, *, jobs_only: bool = True) -> DeepDive:
    """Every point the tracker lists (only those under jobs, by default)."""
    folder = Path(root).expanduser().resolve()
    readme = folder / "README.md"
    if not readme.is_file():
        raise DeepDiveError(f"{folder} has no README.md with a progress tracker")
    rows = tracker_rows(readme.read_text(encoding="utf-8"))
    if not rows:
        raise DeepDiveError(f"{readme} has no progress tracker rows")
    deep = DeepDive()
    for row in rows:
        if jobs_only and not row.job:
            continue
        path = (folder / row.path).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        title = _TITLE.search(text)
        resume_line = _RESUME_LINE.search(text)
        deep.points.append(
            Point(
                id=row.id,
                group=row.group,
                name=row.name,
                title=plain(title.group(1)) if title else row.name,
                resume_line=plain(resume_line.group(1)) if resume_line else "",
                path=str(path.relative_to(folder)),
                notes=text,
            )
        )
        cards, concepts, unanswered = parse_point(text, row.id)
        deep.cards.extend(cards)
        deep.concepts.extend(concepts)
        deep.unanswered += unanswered
    return deep
