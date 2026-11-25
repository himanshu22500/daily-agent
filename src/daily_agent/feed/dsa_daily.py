"""Daily DSA problems and "which pattern?" drills in the DSA track (#138).

Every local day the Mac writes ``prep_dsa_per_day`` new problems (default 1)
and one batch of ``prep_drills_per_day`` drills (default 3), with Claude
(``agents/dsa_writer.py``):

- **Which pattern.** A problem goes to the reader's weakest DSA pattern
  (``GradeStore.weak_tags`` over ``dsa`` and ``drill`` cards, ignoring tags
  that name a source, such as ``blinkit``). With nothing weak yet, it goes to
  the pattern with the fewest ``dsa`` cards. Two problems on one day get two
  patterns. Drills lean the same way: about two thirds go to recently missed
  and weak patterns, the rest to the least-drilled ones, so the whole
  vocabulary comes round over time.
- **No repeats.** Claude gets the titles of the cards already on the pattern
  (with their Pattern notes), and a title already in the feed is sent back.
- **Correct or nothing.** The optimal solution has to pass the tests written
  with it (``feed/dsa_check.py``). A failure goes back to Claude once, with
  the failing tests; a second failure rejects the problem, and the job is
  retried later like any failed write.
- **Pacing.** Jobs sit in ``prep_dsa_jobs`` and go through the shared
  ``StoryQueue`` (claim, retry with backoff, give up). The day's jobs are
  queued on the first pass of each local day. Jobs left over from an earlier
  day are skipped then, so a Mac that slept for a week writes one day's
  worth, not seven.

Problems go in under source ``dsa-daily`` ("Daily DSA") and drills under
``dsa-drills`` ("Pattern drills"), ``web-only``, through ``PrepItems``.

A problem's card is its prompt (Scenario, Task, signature, Constraints,
Examples, Questions to ask the interviewer), then the steps Pattern, Key
idea, Brute force, Optimal solution, Walkthrough, and Edge cases &
follow-ups. A drill's card is exactly::

    <2-4 scenario lines>

    <one question line ending in "?">
    A) <pattern name>
    B) <pattern name>
    C) <pattern name>
    D) <pattern name>

    ### Answer
    <the correct option line, exactly as listed>
    <2-3 lines on why>

    ### Why not the others
    <each wrong option line, as listed> — <why not>   (one line each, A to D)

``check_drill_card`` checks that format; every drill is checked before it's
stored. Drill tags are the correct pattern's slug and ``drill``; problem tags
are the pattern (plus any it also relies on), ``blinkit`` and ``daily``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from sqlalchemy import and_, func, or_, update
from sqlmodel import Field, SQLModel, select

from ..agents.dsa_writer import (
    Drill,
    DrillRequest,
    Problem,
    ProblemRequest,
)
from ..db import conflict_insert, create_tables, make_engine, session_scope
from ..sources.practice_pack import (
    BRUTE_STEP,
    DSA_PATTERNS,
    EDGES_STEP,
    KEY_IDEA_STEP,
    OPTIMAL_STEP,
    PATTERN_STEP,
    WALKTHROUGH_STEP,
)
from .dsa_check import CheckResult, check_solution
from .headlines import plain
from .insights_store import InsightRow
from .prep_grades import MISSED, GradeStore
from .prep_tracks import (
    DRILL_TYPE,
    DSA,
    PROBLEM_TYPE,
    STEP_MARK,
    PrepItem,
    PrepItems,
    item_key,
    join_steps,
    slug,
    source_session,
    split_steps,
    types_for,
)
from .story_queue import (
    CLAIM_TTL,
    DONE,
    FAILED,
    PENDING,
    SKIPPED,
    WRITING,
    StoryQueue,
    SyncReport,
    first_line,
    iso,
    now_utc,
    sync_forever,
)

_LOG = logging.getLogger(__name__)

DAILY_SOURCE, DAILY_LABEL = "dsa-daily", "Daily DSA"
DRILL_SOURCE, DRILL_LABEL = "dsa-drills", "Pattern drills"
# Tags that name where a card came from, not a skill (never "weak").
SOURCE_TAGS = ("blinkit", "must-do", "daily", "drill")
PROBLEM_TAGS = ("blinkit", "daily")
DRILL_TAG = "drill"
PROBLEM, DRILLS = "problem", "drills"
PER_RUN = 3
IDLE_SECONDS = 600.0
MISSED_WINDOW = timedelta(days=14)
MAX_EXISTING = 60
MAX_RECENT_DRILLS = 30
LETTERS = "ABCD"
ORDER = {p: i for i, p in enumerate(DSA_PATTERNS)}  # vocabulary order


class DsaJobRow(SQLModel, table=True):
    """One problem, or one batch of drills, to write."""

    __tablename__ = "prep_dsa_jobs"

    id: str = Field(primary_key=True)  # "problem:2026-11-25:1", "drills:2026-11-25"
    kind: str  # problem | drills
    day: str  # the local day it's for
    pattern: str = ""  # asked for, else the one chosen when written
    count: int = 1  # drills in the batch
    published_at: str  # when it was queued; orders the queue
    state: str = PENDING
    attempts: int = 0
    not_before: str | None = None
    claimed_at: str | None = None
    error: str = ""
    keys: str = ""  # the cards it wrote, comma-joined


class DsaStore(StoryQueue):
    """The ``prep_dsa_jobs`` queue (ready, claim, finish, fail_attempt)."""

    item_row = DsaJobRow
    item_id = "id"
    newest_first = False

    def __init__(self, db_path: str | Path) -> None:
        self._engine = make_engine(str(db_path))
        create_tables(self._engine, DsaJobRow)

    def plan_day(self, day: str, *, now: datetime, problems: int, drills: int) -> int:
        """Queue ``day``'s jobs unless they are queued already, and skip jobs
        left over from earlier days. Returns how many were queued."""
        job = DsaJobRow
        rows = [
            {"id": f"{PROBLEM}:{day}:{n}", "kind": PROBLEM, "count": 1}
            for n in range(1, problems + 1)
        ]
        if drills > 0:
            rows.append({"id": f"{DRILLS}:{day}", "kind": DRILLS, "count": drills})
        with session_scope(self._engine) as session:
            session.execute(
                update(job)
                .where(job.day < day)
                .where(
                    or_(
                        job.state == PENDING,
                        and_(
                            job.state == WRITING, job.claimed_at < iso(now - CLAIM_TTL)
                        ),
                    )
                )
                .values(
                    state=SKIPPED,
                    not_before=None,
                    claimed_at=None,
                    error="skipped: a new day began",
                )
            )
            if not rows:
                return 0
            stamp = {"day": day, "published_at": iso(now), "state": PENDING}
            added = session.execute(
                conflict_insert(session, job)
                .values([{**row, **stamp} for row in rows])
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(job.id)
            ).all()
        return len(added)

    def add(
        self, kind: str, *, day: str, now: datetime, pattern: str = "", count: int = 1
    ) -> str:
        """Queue one extra job (``prep dsa write --pattern``/``--drills``)."""
        job_id = f"{kind}:{day}:x{uuid.uuid4().hex[:8]}"
        with session_scope(self._engine) as session:
            session.add(
                DsaJobRow(
                    id=job_id,
                    kind=kind,
                    day=day,
                    pattern=pattern,
                    count=count,
                    published_at=iso(now),
                )
            )
        return job_id

    def jobs(
        self, ids: Iterable[str] | None = None, *, day: str | None = None
    ) -> list[DsaJobRow]:
        stmt = select(DsaJobRow).order_by(DsaJobRow.published_at, DsaJobRow.id)
        if ids is not None:
            stmt = stmt.where(DsaJobRow.id.in_(list(ids)))
        if day is not None:
            stmt = stmt.where(DsaJobRow.day == day)
        with session_scope(self._engine) as session:
            return list(session.exec(stmt).all())

    def failed(self, limit: int = 5) -> list[DsaJobRow]:
        with session_scope(self._engine) as session:
            return list(
                session.exec(
                    select(DsaJobRow)
                    .where(DsaJobRow.state == FAILED)
                    .order_by(DsaJobRow.published_at.desc(), DsaJobRow.id)
                    .limit(limit)
                ).all()
            )

    def record(self, job_id: str, *, pattern: str, keys: Sequence[str]) -> None:
        self._set(job_id, pattern=pattern, keys=",".join(keys))

    def patterns_written(self, day: str) -> list[str]:
        """Patterns of the problems written for ``day``."""
        with session_scope(self._engine) as session:
            return list(
                session.exec(
                    select(DsaJobRow.pattern)
                    .where(DsaJobRow.day == day)
                    .where(DsaJobRow.kind == PROBLEM)
                    .where(DsaJobRow.state == DONE)
                    .where(DsaJobRow.pattern != "")
                    .order_by(DsaJobRow.published_at, DsaJobRow.id)
                ).all()
            )


# --- choosing patterns --------------------------------------------------- #


def pick_problem_pattern(
    weak: Sequence[str], coverage: dict[str, int], *, exclude: Iterable[str] = ()
) -> tuple[str, str]:
    """``(pattern, why)``: the weakest pattern not excluded, else the one with
    the fewest problem cards (vocabulary order breaks ties)."""
    skip = set(exclude)
    for pattern in weak:
        if pattern in DSA_PATTERNS and pattern not in skip:
            return pattern, "weakest"
    candidates = [p for p in DSA_PATTERNS if p not in skip] or list(DSA_PATTERNS)
    least = min(candidates, key=lambda p: (coverage.get(p, 0), ORDER[p]))
    return least, "least covered"


def pick_drill_patterns(
    count: int,
    *,
    missed: Sequence[str] = (),
    weak: Sequence[str] = (),
    drilled: dict[str, int] | None = None,
) -> list[str]:
    """``count`` distinct targets: about two thirds from recently missed, then
    weak, patterns; the rest (and any shortfall) from the least drilled."""
    count = max(0, min(count, len(DSA_PATTERNS)))
    lean = [p for p in dict.fromkeys([*missed, *weak]) if p in DSA_PATTERNS]
    chosen = lean[: max(1, round(count * 2 / 3))] if count else []
    drilled = drilled or {}
    rest = sorted(
        (p for p in DSA_PATTERNS if p not in chosen),
        key=lambda p: (drilled.get(p, 0), ORDER[p]),
    )
    return (chosen + rest)[:count]


# --- card text ----------------------------------------------------------- #

_OPTION = re.compile(r"^([A-D])\) (\S.*)$")


def _prose(text: str) -> str:
    """Model text made safe inside a card: no line opens a step, and an
    unclosed code fence is closed."""
    lines, fenced = [], False
    for line in text.strip().splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith(STEP_MARK):
            line = line[len(STEP_MARK) :]
        lines.append(line)
    if fenced:
        lines.append("```")
    return "\n".join(lines)


def _bullets(lines: Iterable[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def _code(code: str, language: str = "") -> str:
    return f"```{language}\n{code.strip(chr(10))}\n```"


def problem_card(problem: Problem) -> str:
    """The card text: the prompt, then one ``### `` step per reveal."""
    prompt = [
        f"**Scenario.** {_prose(problem.scenario)}",
        f"**Task.** {_prose(problem.task)}",
    ]
    if problem.signature:
        prompt.append(_code(problem.signature, "python"))
    if problem.constraints:
        prompt.append("**Constraints**\n" + _bullets(problem.constraints))
    for number, example in enumerate(problem.examples, 1):
        label = "Example" if len(problem.examples) == 1 else f"Example {number}"
        block = f"**{label}**\n" + _code(
            f"Input: {example.input}\nOutput: {example.output}"
        )
        if example.explanation:
            block += "\n" + _prose(example.explanation)
        prompt.append(block)
    if problem.questions:
        prompt.append(
            "**Questions to ask the interviewer**\n"
            + _bullets(f"{q} → {a}" for q, a in problem.questions)
        )
    names = " + ".join(
        DSA_PATTERNS[p] for p in [problem.pattern, *problem.also_patterns]
    )
    pattern = [f"**{names}**"]
    if problem.leetcode:
        pattern.append("LeetCode: " + " · ".join(problem.leetcode))
    pattern.append(
        f"Target: {problem.time_complexity} time, {problem.space_complexity} space"
    )
    edges = _bullets(problem.edge_cases)
    if problem.follow_ups:
        edges += "\n\n**Follow-ups**\n" + _bullets(
            f"**{q}** {a}" for q, a in problem.follow_ups
        )
    # The practice pack's steps (#135), so every DSA card reveals the same way.
    steps = [
        (PATTERN_STEP, "\n".join(pattern)),
        (KEY_IDEA_STEP, _prose(problem.key_idea)),
        (BRUTE_STEP, _prose(problem.brute_force)),
        (OPTIMAL_STEP, _code(problem.solution, "python")),
        (WALKTHROUGH_STEP, _prose(problem.walkthrough)),
        (EDGES_STEP, edges),
    ]
    return join_steps("\n\n".join(prompt), steps)


def drill_card(drill: Drill, slot: int) -> str:
    """The drill's card, with the correct option at letter ``slot`` (0-3)."""
    patterns = [p for p, _ in drill.distractors]
    patterns.insert(slot, drill.pattern)
    options = [f"{LETTERS[i]}) {DSA_PATTERNS[p]}" for i, p in enumerate(patterns)]
    reasons = dict(drill.distractors)
    prompt = "\n".join(drill.scenario) + "\n\n" + drill.question + "\n"
    prompt += "\n".join(options)
    answer = "\n".join([options[slot], *drill.why])
    why_not = "\n".join(
        f"{option} — {reasons[pattern]}"
        for option, pattern in zip(options, patterns)
        if pattern != drill.pattern
    )
    return join_steps(prompt, [("Answer", answer), ("Why not the others", why_not)])


def check_drill_card(text: str) -> list[str]:
    """What's wrong with a drill card's format (``[]`` when nothing is)."""
    prompt, steps = split_steps(text)
    lines = [line.rstrip() for line in prompt.splitlines() if line.strip()]
    found = [m for m in map(_OPTION.match, lines) if m]
    if [m.group(1) for m in found] != list(LETTERS):
        return ["the prompt needs exactly four options, A) to D), in order"]
    options = [m.group(0) for m in found]
    problems = []
    if lines[-4:] != options:
        problems.append("the options must be the prompt's last four lines")
    elif not lines[-5:-4] or not lines[-5].endswith("?"):
        problems.append('a question line ending in "?" must come before the options')
    elif not 2 <= len(lines) - 5 <= 4:
        problems.append("the scenario must be 2-4 lines")
    if len({m.group(2) for m in found}) != 4:
        problems.append("the options must name four different patterns")
    if [s.title for s in steps] != ["Answer", "Why not the others"]:
        return problems + ['the steps must be "Answer", then "Why not the others"']
    answer = [line for line in steps[0].body.splitlines() if line.strip()]
    if not answer or answer[0] not in options:
        return problems + ["the answer's first line must be one option, as listed"]
    if not 2 <= len(answer) - 1 <= 3:
        problems.append("the answer needs 2-3 lines on why")
    wrong = [o for o in options if o != answer[0]]
    why_not = [line for line in steps[1].body.splitlines() if line.strip()]
    if len(why_not) != 3 or not all(
        line.startswith(option + " ") for line, option in zip(why_not, wrong)
    ):
        problems.append("why-not needs one line per wrong option, in order")
    return problems


def pattern_note(text: str) -> str:
    """The start of a card's Pattern step (what an existing problem maps to)."""
    _, steps = split_steps(text)
    body = next((s.body for s in steps if s.title.lower() == "pattern"), "")
    note = plain(body)
    return note if len(note) <= 200 else note[:199].rsplit(" ", 1)[0] + "…"


# --- writing --------------------------------------------------------------- #

ProblemWriter = Callable[[ProblemRequest], Awaitable[Problem]]
DrillWriter = Callable[[DrillRequest], Awaitable[list[Drill]]]
Checker = Callable[[Problem], Awaitable[CheckResult]]


async def check_problem(problem: Problem) -> CheckResult:
    """Run the problem's optimal solution against its tests, isolated."""
    return await check_solution(
        problem.code_under_test,
        problem.tests,
        entry=problem.entry,
        kind=problem.entry_kind,
        compare=problem.compare,
    )


class ProblemRejected(RuntimeError):
    """The problem failed its checks twice."""


@dataclass
class DsaReport(SyncReport):
    problems: int = 0
    drills: int = 0


@dataclass(frozen=True)
class DsaStatus:
    day: str
    problems: int  # daily problems in the feed
    drills: int
    today: list[DsaJobRow]
    failed: list[DsaJobRow]
    weak: list[str]
    next_pattern: str
    next_reason: str
    uncovered: list[str] = field(default_factory=list)  # no problem card yet


class DsaSync:
    """Plan each day's DSA cards and write them (background loop or CLI)."""

    def __init__(
        self,
        db_path: str | Path,
        write_problem: ProblemWriter,
        write_drills: DrillWriter,
        *,
        per_day: int = 1,
        drills_per_day: int = 3,
        check: Checker = check_problem,
        per_run: int = PER_RUN,
        clock: Callable[[], datetime] = now_utc,
        tz: tzinfo | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.store = DsaStore(db_path)
        self.items = PrepItems(db_path)
        self.grades = GradeStore(db_path)
        self._engine = make_engine(str(db_path))
        self.write_problem = write_problem
        self.write_drills = write_drills
        self.per_day = max(0, per_day)
        self.drills_per_day = max(0, drills_per_day)
        self.check = check
        self.per_run = max(1, per_run)
        self.clock = clock
        # Days start at local midnight (this machine's zone), as in #124.
        self.tz = tz or datetime.now().astimezone().tzinfo
        self.rng = rng or random.Random()

    def today(self) -> str:
        return self.clock().astimezone(self.tz).date().isoformat()

    # --- what the reader needs ---------------------------------------- #
    def weak_patterns(self) -> list[str]:
        tags = self.grades.weak_tags(types_for(DSA), limit=1000, ignore=SOURCE_TAGS)
        return [t for t in tags if t in DSA_PATTERNS]

    def coverage(self, type_: str) -> dict[str, int]:
        """Cards of ``type_`` per pattern tag."""
        return {
            s.tag: s.cards
            for s in self.grades.tag_stats((type_,))
            if s.tag in DSA_PATTERNS
        }

    def recently_missed(self) -> list[str]:
        """Patterns of DSA cards graded Missed lately, most recent first."""
        since = self.clock() - MISSED_WINDOW
        keys = [g.key for g in self.grades.history(since=since) if g.grade == MISSED]
        if not keys:
            return []
        with session_scope(self._engine) as session:
            tags = dict(
                session.exec(
                    select(InsightRow.key, InsightRow.tags)
                    .where(InsightRow.key.in_(keys))
                    .where(InsightRow.type.in_(types_for(DSA)))
                ).all()
            )
        found = [
            tag
            for key in reversed(keys)
            for tag in (tags.get(key) or "").split(",")
            if tag in DSA_PATTERNS
        ]
        return list(dict.fromkeys(found))

    def problem_pattern(self, *, exclude: Iterable[str] = ()) -> tuple[str, str]:
        return pick_problem_pattern(
            self.weak_patterns(), self.coverage(PROBLEM_TYPE), exclude=exclude
        )

    def drill_patterns(self, count: int) -> list[str]:
        return pick_drill_patterns(
            count,
            missed=self.recently_missed(),
            weak=self.weak_patterns(),
            drilled=self.coverage(DRILL_TYPE),
        )

    def _cards(self, type_: str) -> list[tuple[str, str, list[str], str]]:
        """``(key, title, tags, text)`` of every card of ``type_``, newest first."""
        with session_scope(self._engine) as session:
            rows = session.exec(
                select(
                    InsightRow.key, InsightRow.title, InsightRow.tags, InsightRow.text
                )
                .where(InsightRow.type == type_)
                .order_by(InsightRow.captured_at.desc(), InsightRow.key)
            ).all()
        return [
            (key, title or "", (tags or "").split(","), text)
            for key, title, tags, text in rows
        ]

    def _count(self, source_id: str) -> int:
        with session_scope(self._engine) as session:
            return session.scalar(
                select(func.count())
                .select_from(InsightRow)
                .where(InsightRow.source_session == source_session(source_id))
            )

    # --- passes ------------------------------------------------------------ #
    async def run_once(self, *, limit: int | None = None) -> DsaReport:
        """Queue today's jobs if this is the day's first pass, then write up
        to ``limit`` ready jobs."""
        now = self.clock()
        self.store.plan_day(
            self.today(), now=now, problems=self.per_day, drills=self.drills_per_day
        )
        report = DsaReport()
        for job in self.store.ready(now=now, limit=limit or self.per_run):
            await self._run(job, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    async def write_now(
        self, *, problems: int = 0, pattern: str = "", drills: int = 0
    ) -> DsaReport:
        """Write extra cards now, beyond the day's: ``problems`` problems (on
        ``pattern``, or chosen as usual) and a batch of ``drills`` drills."""
        if pattern and pattern not in DSA_PATTERNS:
            raise ValueError(f"not a DSA pattern: {pattern!r}")
        now, day = self.clock(), self.today()
        ids = [
            self.store.add(PROBLEM, day=day, now=now, pattern=pattern)
            for _ in range(max(0, problems))
        ]
        if drills > 0:
            ids.append(self.store.add(DRILLS, day=day, now=now, count=drills))
        report = DsaReport()
        for job in self.store.jobs(ids):
            await self._run(job, report)
        report.remaining = self.store.ready_count(now=self.clock())
        return report

    async def _run(self, job: DsaJobRow, report: DsaReport) -> None:
        if not self.store.claim(job.id, now=self.clock()):
            return
        try:
            if job.kind == DRILLS:
                keys, pattern = await self._drills(job)
                report.drills += len(keys)
            else:
                keys, pattern = await self._problem(job)
                report.problems += 1
        except Exception as exc:  # noqa: BLE001 - one job never stops the rest
            error = first_line(exc)
            if self.store.fail_attempt(job.id, error, now=self.clock()):
                report.failed += 1
            report.errors.append(f"{job.id}: {error}")
            _LOG.warning("DSA job %s: %s", job.id, error)
            return
        self.store.record(job.id, pattern=pattern, keys=keys)
        self.store.finish(job.id)
        report.written += 1

    async def _problem(self, job: DsaJobRow) -> tuple[list[str], str]:
        pattern = (
            job.pattern
            or (self.problem_pattern(exclude=self.store.patterns_written(job.day))[0])
        )
        cards = self._cards(PROBLEM_TYPE)
        taken = {slug(title) for _, title, _, _ in cards if title}
        request = ProblemRequest(
            pattern=pattern,
            existing=[
                (title, pattern_note(text))
                for _, title, tags, text in cards
                if pattern in tags and title
            ][:MAX_EXISTING],
        )
        problem = await self.write_problem(request)
        issues = await self._issues(problem, taken)
        if issues:
            retry = replace(request, previous=problem.raw, issues=issues)
            problem = await self.write_problem(retry)
            issues = await self._issues(problem, taken)
            if issues:
                raise ProblemRejected("rejected: " + "; ".join(issues))
        key = item_key(DAILY_SOURCE, problem.title)
        item = PrepItem(
            key=key,
            type=PROBLEM_TYPE,
            title=problem.title,
            text=problem_card(problem),
            tags=[pattern, *problem.also_patterns, *PROBLEM_TAGS],
        )
        self.items.put(DAILY_SOURCE, DAILY_LABEL, [item], track=DSA, now=self.clock())
        return [key], pattern

    async def _issues(self, problem: Problem, taken: set[str]) -> list[str]:
        """Why ``problem`` can't go in the feed as it is (``[]`` if it can)."""
        key = item_key(DAILY_SOURCE, problem.title)
        if slug(problem.title) in taken or self.items.insights.get(key) is not None:
            return [
                f"the title {problem.title!r} is already in the feed: write a "
                "different problem"
            ]
        result = await self.check(problem)
        if result.ok:
            return []
        if result.error:
            return [f"the optimal solution failed: {result.error}"]
        return [
            f"the optimal solution passed {result.passed} of {result.total} tests",
            *result.failures[:5],
        ]

    async def _drills(self, job: DsaJobRow) -> tuple[list[str], str]:
        targets = self.drill_patterns(job.count)
        recent = [title for _, title, _, _ in self._cards(DRILL_TYPE) if title]
        drills = await self.write_drills(
            DrillRequest(patterns=targets, recent_titles=recent[:MAX_RECENT_DRILLS])
        )
        items = []
        for drill in drills[: job.count]:
            text = drill_card(drill, self.rng.randrange(len(LETTERS)))
            problems = check_drill_card(text)
            if problems:  # a drill the reader can't answer is worse than none
                _LOG.warning("Drill %r dropped: %s", drill.title, "; ".join(problems))
                continue
            items.append(
                PrepItem(
                    key=item_key(DRILL_SOURCE, f"{job.id}-{len(items) + 1}"),
                    type=DRILL_TYPE,
                    title=drill.title,
                    text=text,
                    tags=[drill.pattern, DRILL_TAG],
                )
            )
        if not items:
            raise ProblemRejected("no drill in the reply had the right format")
        self.items.put(DRILL_SOURCE, DRILL_LABEL, items, track=DSA, now=self.clock())
        return [i.key for i in items], ",".join(i.tags[0] for i in items)

    # --- status ------------------------------------------------------------ #
    def status(self) -> DsaStatus:
        day = self.today()
        weak = self.weak_patterns()
        pattern, why = self.problem_pattern(exclude=self.store.patterns_written(day))
        covered = self.coverage(PROBLEM_TYPE)
        return DsaStatus(
            day=day,
            problems=self._count(DAILY_SOURCE),
            drills=self._count(DRILL_SOURCE),
            today=self.store.jobs(day=day),
            failed=self.store.failed(),
            weak=weak,
            next_pattern=pattern,
            next_reason=why,
            uncovered=[p for p in DSA_PATTERNS if not covered.get(p)],
        )


def run_in_background(sync: DsaSync, *, idle: float = IDLE_SECONDS) -> threading.Thread:
    """Write the daily cards from a daemon thread for as long as the process
    runs (``daily-agent web`` on the Mac). A pass every ``idle`` seconds plans
    the day and writes what's due. The thread has its own event loop, so it
    starts without touching the feed app's lifespan."""

    async def loop() -> None:
        await sync_forever(sync, asyncio.Event(), idle=idle)

    thread = threading.Thread(
        target=lambda: asyncio.run(loop()), name="dsa-daily", daemon=True
    )
    thread.start()
    return thread
