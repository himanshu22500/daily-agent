"""Practice cards from YouTube videos and from study sessions (issue #139).

Two of the reader's learning sources feed the DSA and System design tracks of
the Interview tab (#130):

- **Videos.** The call that writes a video's story (``agents.video_writer``)
  also returns 0-5 interview cards, none unless the video teaches interview
  prep, DSA or system design. They go under the channel's source
  ``yt-<channel id>`` (label "▶ <channel>"), keyed by video and position, so
  writing a video again updates its cards and never adds more. ``video_cards``
  records every video whose cards were written, even when there were none, so
  the backfill (``videos.cards_from_videos``) skips it.
- **Study sessions.** A transcript batch is interview study when the session
  ran in a study folder (``DAILY_AGENT_PREP_STUDY_PATHS``) or the insight
  extractor recognises it from the content. Then the extractor's cards are
  kept and its coding insights dropped (``insights_capture.collect_file``).
  The cards go under the ``study`` source ("From your study sessions"), keyed
  by their question, so a question studied twice is one card.

A card is a question (the prompt) with tap-to-reveal steps: ``### Answer`` and,
when useful, ``### Why it matters``. A design walkthrough reveals its own steps
(Requirements, Core entities, API, ...) instead of an answer. DSA cards are
``drill``s tagged with pattern slugs; design cards are ``design``s tagged with
topics. The pattern slugs are ``sources.practice_pack.DSA_PATTERNS``, shared
with every DSA writer.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Field, SQLModel, select

from ..db import conflict_insert, create_tables, make_engine, session_scope
from ..models import PrepCard, PrepStep
from ..sources.practice_pack import DSA_PATTERNS, pattern_slugs
from .insights_store import InsightRow
from .prep_tracks import (
    DESIGN,
    DESIGN_TYPE,
    DRILL_TYPE,
    DSA,
    STEP_MARK,
    PrepItem,
    PrepItems,
    PutReport,
    item_key,
    join_steps,
    slug,
    source_session,
)
from .transcripts import TranscriptMessage

# Examples of design topic and building-block slugs (not a closed list).
DESIGN_TAGS: tuple[str, ...] = (
    "caching",
    "cdn",
    "load-balancing",
    "sharding",
    "replication",
    "consistent-hashing",
    "queues",
    "pub-sub",
    "rate-limiting",
    "idempotency",
    "consistency",
    "indexing",
    "sql-vs-nosql",
    "blob-storage",
    "search",
    "websockets",
    "geospatial",
    "distributed-locks",
    "stream-processing",
    "estimation",
)
# Tags that name a track or the whole subject rather than a topic.
_NOT_TOPICS = {
    "dsa",
    "design",
    "system-design",
    "algorithms",
    "data-structures",
    "interview",
    "interview-prep",
}
_TRACKS = {"dsa": DSA, "design": DESIGN, "system-design": DESIGN}

MAX_VIDEO_CARDS = 5
MAX_STUDY_CARDS = 8
MAX_TAGS = 4
MAX_STEPS = 8
QUESTION_CHARS = 300
ANSWER_CHARS = 1_200
WHY_CHARS = 300
STEP_TITLE_CHARS = 60
STEP_CHARS = 1_500
ANSWER, WHY = "Answer", "Why it matters"

VIDEO_SOURCE_PREFIX = "yt-"
STUDY_SOURCE = "study"
STUDY_LABEL = "From your study sessions"

# What a good card is, for every prompt that writes them.
CARD_GUIDE = f"""\
Each card is one question for a reader preparing for mid-level software
engineer interviews (a DSA round and a system design round):
- track: "dsa" for data structures and algorithms, "design" for system design.
- question: what an interviewer could ask to test recall or understanding of
  one idea: why a technique works, when to choose it, what it costs, how to
  apply it. It must stand on its own weeks later, without the source: never
  trivia about the source ("What did the video say about ...?"), and never a
  bare definition when a why or a trade-off can be asked instead.
- answer: what a strong mid-level candidate would say: correct, specific and
  short (1-4 sentences, at most about 80 words), with the reason or the
  trade-off, not just the fact. If the source is wrong or oversimplified, the
  answer is still correct.
- why_it_matters: one sentence on where this comes up in an interview or what
  it unlocks, or "" when there is nothing useful to add.
- tags: canonical lowercase slugs. A dsa card: its 1-2 patterns, from this
  list only: {", ".join(DSA_PATTERNS)}. A design card: 1-4 topic or
  building-block slugs, such as {", ".join(DESIGN_TAGS)}.
Pick the ideas most worth practising or most often gotten wrong, and never
write two cards on the same idea. Write the cards in English.
"""


def card_schema(*, steps: bool = False) -> dict:
    """JSON schema for one card (Claude Code holds replies to it)."""
    properties: dict[str, dict] = {
        "track": {"type": "string", "enum": [DSA, DESIGN]},
        "question": {"type": "string", "minLength": 10, "maxLength": QUESTION_CHARS},
        "answer": {"type": "string", "maxLength": ANSWER_CHARS},
        "why_it_matters": {"type": "string", "maxLength": WHY_CHARS},
        "tags": {
            "type": "array",
            "items": {"type": "string", "maxLength": 40},
            "maxItems": MAX_TAGS,
        },
    }
    if steps:
        properties["steps"] = {
            "type": "array",
            "maxItems": MAX_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": STEP_TITLE_CHARS},
                    "body": {"type": "string", "maxLength": STEP_CHARS},
                },
                "required": ["title", "body"],
            },
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["track", "question", "answer", "why_it_matters", "tags"],
    }


def card_list_schema(max_items: int, *, steps: bool = False) -> dict:
    return {"type": "array", "maxItems": max_items, "items": card_schema(steps=steps)}


# --- cleaning and rendering ------------------------------------------------ #

_BLANKS = re.compile(r"\n\s*\n+")


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _get(item: object, name: str) -> object:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def _line(value: object, limit: int) -> str:
    return _clip(" ".join(_str(value).split()), limit)


def _body(value: object, limit: int) -> str:
    """Paragraphs and code kept; a stray ``### `` line can't start a step."""
    lines: list[str] = []
    fenced = False
    for line in _str(value).replace("\r\n", "\n").strip().splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith(STEP_MARK):
            line = f"**{line[len(STEP_MARK) :].strip()}**"
        lines.append(line.rstrip())
    return _clip(_BLANKS.sub("\n\n", "\n".join(lines)).strip(), limit)


def _tags(track: str, raw: object) -> list[str]:
    out: list[str] = []
    for tag in raw if isinstance(raw, list) else []:
        clean = slug(_str(tag), limit=40)
        if not clean or clean in _NOT_TOPICS:
            continue
        if track != DSA:
            found = [clean]
        elif clean in DSA_PATTERNS:
            found = [clean]
        else:  # only canonical patterns: "hash-map" -> arrays-hashing
            found = pattern_slugs(clean.replace("-", " "))
        for found_tag in found:
            if found_tag not in out:
                out.append(found_tag)
    return out[:MAX_TAGS]


def _steps(raw: object) -> list[PrepStep]:
    steps: list[PrepStep] = []
    for step in raw if isinstance(raw, list) else []:
        title = _line(_get(step, "title"), STEP_TITLE_CHARS).strip("#: ")
        body = _body(_get(step, "body"), STEP_CHARS)
        if title and body:
            steps.append(PrepStep(title=title, body=body))
    return steps[:MAX_STEPS]


def clean_card(raw: object) -> PrepCard | None:
    """A card fit to store, or ``None``: a known track, a question, and an
    answer (or, for a design walkthrough, at least two steps)."""
    track = _TRACKS.get(slug(_str(_get(raw, "track")), limit=20))
    question = _line(_get(raw, "question"), QUESTION_CHARS)
    if track is None or len(question) < 8:
        return None
    steps = _steps(_get(raw, "steps")) if track == DESIGN else []
    if len(steps) < 2:
        steps = []
    answer = "" if steps else _body(_get(raw, "answer"), ANSWER_CHARS)
    if not answer and not steps:
        return None
    return PrepCard(
        track=track,
        question=question,
        answer=answer,
        why_it_matters=_line(_get(raw, "why_it_matters"), WHY_CHARS),
        steps=steps,
        tags=_tags(track, _get(raw, "tags")),
    )


def _norm(question: str) -> str:
    return " ".join(question.lower().split())


def clean_cards(raw: object, *, limit: int) -> list[PrepCard]:
    """Cards fit to store, at most ``limit``, one per question."""
    out: list[PrepCard] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        card = clean_card(item)
        if card is None or _norm(card.question) in seen:
            continue
        seen.add(_norm(card.question))
        out.append(card)
        if len(out) == limit:
            break
    return out


def card_type(card: PrepCard) -> str:
    return DRILL_TYPE if card.track == DSA else DESIGN_TYPE


def card_text(card: PrepCard) -> str:
    """The question, then its steps: Answer (or the walkthrough), Why it matters."""
    steps = [(s.title, s.body) for s in card.steps] or [(ANSWER, card.answer)]
    if card.why_it_matters:
        steps.append((WHY, card.why_it_matters))
    return join_steps(card.question, steps)


def _item(key: str, card: PrepCard, captured_at: datetime | None) -> PrepItem:
    return PrepItem(
        key=key,
        type=card_type(card),
        title=card.question,
        text=card_text(card),
        tags=card.tags,
        captured_at=captured_at,
    )


# --- keys and sources ------------------------------------------------------ #


def video_source(channel_id: str) -> str:
    return VIDEO_SOURCE_PREFIX + channel_id


def video_label(channel: str) -> str:
    return f"▶ {channel or 'YouTube'}"


def video_card_prefix(channel_id: str, video_id: str) -> str:
    """Every key of a video's cards starts with this; the card's position
    follows. The video id keeps its case: two ids may differ only in case."""
    return f"{source_session(video_source(channel_id))}:{video_id}-"


def study_card_key(question: str) -> str:
    """Same question, same key; a hash keeps similar questions apart."""
    digest = hashlib.sha1(_norm(question).encode()).hexdigest()[:8]
    return item_key(STUDY_SOURCE, f"{slug(question, limit=48)}-{digest}")


# --- study sessions -------------------------------------------------------- #


def study_folders(setting: str) -> list[str]:
    """``DAILY_AGENT_PREP_STUDY_PATHS``: comma-separated folder names or paths."""
    return [part.strip() for part in setting.split(",") if part.strip()]


def in_study_folder(cwd: str, folders: Iterable[str]) -> bool:
    """Whether a session's working directory is inside a folder given as a
    path (``~/prep``), or has a folder of that name on its path
    (``interview-prep`` matches ``~/notes/interview-prep/dsa``, not
    ``~/interview-prep-tools``)."""
    if not cwd:
        return False
    here = cwd.rstrip("/").lower() + "/"
    for folder in folders:
        if folder.startswith(("/", "~")):
            root = os.path.expanduser(folder).rstrip("/").lower() + "/"
            if here.startswith(root):
                return True
        elif f"/{folder.strip('/').lower()}/" in here:
            return True
    return False


def is_study_session(messages: Iterable[TranscriptMessage], setting: str) -> bool:
    folders = study_folders(setting)
    return bool(folders) and any(in_study_folder(m.cwd, folders) for m in messages)


# --- storage --------------------------------------------------------------- #


class VideoCardRow(SQLModel, table=True):
    """A video whose practice cards were written, even if there were none."""

    __tablename__ = "video_cards"

    video_id: str = Field(primary_key=True)
    cards: int = 0
    written_at: str


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


class PracticeCards:
    """Cards from videos and study sessions, through ``PrepItems``."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        self.items = PrepItems(db_path)
        create_tables(self._engine, VideoCardRow)

    def put_video(
        self,
        video_id: str,
        channel_id: str,
        channel: str,
        cards: Iterable[PrepCard],
        *,
        captured_at: datetime | None = None,
        now: datetime | None = None,
    ) -> PutReport:
        """Store a video's cards in place of any it had, and mark it done."""
        moment = now or datetime.now(timezone.utc)
        prefix = video_card_prefix(channel_id, video_id)
        items = [
            _item(f"{prefix}{n}", card, captured_at)
            for n, card in enumerate(
                clean_cards(list(cards), limit=MAX_VIDEO_CARDS), start=1
            )
        ]
        report = PutReport()
        if items:
            report = self.items.put(
                video_source(channel_id), video_label(channel), items, now=moment
            )
        keep = {item.key for item in items}
        report.removed = self.items.insights.delete(
            [key for key in self._keys(prefix) if key not in keep]
        )
        values = {"cards": len(items), "written_at": _iso(moment)}
        with session_scope(self._engine) as session:
            session.execute(
                conflict_insert(session, VideoCardRow)
                .values(video_id=video_id, **values)
                .on_conflict_do_update(index_elements=["video_id"], set_=values)
            )
        return report

    def _keys(self, prefix: str) -> list[str]:
        with session_scope(self._engine) as session:
            keys = session.exec(
                select(InsightRow.key).where(
                    InsightRow.key.startswith(prefix, autoescape=True)
                )
            ).all()
        # SQLite's LIKE ignores case; video ids don't.
        return [key for key in keys if key.startswith(prefix)]

    def done_videos(self) -> set[str]:
        """Videos whose cards were written (with or without any cards)."""
        with session_scope(self._engine) as session:
            return set(session.exec(select(VideoCardRow.video_id)).all())

    def put_study(
        self,
        cards: Iterable[PrepCard],
        *,
        captured_at: datetime | None = None,
        now: datetime | None = None,
    ) -> PutReport:
        """Store cards from a study session; a known question is updated."""
        items = [
            _item(study_card_key(card.question), card, captured_at)
            for card in clean_cards(list(cards), limit=MAX_STUDY_CARDS)
        ]
        if not items:
            return PutReport()
        return self.items.put(STUDY_SOURCE, STUDY_LABEL, items, now=now)
