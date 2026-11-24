"""Practice packs and talk-track notes, as interview prep cards (issue #135).

Two kinds of study material the maintainer already wrote become prep cards
through ``PrepItems.put`` (#130):

- **A DSA practice pack**: a folder with a ``README.md`` whose index table
  gives each problem's pattern and target complexity (plus a "do these first"
  line), ``problems/NN-*.md`` statements and ``answers/NN-*.md`` write-ups.
  Each problem becomes one ``dsa`` card. The statement is the prompt, and the
  answer reveals in steps: Pattern, Key idea, Brute force, Optimal solution,
  Walkthrough, Edge cases & follow-ups. The answer's own sections are mapped
  onto those steps by their headings and keep their content as written.
  Sections no rule knows ("Faster reads", "Quickselect") stay with the step
  before them.
- **Talk-track notes**: one markdown file becomes one card. The H1 is the
  title, the prompt is the interview question it answers, and each ``## ``
  section is one step, in order.

Headings inside a prompt or a step become bold lines, so only the steps built
here start with ``### `` and the feed's inline renderer shows them. Code fences
are kept as written; a fence left open is closed at the end of its step.

Tags on DSA cards come from ``DSA_PATTERNS``, the pattern vocabulary shared
with the agents that write new DSA problems and read weak spots from grades.
The pack's own tag (``blinkit``) and ``must-do`` name a source and a priority,
not a skill.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..feed.headlines import plain
from ..feed.prep_tracks import (
    DESIGN,
    PROBLEM_TYPE,
    TRACKS,
    PrepItem,
    Step,
    item_key,
    join_steps,
    slug,
    split_steps,
    types_for,
)

# Canonical DSA pattern slugs and their display names.
DSA_PATTERNS: dict[str, str] = {
    "arrays-hashing": "Arrays & hashing",
    "two-pointers": "Two pointers",
    "sliding-window": "Sliding window",
    "prefix-sum": "Prefix sum",
    "binary-search": "Binary search",
    "binary-search-on-answer": "Binary search on the answer",
    "stack": "Stack",
    "monotonic-stack": "Monotonic stack",
    "linked-list": "Linked list",
    "intervals": "Intervals",
    "heap": "Heap / priority queue",
    "greedy": "Greedy",
    "bfs": "BFS",
    "dfs": "DFS",
    "backtracking": "Backtracking",
    "topological-sort": "Topological sort",
    "union-find": "Union-Find",
    "shortest-path": "Shortest path",
    "trees": "Trees",
    "trie": "Trie",
    "dp": "Dynamic programming",
    "knapsack": "Knapsack",
    "bit-manipulation": "Bit manipulation",
    "ds-design": "Data structure design",
    "math": "Math",
}

MUST_DO = "must-do"

# Free-text pattern descriptions ("two heaps (Meeting Rooms III)") to slugs.
# A description may name several; every rule that matches adds its slugs.
_PATTERN_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = tuple(
    (re.compile(rx, re.IGNORECASE), slugs)
    for rx, slugs in (
        (r"hash|count(ing)? map|frequency|two[- ]sum|anagram", ("arrays-hashing",)),
        (r"two[- ]pointers?", ("two-pointers",)),
        (r"window", ("sliding-window",)),
        (r"prefix[- ]sums?|running sums?|cumulative sums?", ("prefix-sum",)),
        (r"binary search|bisect", ("binary-search",)),
        (
            r"binary search on (the )?(answer|time|result)|aggressive cows"
            r"|koko|capacity to ship|split array largest",
            ("binary-search-on-answer",),
        ),
        (r"monotonic (stack|queue|deque)|next (greater|smaller)", ("monotonic-stack",)),
        (r"(?<!monotonic )\bstacks?\b", ("stack",)),
        (r"linked[- ]list", ("linked-list",)),
        (r"interval|sweep[- ]line|meeting rooms", ("intervals",)),
        (r"heap|priority queue", ("heap",)),
        (r"greedy", ("greedy",)),
        (r"\bbfs\b|breadth[- ]first", ("bfs",)),
        (r"\bdfs\b|depth[- ]first|flood[- ]fill", ("dfs",)),
        (r"backtrack|permutations|n[- ]queens", ("backtracking",)),
        (r"topolog|kahn", ("topological-sort",)),
        (r"union[- ]find|disjoint[- ]set|\bdsu\b", ("union-find",)),
        (r"dijkstra|bellman|floyd|shortest[- ]path", ("shortest-path",)),
        (r"\btrees?\b|\bbst\b|fenwick|binary indexed", ("trees",)),
        (r"\btries?\b|prefix tree", ("trie",)),
        (
            r"\bdp\b|dynamic programming|memoi[sz]|\blis\b|\blcs\b|edit distance",
            ("dp",),
        ),
        (r"knapsack|coin change|subset[- ]sum", ("dp", "knapsack")),
        (r"\bbits?\b|bitmask|\bxor\b", ("bit-manipulation",)),
        (
            r"\blru\b|\blfu\b|hit counter|rate limiter|\bcache|expiry|\bttl\b"
            r"|\bdesign\b",
            ("ds-design",),
        ),
        (r"\bmath|\bgcd\b|\blcm\b|\bprimes?\b|modular|combinatoric", ("math",)),
    )
)
# A more specific slug replaces the general one it implies.
_SUBSUMES = {"binary-search-on-answer": "binary-search", "monotonic-stack": "stack"}


def pattern_slugs(text: str) -> list[str]:
    """``"binary search + greedy (aggressive cows)"`` ->
    ``["binary-search-on-answer", "greedy"]``, in ``DSA_PATTERNS`` order."""
    found = {s for rx, slugs in _PATTERN_RULES if rx.search(text) for s in slugs}
    found -= {_SUBSUMES[s] for s in found if s in _SUBSUMES}
    return [s for s in DSA_PATTERNS if s in found]


class PackError(ValueError):
    """The path isn't a practice pack or notes file this can read."""


# --- markdown outline ---------------------------------------------------------- #

_FENCE = "```"
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
_NUMBERED = re.compile(r"^\s*(?:\d+|[a-z])\s*[.)]\s+", re.IGNORECASE)
_NUMBER = re.compile(r"^\s*#?\s*(\d{1,3})\b")
_NUMBER_PREFIX = re.compile(r"^\s*#?\d{1,3}(?:\s*[—–\-.:)]\s*|\s+)")


def _is_fence(line: str) -> bool:
    # The same test as ``split_steps``, so both agree on what's code.
    return line.lstrip().startswith(_FENCE)


def _inline(text: str) -> str:
    """Heading or cell text without markdown: links keep their text."""
    return plain(_LINK.sub(r"\1", text))


@dataclass
class _Section:
    heading: str
    lines: list[str] = field(default_factory=list)


@dataclass
class _Outline:
    title: str  # the H1 ("" without one)
    preamble: list[str]  # lines between the H1 and the first section
    sections: list[_Section]


def _outline(text: str) -> _Outline:
    """A markdown file split at its top heading level under the H1 (``## ``
    usually, ``### `` in a file without ``## `` headings). Headings inside code
    fences don't count, and deeper headings stay inside their section."""
    lines = text.splitlines()
    marks: list[tuple[int, int, str]] = []  # (line, level, text)
    in_fence = False
    for i, line in enumerate(lines):
        if _is_fence(line):
            in_fence = not in_fence
        elif not in_fence and (match := _HEADING.match(line)):
            marks.append((i, len(match.group(1)), match.group(2)))
    title_at = next((i for i, level, _ in marks if level == 1), None)
    if title_at is not None and any(i < title_at for i, _, _ in marks):
        title_at = None  # an H1 after other headings is just a heading
    title = next((t for i, _, t in marks if i == title_at), "")
    levels = [level for i, level, _ in marks if i != title_at]
    top = min(levels, default=0)
    starts = {i: t for i, level, t in marks if level == top and i != title_at}
    preamble: list[str] = []
    sections: list[_Section] = []
    for i, line in enumerate(lines):
        if i == title_at:
            continue
        if i in starts:
            sections.append(_Section(starts[i]))
        elif sections:
            sections[-1].lines.append(line)
        else:
            preamble.append(line)
    return _Outline(_inline(title), preamble, sections)


def _body(lines: Iterable[str]) -> str:
    """Card text from markdown lines: headings outside code become bold lines
    (so no stray ``### `` starts a step), and an open fence is closed."""
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _is_fence(line):
            in_fence = not in_fence
        elif not in_fence and (match := _HEADING.match(line)):
            text = match.group(2).replace("**", "").strip()
            line = f"**{text}**" if text else ""
        out.append(line.rstrip())
    if in_fence:
        out.append(_FENCE)
    return "\n".join(out).strip()


def _norm(heading: str) -> str:
    return _NUMBERED.sub("", _inline(heading)).strip().lower()


# --- DSA packs ------------------------------------------------------------------ #

PATTERN_STEP = "Pattern"
KEY_IDEA_STEP = "Key idea"
BRUTE_STEP = "Brute force"
OPTIMAL_STEP = "Optimal solution"
WALKTHROUGH_STEP = "Walkthrough"
EDGES_STEP = "Edge cases & follow-ups"
DSA_STEPS = (
    PATTERN_STEP,
    KEY_IDEA_STEP,
    BRUTE_STEP,
    OPTIMAL_STEP,
    WALKTHROUGH_STEP,
    EDGES_STEP,
)

# Answer headings (lowercased, numbering dropped) to steps, by how they start.
_STEP_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (step, re.compile(rx))
    for step, rx in (
        (
            PATTERN_STEP,
            r"(pattern|standard problems?|related problems?|problem type|recogni)",
        ),
        (
            KEY_IDEA_STEP,
            r"(intuition|key idea|core idea|idea\b|insight|approach|observation"
            r"|translating|translation|how to think|the trick)",
        ),
        (BRUTE_STEP, r"(brute|naive|baseline)"),
        (
            OPTIMAL_STEP,
            r"(optimal|optimi[sz]ed|solution|efficient|better|implementation"
            r"|code\b|complexity|final)",
        ),
        (WALKTHROUGH_STEP, r"(walk[- ]?through|dry[- ]run|trace\b|worked example)"),
        (
            EDGES_STEP,
            r"(edge|corner|follow|common mistakes|mistakes|pitfalls|gotchas"
            r"|scaling|variants|variations|extensions|what if)",
        ),
    )
)
# A heading that says no more than its step needs no label inside the step.
_BARE = {
    PATTERN_STEP: {"pattern"},
    KEY_IDEA_STEP: {"intuition", "key idea", "idea"},
    BRUTE_STEP: {"brute force", "brute-force", "naive"},
    OPTIMAL_STEP: {"optimal", "optimal solution", "solution"},
    WALKTHROUGH_STEP: {"walkthrough", "walk-through", "walk through", "dry run"},
    EDGES_STEP: {"edge cases", "edge cases & follow-ups", "edge cases and follow-ups"},
}


def answer_step(heading: str) -> str | None:
    """The step an answer section belongs to (``None``: unknown heading)."""
    norm = _norm(heading)
    return next((step for step, rx in _STEP_RULES if rx.match(norm)), None)


@dataclass(frozen=True)
class IndexRow:
    """One row of a pack README's index table."""

    number: int | None
    name: str
    pattern: str
    target: str
    stem: str  # the problem file's stem, from the row's link ("" without one)


@dataclass
class PackIndex:
    title: str = ""  # the README's H1
    rows: list[IndexRow] = field(default_factory=list)
    must_do: set[int] = field(default_factory=set)

    @property
    def company(self) -> str:
        """``"Blinkit — Round 1 (...)"`` -> ``"Blinkit"`` ("" if unclear)."""
        head = re.split(r"\s+[—–-]\s+|:\s*", self.title, maxsplit=1)
        if len(head) == 2 and 0 < len(head[0].split()) <= 3:
            return head[0].strip()
        return ""


def _cells(line: str) -> list[str]:
    inner = line.strip().strip("|")
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", inner)]


def _column(header: list[str], *words: str) -> int | None:
    for i, cell in enumerate(header):
        text = _inline(cell).lower()
        if any(word in text for word in words):
            return i
    return None


_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
_PRIORITY = re.compile(r"\b(first|priority|must[- ]?do)\b", re.IGNORECASE)
_REF = re.compile(r"#(\d{1,3})\b")


def _priority(lines: list[str]) -> set[int]:
    """Problem numbers in the README's "do these first" line: the first
    paragraph under a heading that says so, or a line with ``#NN`` refs."""
    for i, line in enumerate(lines):
        if not _PRIORITY.search(line):
            continue
        if _HEADING.match(line):
            paragraph: list[str] = []
            for nxt in lines[i + 1 :]:
                if _HEADING.match(nxt) or (not nxt.strip() and paragraph):
                    break
                if nxt.strip():
                    paragraph.append(nxt)
            refs = _REF.findall(" ".join(paragraph))
        else:
            refs = _REF.findall(line)
        if refs:
            return {int(ref) for ref in refs}
    return set()


def parse_index(readme: str) -> PackIndex:
    """A pack README's title, index table (number, problem, pattern, target)
    and "do these first" problems. A table needs a pattern column."""
    lines = readme.splitlines()
    index = PackIndex(title=_outline(readme).title, must_do=_priority(lines))
    i = 0
    while i < len(lines) - 1:
        line, nxt = lines[i], lines[i + 1]
        if not (line.lstrip().startswith("|") and _SEPARATOR.match(nxt)):
            i += 1
            continue
        header = _cells(line)
        pattern = _column(header, "pattern")
        i += 2
        if pattern is None:
            continue
        number = next(
            (
                n
                for n, c in enumerate(header)
                if _inline(c).strip() in ("#", "no", "no.")
            ),
            None,
        )
        problem = _column(header, "problem", "name", "title")
        target = _column(header, "target", "complexity")
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            cells = _cells(lines[i])
            i += 1

            def cell(col: int | None, cells: list[str] = cells) -> str:
                return cells[col] if col is not None and col < len(cells) else ""

            links = _LINK.findall(cell(problem))
            path = next((p for _, p in links if "problem" in p.lower()), "")
            path = path or (links[0][1] if links else "")
            stem = Path(path).stem if path.endswith(".md") else ""
            raw = _NUMBER.match(cell(number)) or _NUMBER.match(stem)
            name = links[0][0] if links else cell(problem)
            index.rows.append(
                IndexRow(
                    number=int(raw.group(1)) if raw else None,
                    name=_inline(name),
                    pattern=_inline(cell(pattern)),
                    target=_inline(cell(target)),
                    stem=stem,
                )
            )
    return index


def _number(stem: str) -> int | None:
    match = re.match(r"(\d{1,3})(?=[-_. ]|$)", stem)
    return int(match.group(1)) if match else None


def problem_title(h1: str) -> str:
    """``"10 — Top-K Trending SKUs"`` -> ``"Top-K Trending SKUs"``."""
    title = _NUMBER_PREFIX.sub("", h1, count=1).strip()
    return re.sub(r":\s*answer$", "", title, flags=re.IGNORECASE).strip() or h1


def answer_steps(answer: str, row: IndexRow | None = None) -> list[Step]:
    """The steps a problem's answer reveals, in ``DSA_STEPS`` order. The text
    before the first section (pattern and related problems) is the Pattern
    step, with the index's target complexity added."""
    outline = _outline(answer)
    groups: dict[str, list[_Section]] = {step: [] for step in DSA_STEPS}
    current = KEY_IDEA_STEP
    for section in outline.sections:
        current = answer_step(section.heading) or current
        groups[current].append(section)

    bodies: dict[str, list[str]] = {step: [] for step in DSA_STEPS}
    preamble = _body(outline.preamble)
    if preamble:
        bodies[PATTERN_STEP].append(preamble)
    for step, sections in groups.items():
        for section in sections:
            text = _body(section.lines)
            label = _inline(section.heading).replace("**", "")
            bare = len(sections) == 1 and _norm(section.heading) in _BARE[step]
            if label and not bare:
                text = f"**{label}**\n\n{text}" if text else f"**{label}**"
            if text:
                bodies[step].append(text)
    if row and row.pattern and not bodies[PATTERN_STEP]:
        bodies[PATTERN_STEP].append(f"**Pattern:** {row.pattern}")
    if row and row.target:
        bodies[PATTERN_STEP].append(f"**Target complexity:** {row.target}")
    return [Step(step, "\n\n".join(parts)) for step, parts in bodies.items() if parts]


@dataclass
class Pack:
    """Cards read from a pack folder, and what looked off while reading."""

    title: str
    label: str  # default source label, e.g. "Blinkit DSA pack"
    tags: list[str]  # tags on every card (the pack's company)
    items: list[PrepItem] = field(default_factory=list)
    steps: dict[str, list[str]] = field(default_factory=dict)  # key -> titles
    must_do: int = 0
    warnings: list[str] = field(default_factory=list)


def pack_source(folder: str | Path) -> str:
    """Default source id for a pack folder: ``dsa-<folder name>``."""
    return "dsa-" + slug(Path(folder).expanduser().resolve().name)


def _inside(path: Path, folder: Path) -> bool:
    return path.resolve().is_relative_to(folder.resolve())


def _md_files(folder: Path, name: str) -> list[Path]:
    directory = folder / name
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob("*.md") if p.is_file() and _inside(p, folder)
    )


def _check(item: PrepItem, prompt: str, steps: list[Step]) -> str | None:
    """Make sure the card splits back into what it was built from."""
    if split_steps(item.text) != (prompt.strip(), steps):
        return f"{item.title}: its text doesn't split back into its steps"
    return None


def read_pack(folder: str | Path, source_id: str, *, tags: Iterable[str] = ()) -> Pack:
    """One ``dsa`` card per ``problems/*.md``, with steps from the matching
    ``answers/*.md`` (same file name, else the same number) and tags from the
    README index row (same file, else the same number)."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise PackError(f"not a folder: {root}")
    problems = _md_files(root, "problems")
    if not problems:
        raise PackError(f"no problems/*.md in {root}")
    readme = root / "README.md"
    index = parse_index(readme.read_text()) if readme.is_file() else PackIndex()
    company = index.company
    pack = Pack(
        title=index.title or root.name,
        label=f"{company} DSA pack" if company else index.title or root.name,
        tags=[t for t in [slug(company), *(slug(t) for t in tags)] if t],
    )
    if not readme.is_file():
        pack.warnings.append("no README.md: no index, so no pattern tags from it")

    answers = _md_files(root, "answers")
    answer_by_stem = {p.stem: p for p in answers}
    answer_by_number: dict[int, Path] = {}
    for path in answers:
        if (n := _number(path.stem)) is not None:
            answer_by_number.setdefault(n, path)
    row_by_stem = {r.stem: r for r in index.rows if r.stem}
    row_by_number = {r.number: r for r in index.rows if r.number is not None}
    used_answers: set[Path] = set()
    used_rows: set[int] = set()

    for path in problems:
        number = _number(path.stem)
        row = row_by_stem.get(path.stem) or row_by_number.get(number)
        answer = answer_by_stem.get(path.stem) or answer_by_number.get(number)
        if row:
            used_rows.add(id(row))
        statement = _outline(path.read_text())
        title = problem_title(statement.title) if statement.title else ""
        title = title or (row.name if row else "") or path.stem
        lines = list(statement.preamble)
        for section in statement.sections:
            lines += [f"## {section.heading}", *section.lines]
        prompt = _body(lines)
        if not prompt:
            pack.warnings.append(f"{path.name}: no problem statement")
        steps: list[Step] = []
        answer_pattern = ""
        if answer is None:
            pack.warnings.append(f"{path.name}: no answer file")
        else:
            used_answers.add(answer)
            answer_text = answer.read_text()
            steps = answer_steps(answer_text, row)
            found = re.search(r"\*\*Pattern:\*\*(.*)", answer_text)
            answer_pattern = found.group(1) if found else ""
        patterns = pattern_slugs(row.pattern if row and row.pattern else "")
        if not patterns:
            patterns = pattern_slugs(answer_pattern)
        if not patterns:
            described = row.pattern if row else answer_pattern.strip()
            pack.warnings.append(
                f"{path.name}: no canonical pattern for {described or '(none)'!r}"
            )
        priority = number is not None and number in index.must_do
        pack.must_do += priority
        item = PrepItem(
            key=item_key(source_id, path.stem),
            type=PROBLEM_TYPE,
            title=title,
            text=join_steps(prompt, steps),
            tags=[*patterns, *pack.tags, *([MUST_DO] if priority else [])],
        )
        if mismatch := _check(item, prompt, steps):
            pack.warnings.append(mismatch)
        pack.items.append(item)
        pack.steps[item.key] = [s.title for s in steps]

    for path in answers:
        if path not in used_answers:
            pack.warnings.append(f"answers/{path.name}: no matching problem")
    for row in index.rows:
        if id(row) not in used_rows:
            pack.warnings.append(f"index row {row.number or row.name}: no problem file")
    return pack


# --- talk-track notes ----------------------------------------------------------- #

NOTES_LABEL = "Interview notes"
_TALK_TRACK = re.compile(
    r"^(?:an?\s+|my\s+)?(?:interview\s+)?"
    r"(?:talk[- ]?track|notes?|cheat[- ]?sheet|crib[- ]?sheet|study notes|prep)$",
    re.IGNORECASE,
)
_TALK_TRACK_END = re.compile(
    r"\s+(?:interview\s+)?(?:talk[- ]?track|notes|cheat[- ]?sheet)$", re.IGNORECASE
)
_ASKS = re.compile(r"(design|build|implement|how|what|why|explain|walk)\b", re.I)
_ARTICLE = re.compile(r"(a|an|the)\s", re.IGNORECASE)


def notes_topic(title: str) -> str:
    """``"Rate limiter: interview talk track"`` -> ``"Rate limiter"``."""
    for separator in (":", " — ", " – ", " - "):
        head, found, tail = title.partition(separator)
        if found and head.strip() and _TALK_TRACK.match(tail.strip()):
            return head.strip()
    return _TALK_TRACK_END.sub("", title).strip() or title.strip()


def interview_question(topic: str) -> str:
    """``"Rate limiter"`` -> ``"Design a rate limiter. Talk me through it."``"""
    topic = topic.strip().rstrip(".")
    if not topic or topic.endswith("?"):
        return topic
    if _ASKS.match(topic):
        return f"{topic[0].upper()}{topic[1:]}. Talk me through it."
    first = topic.split()[0]
    acronym = first[:2].isupper()
    noun = topic if acronym else topic[0].lower() + topic[1:]
    if _ARTICLE.match(noun):
        return f"Design {noun}. Talk me through it."
    if acronym:  # "an API gateway", "a URL shortener"
        article = "an" if first[0] in "AEFHILMNORSX" else "a"
    else:
        article = "an" if noun[0] in "aeiou" else "a"
    return f"Design {article} {noun}. Talk me through it."


def notes_source(path: str | Path) -> str:
    """Default source id for a notes file: ``rate_limiter_notes.md`` ->
    ``notes-rate-limiter``."""
    name = slug(Path(path).stem)
    return "notes-" + (re.sub(r"-?notes$", "", name) or name)


@dataclass
class Notes:
    item: PrepItem
    question: str
    steps: list[str]  # step titles, in order


def parse_notes(
    text: str,
    source_id: str,
    *,
    name: str,
    track: str = DESIGN,
    question: str | None = None,
    tags: Iterable[str] = (),
) -> Notes:
    """One card from a talk-track file: the H1 is its title, the interview
    question its prompt, and each top-level section a step (any text before
    the first section is an "Overview" step)."""
    if track not in TRACKS:
        raise PackError(f"unknown track {track!r} (one of: {', '.join(TRACKS)})")
    outline = _outline(text)
    title = outline.title or name.replace("_", " ").replace("-", " ").capitalize()
    topic = notes_topic(title)
    asked = (question or "").strip() or interview_question(topic)
    steps: list[Step] = []
    if overview := _body(outline.preamble):
        steps.append(Step("Overview", overview))
    for section in outline.sections:
        heading = _NUMBERED.sub("", _inline(section.heading)).strip()
        steps.append(Step(heading or "Notes", _body(section.lines)))
    if not steps:
        raise PackError(f"{name}: no sections to reveal")
    item = PrepItem(
        key=item_key(source_id, name),
        type=types_for(track)[0],
        title=title,
        text=join_steps(asked, steps),
        tags=[*tags, slug(topic)],
    )
    if split_steps(item.text) != (asked, steps):
        raise PackError(f"{name}: its text doesn't split back into its steps")
    return Notes(item=item, question=asked, steps=[s.title for s in steps])


def read_notes(
    path: str | Path,
    source_id: str,
    *,
    track: str = DESIGN,
    question: str | None = None,
    tags: Iterable[str] = (),
) -> Notes:
    file = Path(path).expanduser()
    if not file.is_file():
        raise PackError(f"not a file: {file}")
    return parse_notes(
        file.read_text(),
        source_id,
        name=file.stem,
        track=track,
        question=question,
        tags=tags,
    )
