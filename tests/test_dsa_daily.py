"""Daily DSA problems and "which pattern?" drills (#138). Offline: fake writers
stand in for Claude, and tmp_path SQLite (or Postgres schemas in CI) for the DB.
Solutions that do get checked run in the local interpreter, isolated."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.agents import dsa_writer
from daily_agent.agents.dsa_writer import (
    Drill,
    DrillRequest,
    ProblemRequest,
    parse_problem,
)
from daily_agent.db_copy import STORE_MODULES, store_tables
from daily_agent.feed import dsa_daily, prep_view
from daily_agent.feed.dsa_check import CheckResult
from daily_agent.feed.dsa_daily import (
    DAILY_SOURCE,
    DRILL_SOURCE,
    DsaSync,
    check_drill_card,
    check_problem,
    drill_card,
    pattern_note,
    pick_drill_patterns,
    pick_problem_pattern,
    problem_card,
)
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.prep_grades import GOT, MISSED, GradeStore
from daily_agent.feed.prep_tracks import (
    DRILL_TYPE,
    DSA,
    PROBLEM_TYPE,
    PrepItem,
    PrepItems,
    item_key,
    join_steps,
    split_steps,
)
from daily_agent.feed.story_queue import DONE, FAILED, MAX_ATTEMPTS, PENDING, SKIPPED
from daily_agent.sources.practice_pack import DSA_PATTERNS, DSA_STEPS

from test_dsa_writer import SOLUTION, problem_reply

NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
DAY = "2026-11-25"


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def fake_drill(pattern: str, n: int = 1, **changes) -> Drill:
    wrong = [p for p in DSA_PATTERNS if p != pattern][:3]
    values = {
        "pattern": pattern,
        "title": f"Shelf puzzle {n}",
        "scenario": [
            "A dark store has n racks, n up to 10^5.",
            "Each picker needs an answer per rack.",
        ],
        "question": "Which pattern gives the optimal solution?",
        "distractors": [(p, f"{DSA_PATTERNS[p]} does not fit here.") for p in wrong],
        "why": ["The signal is in the second line.", "O(n) overall."],
    }
    values.update(changes)
    return Drill(**values)


class Writers:
    """Fake ``write_problem``/``write_drills``: replies in order, then defaults."""

    def __init__(self):
        self.problem_requests: list[ProblemRequest] = []
        self.drill_requests: list[DrillRequest] = []
        self.replies: list[dict] = []
        self.drills: list[list[Drill]] = []
        self.failing = False

    async def problem(self, request: ProblemRequest):
        self.problem_requests.append(request)
        if self.failing:
            raise RuntimeError("claude -p exited 1\nusage limit reached")
        reply = (
            self.replies.pop(0)
            if self.replies
            else problem_reply(title=f"Picker Batch {len(self.problem_requests)}")
        )
        return parse_problem(reply, request.pattern)

    async def drill(self, request: DrillRequest) -> list[Drill]:
        self.drill_requests.append(request)
        if self.failing:
            raise RuntimeError("claude -p exited 1\nusage limit reached")
        if self.drills:
            return self.drills.pop(0)
        return [fake_drill(p, n) for n, p in enumerate(request.patterns, 1)]


async def passes(problem) -> CheckResult:
    return CheckResult(total=len(problem.tests), passed=len(problem.tests))


async def fails(problem) -> CheckResult:
    return CheckResult(
        total=4, passed=3, failures=["test 4 (input [[1], 1]): expected [1], got []"]
    )


@pytest.fixture
def make(tmp_path):
    db = str(tmp_path / "dsa.db")
    writers = Writers()
    clock = Clock()

    def build(**kwargs) -> DsaSync:
        kwargs.setdefault("check", passes)
        kwargs.setdefault("tz", timezone.utc)
        return DsaSync(db, writers.problem, writers.drill, clock=clock, **kwargs)

    return build, writers, clock, db


def pack_card(db, n: int, pattern: str, *, type_=PROBLEM_TYPE, title=None) -> str:
    """A card from someone else's source (a practice pack), tagged ``pattern``."""
    title = title or f"Pack problem {n}"
    key = item_key("dsa-pack", f"p{n}")
    text = join_steps(
        f"Statement {n}.",
        [("Pattern", f"**{DSA_PATTERNS[pattern]}**\nTarget: O(n)"), ("Key idea", "x")],
    )
    PrepItems(db).put(
        "dsa-pack",
        "Blinkit DSA pack",
        [PrepItem(key, type_, title, text, tags=[pattern, "blinkit", "must-do"])],
        track=DSA,
        now=NOW - timedelta(days=3),
    )
    return key


# --- choosing patterns ------------------------------------------------------ #


def test_problem_pattern_is_the_weakest_else_the_least_covered():
    assert pick_problem_pattern(["blinkit", "heap", "dp"], {}) == ("heap", "weakest")
    assert pick_problem_pattern(["heap", "dp"], {}, exclude=["heap"]) == (
        "dp",
        "weakest",
    )
    covered = {p: 2 for p in DSA_PATTERNS}
    covered |= {"trie": 0, "math": 0, "dp": 1}
    assert pick_problem_pattern([], covered) == ("trie", "least covered")  # order
    assert pick_problem_pattern(["heap"], covered, exclude=["heap", "trie"]) == (
        "math",
        "least covered",
    )
    assert pick_problem_pattern([], {}, exclude=DSA_PATTERNS)[0] == "arrays-hashing"


def test_drill_targets_lean_to_missed_and_weak_then_cover_the_rest():
    drilled = {p: 3 for p in DSA_PATTERNS} | {"trie": 0, "math": 1}
    targets = pick_drill_patterns(
        3, missed=["intervals", "blinkit"], weak=["heap", "intervals"], drilled=drilled
    )
    assert targets == ["intervals", "heap", "trie"]
    assert pick_drill_patterns(3, drilled=drilled) == ["trie", "math", "arrays-hashing"]
    assert pick_drill_patterns(1, missed=["dp"], weak=["heap"]) == ["dp"]
    assert pick_drill_patterns(0, missed=["dp"]) == []
    everything = pick_drill_patterns(99, weak=["heap"])
    assert sorted(everything) == sorted(DSA_PATTERNS) and everything[0] == "heap"


async def test_the_daily_problem_goes_to_the_weakest_pattern(make):
    build, writers, _, db = make
    sync = build()
    missed = pack_card(db, 1, "heap", title="Top-K Trending SKUs")
    got = pack_card(db, 2, "two-pointers")
    grades = GradeStore(db)
    grades.grade(missed, MISSED, now=NOW - timedelta(days=1))
    grades.grade(got, GOT, now=NOW - timedelta(days=1))
    assert sync.weak_patterns() == ["heap"]  # never blinkit or must-do

    writers.replies = [problem_reply(title="Rider Batch Ranking")]
    report = await sync.run_once()
    assert (report.problems, report.drills, report.written) == (1, 3, 2)

    [request] = writers.problem_requests
    assert request.pattern == "heap"
    assert request.existing == [
        ("Top-K Trending SKUs", "Heap / priority queue Target: O(n)")
    ]
    key = item_key(DAILY_SOURCE, "Rider Batch Ranking")
    card = InsightStore(db).get(key)
    assert card.type == PROBLEM_TYPE and card.status == "web-only"
    assert card.title == "Rider Batch Ranking"
    assert card.tags == ["heap", "arrays-hashing", "blinkit", "daily"]
    assert PrepItems(db).labels()["prep:dsa-daily"] == "Daily DSA"
    kicker = prep_view.kicker(card.type, key, card.tags, "Daily DSA")
    assert kicker == "DSA · Heap / priority queue"
    assert [s.title for s in split_steps(card.text)[1]] == list(DSA_STEPS)


async def test_with_nothing_weak_problems_go_to_the_least_covered(make):
    build, writers, _, db = make
    pack_card(db, 1, "arrays-hashing")
    pack_card(db, 2, "two-pointers")
    pack_card(db, 3, "prefix-sum", type_=DRILL_TYPE)  # a drill, not a problem
    sync = build(per_day=2, drills_per_day=0)
    assert sync.status().next_pattern == "sliding-window"
    await sync.run_once()
    # Two problems on one day get two patterns.
    assert [r.pattern for r in writers.problem_requests] == [
        "sliding-window",
        "prefix-sum",
    ]
    assert sync.store.patterns_written(DAY) == ["sliding-window", "prefix-sum"]


# --- no repeats, and correct or nothing ------------------------------------- #


async def test_a_title_already_in_the_feed_is_sent_back(make):
    build, writers, _, db = make
    pack_card(db, 1, "heap", title="Top-K Trending SKUs")
    sync = build(drills_per_day=0)
    first = problem_reply()  # "Top-K Trending SKUs" again
    writers.replies = [first, problem_reply(title="Surge Zone Ranking")]
    report = await sync.write_now(problems=1, pattern="heap")
    assert report.problems == 1 and not report.errors

    retry = writers.problem_requests[1]
    assert retry.pattern == "heap" and retry.previous == first
    assert "already in the feed" in retry.issues[0]
    assert InsightStore(db).get(item_key(DAILY_SOURCE, "Surge Zone Ranking"))


async def test_a_wrong_solution_is_retried_with_its_failing_tests(make):
    build, writers, _, db = make
    sync = build(drills_per_day=0, check=check_problem)  # the real checker
    wrong = problem_reply(solution=SOLUTION.replace("-p[1]", "p[1]"))
    writers.replies = [wrong, problem_reply()]
    report = await sync.run_once()
    assert report.problems == 1 and not report.errors

    retry = writers.problem_requests[1]
    assert retry.previous == wrong
    assert retry.issues[0] == "the optimal solution passed 2 of 4 tests"
    assert retry.issues[1].startswith("test 1 (input [[101, 101, 202], 1])")
    assert "expected [101], got [202]" in retry.issues[1]
    card = InsightStore(db).get(item_key(DAILY_SOURCE, "Top-K Trending SKUs"))
    assert "-p[1], p[0]" in card.text


async def test_a_problem_failing_twice_is_rejected_and_retried_later(make):
    build, writers, clock, db = make
    sync = build(drills_per_day=0, check=fails)
    report = await sync.run_once()
    assert report.problems == 0 and len(writers.problem_requests) == 2
    assert report.errors[0].startswith(f"problem:{DAY}:1: rejected: the optimal")
    assert not InsightStore(db).all()
    [job] = sync.store.jobs()
    assert (job.state, job.attempts) == (PENDING, 1)

    await sync.run_once()  # backing off: not retried yet
    assert len(writers.problem_requests) == 2
    for hours in (1, 2, 4)[: MAX_ATTEMPTS - 1]:  # the backoff, on the same day
        clock.now += timedelta(hours=hours, minutes=1)
        await sync.run_once()
    assert len(writers.problem_requests) == 2 * MAX_ATTEMPTS
    [job] = sync.status().failed
    assert job.state == FAILED and job.error.startswith("rejected:")


async def test_a_crashing_solution_is_reported_as_such(make):
    build, writers, _, _ = make
    sync = build(drills_per_day=0, check=check_problem)
    broken = problem_reply(
        solution="def top_k_skus(orders, k):\n    return orders[99]\n"
    )
    writers.replies = [broken, problem_reply()]
    await sync.run_once()
    assert "raised IndexError" in writers.problem_requests[1].issues[1]


# --- cards --------------------------------------------------------------------- #


def test_a_problem_card_is_its_prompt_then_the_packs_steps():
    reply = problem_reply(
        scenario="Riders wait.\n### Not a step\nStill the scenario.",
        walkthrough="Step one.\n```\nopen fence",
    )
    text = problem_card(parse_problem(reply, "heap"))
    prompt, steps = split_steps(text)
    assert prompt.startswith("**Scenario.** Riders wait.\nNot a step\n")
    for part in ("**Task.**", "```python\ndef top_k_skus", "**Constraints**"):
        assert part in prompt
    assert "**Example**\n```\nInput: orders = [101, 101, 202], k = 1" in prompt
    assert "How are ties broken? → Smaller id first." in prompt
    assert [s.title for s in steps] == list(DSA_STEPS)
    pattern = steps[0].body
    assert pattern.startswith("**Heap / priority queue + Arrays & hashing**")
    assert "LeetCode: 347. Top K Frequent Elements" in pattern
    assert "Target: O(n log k) time, O(n) space" in pattern
    assert steps[3].body == f"```python\n{SOLUTION.rstrip()}\n```"
    assert steps[4].body.endswith("open fence\n```")  # closed, so it can't leak
    assert "**Follow-ups**" in steps[5].body
    assert pattern_note(text).startswith("Heap / priority queue + Arrays & hashing")


@pytest.mark.parametrize("slot", range(4))
def test_drill_cards_follow_the_agreed_format(slot):
    drill = fake_drill("monotonic-stack")
    text = drill_card(drill, slot)
    assert check_drill_card(text) == []
    prompt, steps = split_steps(text)
    lines = prompt.splitlines()
    assert lines[:2] == drill.scenario and lines[2] == ""
    assert lines[3] == drill.question
    assert [line[:3] for line in lines[4:]] == ["A) ", "B) ", "C) ", "D) "]
    assert lines[4 + slot] == f"{'ABCD'[slot]}) Monotonic stack"
    assert [s.title for s in steps] == ["Answer", "Why not the others"]
    assert steps[0].body.splitlines() == [lines[4 + slot], *drill.why]
    why_not = steps[1].body.splitlines()
    assert len(why_not) == 3 and "Monotonic stack" not in steps[1].body
    assert why_not[0].endswith("— Arrays & hashing does not fit here.")
    # The Interview tab (#137) reads it as a drill with the right answer.
    shown = prep_view.parse_drill(text)
    assert shown is not None and shown.answer == "ABCD"[slot]
    assert shown.option(shown.answer).text == "Monotonic stack"
    assert shown.prompt == "\n".join(drill.scenario) + "\n\n" + drill.question
    assert len(shown.options) == 4 and not shown.after


GOOD = drill_card(fake_drill("heap"), 1)


@pytest.mark.parametrize(
    ("broken", "problem"),
    [
        (GOOD.replace("D) ", "E) "), "exactly four options"),
        (GOOD.replace("A) ", "C) ", 1), "exactly four options"),
        (GOOD.replace("optimal solution?", "optimal solution."), 'ending in "?"'),
        (GOOD.replace("Each picker", "One.\nTwo.\nThree.\nEach picker"), "2-4"),
        (GOOD.replace("Two pointers", "Arrays & hashing"), "four different"),
        (GOOD.replace("### Why not the others", "### Why"), '"Answer", then'),
        (GOOD.replace("### Answer\nB)", "### Answer\nB."), "one option, as listed"),
        (GOOD.replace("O(n) overall.", ""), "2-3 lines on why"),
        (GOOD.replace("\nC) Two", "\nC)Two", 1), "exactly four options"),
        (GOOD.rsplit("\n", 1)[0], "one line per wrong option"),
    ],
)
def test_the_drill_validator_catches_broken_cards(broken, problem):
    assert any(problem in p for p in check_drill_card(broken)), check_drill_card(broken)


async def test_drills_are_stored_tagged_and_lean_to_what_was_missed(make):
    build, writers, _, db = make
    missed = pack_card(db, 1, "intervals", type_=DRILL_TYPE)
    GradeStore(db).grade(missed, MISSED, now=NOW - timedelta(days=2))
    sync = build(per_day=0)
    report = await sync.run_once()
    assert report.drills == 3 and report.problems == 0

    [request] = writers.drill_requests
    assert request.patterns[0] == "intervals" and len(request.patterns) == 3
    assert request.recent_titles == ["Pack problem 1"]
    store = InsightStore(db)
    cards = [store.get(item_key(DRILL_SOURCE, f"drills:{DAY}-{n}")) for n in (1, 2, 3)]
    assert [c.tags for c in cards] == [[p, "drill"] for p in request.patterns]
    assert all(c.type == DRILL_TYPE and c.status == "web-only" for c in cards)
    assert all(check_drill_card(c.text) == [] for c in cards)
    assert PrepItems(db).labels()["prep:dsa-drills"] == "Pattern drills"
    [job] = sync.store.jobs()
    assert job.state == DONE and job.pattern == ",".join(request.patterns)


async def test_a_drill_that_breaks_the_format_is_dropped(make):
    build, writers, _, db = make
    sync = build(per_day=0, drills_per_day=2)
    bad = fake_drill("dp", 2, scenario=["One.", "Two.", "Three.", "Four.", "Five."])
    writers.drills = [[fake_drill("heap"), bad], [bad]]
    report = await sync.run_once()
    assert report.drills == 1 and report.written == 1
    await sync.write_now(drills=1)  # nothing usable: the job fails an attempt
    [failed] = [j for j in sync.store.jobs() if j.attempts]
    assert failed.state == PENDING and "right format" in failed.error
    assert len(InsightStore(db).all()) == 1


# --- pacing -------------------------------------------------------------------- #


async def test_one_days_worth_per_day_even_after_a_long_sleep(make):
    build, writers, clock, db = make
    sync = build()
    await sync.run_once()
    assert (len(writers.problem_requests), len(writers.drill_requests)) == (1, 1)
    report = await sync.run_once()  # the same day: nothing new
    assert report.written == 0 and len(writers.problem_requests) == 1

    clock.now += timedelta(days=1)
    writers.failing = True  # a usage limit all day
    report = await sync.run_once()
    assert report.written == 0 and len(report.errors) == 2

    clock.now += timedelta(days=5)  # the Mac slept for five days
    writers.failing = False
    report = await sync.run_once()
    assert (report.problems, report.drills) == (1, 3)
    assert (len(writers.problem_requests), len(writers.drill_requests)) == (3, 3)
    states = {j.id: j.state for j in sync.store.jobs()}
    assert states == {
        f"problem:{DAY}:1": DONE,
        f"drills:{DAY}": DONE,
        "problem:2026-11-26:1": SKIPPED,
        "drills:2026-11-26": SKIPPED,
        "problem:2026-12-01:1": DONE,
        "drills:2026-12-01": DONE,
    }
    status = sync.status()
    assert status.day == "2026-12-01" and (status.problems, status.drills) == (2, 6)
    assert sorted((j.kind, j.state) for j in status.today) == [
        ("drills", DONE),
        ("problem", DONE),
    ]


async def test_a_pass_writes_at_most_per_run(make):
    build, writers, _, _ = make
    sync = build(per_day=2, drills_per_day=3, per_run=1)
    report = await sync.run_once()
    assert report.written == 1 and report.remaining == 2
    report = await sync.run_once(limit=5)
    assert report.written == 2 and report.remaining == 0


async def test_days_follow_the_local_zone(make):
    build, writers, clock, _ = make
    sync = build(tz=timezone(timedelta(hours=5, minutes=30)))
    clock.now = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)  # 01:30 IST
    assert sync.today() == "2026-11-26"


async def test_extra_cards_now(make):
    build, writers, _, db = make
    sync = build()
    report = await sync.write_now(problems=1, pattern="trie", drills=2)
    assert (report.problems, report.drills) == (1, 2)
    assert writers.problem_requests[0].pattern == "trie"
    assert len(writers.drill_requests[0].patterns) == 2
    assert all(j.id.split(":")[2].startswith("x") for j in sync.store.jobs())
    with pytest.raises(ValueError, match="not a DSA pattern"):
        await sync.write_now(problems=1, pattern="segment-tree")
    # The day's own cards are still written on the next pass.
    report = await sync.run_once()
    assert (report.problems, report.drills) == (1, 3)


def test_status_before_anything_is_written(make):
    build, _, _, db = make
    pack_card(db, 1, "arrays-hashing")
    status = build().status()
    assert (status.problems, status.drills, status.today) == (0, 0, [])
    assert (status.next_pattern, status.next_reason) == (
        "two-pointers",
        "least covered",
    )
    assert "arrays-hashing" not in status.uncovered and len(status.uncovered) == 24


def test_the_job_table_is_copied_with_the_other_stores():
    assert "dsa_daily" in STORE_MODULES
    assert "prep_dsa_jobs" in {t.name for t in store_tables()}


def test_the_background_loop_runs_passes_on_its_own_thread():
    ran = threading.Event()

    class Sync:
        async def run_once(self):
            ran.set()
            return dsa_daily.DsaReport()

    thread = dsa_daily.run_in_background(Sync(), idle=60)
    assert ran.wait(5) and thread.daemon and thread.name == "dsa-daily"


# --- CLI ----------------------------------------------------------------------- #


@pytest.fixture
def run_cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "cli.db"))
    calls: list[tuple[str, object]] = []

    async def fake_problem(model, request):
        calls.append((model, request))
        return parse_problem(problem_reply(), request.pattern)

    async def fake_drills(model, request):
        calls.append((model, request))
        return [fake_drill(p, n) for n, p in enumerate(request.patterns, 1)]

    monkeypatch.setattr(dsa_writer, "write_problem", fake_problem)
    monkeypatch.setattr(dsa_writer, "write_drills", fake_drills)

    def run(*args: str):
        cli.get_settings.cache_clear()
        try:
            return CliRunner().invoke(cli.app, ["prep", "dsa", *args])
        finally:
            cli.get_settings.cache_clear()

    return run, calls


def test_cli_writes_extra_cards_and_shows_status(run_cli):
    run, calls = run_cli
    result = run("write", "--pattern", "heap", "--drills", "2")
    assert result.exit_code == 0, result.output
    assert "Wrote 1 problem(s) and 2 drill(s)" in result.output
    assert {model for model, _ in calls} == {"claude-code:claude-opus-5-5"}
    assert [r.pattern for _, r in calls if isinstance(r, ProblemRequest)] == ["heap"]

    result = run("status")
    assert result.exit_code == 0, result.output
    assert "1 daily problem(s) and 2 drill(s) in the feed." in result.output
    assert "Next problem: Two pointers (least covered)." in result.output

    result = run("write", "--pattern", "segment-tree")
    assert result.exit_code == 1 and "Unknown pattern" in result.output


def test_cli_write_with_no_options_writes_the_days_cards(run_cli):
    run, calls = run_cli
    result = run("write", "--model", "claude-code:sonnet")
    assert result.exit_code == 0, result.output
    assert "Wrote 1 problem(s) and 3 drill(s)" in result.output
    assert {model for model, _ in calls} == {"claude-code:sonnet"}
    result = run("write")
    assert "Wrote 0 problem(s) and 0 drill(s)" in result.output
