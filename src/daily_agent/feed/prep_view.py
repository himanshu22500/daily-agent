"""How a prep card reads in the Interview tab (#137).

Pure helpers over a card's type, tags and text (``prep_tracks`` holds the
storage): the small kicker line on a card ("DSA · Sliding window"), the
button that reveals each step ("Show key idea"), and a ``drill``'s options.

A drill's text follows one format, which the daily DSA writer also follows:
a 2-4 line scenario and the question, then four option lines that start
with ``A) ``, ``B) ``, ``C) `` and ``D) ``. The first step is ``### Answer``;
its body's first line starts with the right letter and ``) `` (``B) Monotonic
stack``), then the explanation. ``parse_drill`` returns ``None`` for anything
else, and the reader falls back to plain steps.

``web_feed`` imports this module, so it must not import ``web_feed`` or
``prep_grades``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..sources.practice_pack import DSA_PATTERNS, MUST_DO
from .prep_tracks import (
    CARD_TYPE,
    DESIGN_TYPE,
    DRILL_TYPE,
    KEY_PREFIX,
    PROBLEM_TYPE,
    Step,
    slug,
    split_steps,
)

# Cards whose text reveals step by step, and cards the reader grades.
STEP_TYPES: tuple[str, ...] = (PROBLEM_TYPE, DRILL_TYPE, DESIGN_TYPE)
GRADED_TYPES: tuple[str, ...] = (CARD_TYPE, *STEP_TYPES)

_KICKERS = {
    PROBLEM_TYPE: "DSA",
    DRILL_TYPE: "Pattern drill",
    DESIGN_TYPE: "System design",
}
# Tags that name a type, track or priority rather than a topic.
_NOT_TOPICS = {
    "dsa",
    "drill",
    "drills",
    "design",
    "system-design",
    "interview",
    "prep",
    MUST_DO,
}
_UPPER = {
    "acid", "api", "aws", "bfs", "bst", "cap", "cdn", "cpu", "cqrs", "crdt",
    "dag", "db", "dfs", "dns", "dp", "ec2", "etl", "gcp", "gpu", "http",
    "https", "id", "ids", "io", "json", "jwt", "k", "kv", "lfu", "lru", "lsm",
    "mst", "olap", "oltp", "os", "p2p", "qps", "rest", "rpc", "rps", "s3",
    "sla", "slo", "sns", "sql", "sqs", "sse", "tcp", "ttl", "udp", "ui",
    "url", "uuid", "xml",
}  # fmt: skip
_CASED = {
    "dynamodb": "DynamoDB",
    "graphql": "GraphQL",
    "grpc": "gRPC",
    "kafka": "Kafka",
    "mongodb": "MongoDB",
    "mysql": "MySQL",
    "nosql": "NoSQL",
    "oauth": "OAuth",
    "postgres": "Postgres",
    "postgresql": "PostgreSQL",
    "redis": "Redis",
    "websocket": "WebSocket",
    "websockets": "WebSockets",
}


def tag_label(tag: str) -> str:
    """``"sliding-window"`` -> ``"Sliding window"``, ``"lru-cache"`` ->
    ``"LRU cache"``: a tag slug for display. DSA patterns use the shared
    names (``"heap"`` -> ``"Heap / priority queue"``)."""
    if tag in DSA_PATTERNS:
        return DSA_PATTERNS[tag]
    words = [w for w in re.split(r"[-_\s]+", (tag or "").lower()) if w]
    shown = []
    for n, word in enumerate(words):
        if word in _UPPER:
            shown.append(word.upper())
        elif word in _CASED:
            shown.append(_CASED[word])
        else:
            shown.append(word.capitalize() if n == 0 else word)
    return " ".join(shown)


def _source_words(key: str, source: str) -> set[str]:
    """Words naming where a card came from: its source label ("Blinkit DSA
    pack") and the source id in its key (``prep:dsa-blinkit-r1:...``)."""
    words = set(slug(source).split("-"))
    rest = key[len(KEY_PREFIX) :] if key.startswith(KEY_PREFIX) else ""
    source_id, sep, _ = rest.partition(":")
    if sep:
        words.update(source_id.split("-"))
    return words - {""}


def topic_tag(key: str, tags: list[str], source: str = "") -> str:
    """The card's first tag that names a topic, not its source or type."""
    named = _source_words(key, source)
    for tag in tags:
        if tag in _NOT_TOPICS or set(tag.split("-")) <= named:
            continue
        return tag
    return ""


def kicker(type_: str, key: str, tags: list[str], source: str = "") -> str:
    """The line above a step card's headline: "DSA · Sliding window",
    "Pattern drill", "System design · Caching" ("" for other cards).

    A drill never shows its tag: that would give the answer away.
    """
    kind = _KICKERS.get(type_, "")
    if not kind or type_ == DRILL_TYPE:
        return kind
    topic = topic_tag(key, tags, source)
    return f"{kind} · {tag_label(topic)}" if topic else kind


def reveal_label(title: str) -> str:
    """The button for a step: ``"Key idea"`` -> ``"Show key idea"``.
    Acronyms and symbols keep their case (``"BFS order"``, ``"O(n) pass"``)."""
    first, sep, rest = title.strip().partition(" ")
    if len(first) > 1 and first[0].isupper() and first[1].islower():
        first = first[0].lower() + first[1:]
    return f"Show {first}{sep}{rest}"


# --- drills ------------------------------------------------------------------ #

_OPTION = re.compile(r"^([A-D])\) (\S.*)$")
_ANSWER = re.compile(r"^(?:\*\*)?([A-D])\)\s")


@dataclass(frozen=True)
class Option:
    letter: str
    text: str


@dataclass(frozen=True)
class Drill:
    prompt: str  # the scenario and the question
    options: list[Option]
    answer: str  # the right option's letter
    steps: list[Step]  # "Answer" first
    after: str = ""  # text after the options (rare)

    def right(self, letter: str) -> bool:
        return letter == self.answer

    def option(self, letter: str) -> Option | None:
        return next((o for o in self.options if o.letter == letter), None)


def parse_drill(text: str) -> Drill | None:
    """A drill's options and answer, or ``None`` if the text doesn't follow
    the format (see the module docstring)."""
    prompt, steps = split_steps(text)
    if not steps or steps[0].title.strip().lower() != "answer":
        return None
    lines = prompt.splitlines()
    found = [(n, _OPTION.match(line)) for n, line in enumerate(lines)]
    marks = [(n, m) for n, m in found if m]
    letters = [m.group(1) for _, m in marks]
    if len(marks) < 2 or letters != list("ABCD"[: len(marks)]):
        return None
    first, last = marks[0][0], marks[-1][0]
    if any(lines[n].strip() and not m for n, m in found[first : last + 1]):
        return None  # text between the options
    answer = _ANSWER.match(steps[0].body.lstrip())
    if answer is None or answer.group(1) not in letters:
        return None
    return Drill(
        prompt="\n".join(lines[:first]).strip(),
        options=[Option(m.group(1), m.group(2).strip()) for _, m in marks],
        answer=answer.group(1),
        steps=steps,
        after="\n".join(lines[last + 1 :]).strip(),
    )


@dataclass(frozen=True)
class Steps:
    """What the reader shows for a step card: the prompt, then each step."""

    prompt: str
    steps: list[Step]
    drill: Drill | None = None


def steps_of(type_: str, text: str) -> Steps:
    """A card's prompt and steps; a well-formed drill also gets its options."""
    if type_ == DRILL_TYPE:
        drill = parse_drill(text)
        if drill is not None:
            return Steps(drill.prompt, drill.steps, drill)
    prompt, steps = split_steps(text)
    return Steps(prompt, steps)


def teaser(type_: str, text: str) -> str:
    """The prose a step card asks, for its teaser: no steps (which would give
    them away), no drill options, and no code blocks, tables or headings."""
    prompt, _ = split_steps(text)
    kept: list[str] = []
    in_fence = False
    for line in prompt.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        bare = line.strip()
        if (
            in_fence
            or bare.startswith("|")
            or (type_ == DRILL_TYPE and _OPTION.match(line))
        ):
            continue
        kept.append(bare.lstrip("#").strip() if bare.startswith("#") else line)
    return "\n".join(kept).strip()
