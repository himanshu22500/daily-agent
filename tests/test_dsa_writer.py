"""The DSA problem and drill writer (#138): the Claude Code call is faked; offline."""

from __future__ import annotations

import copy
import json

import pytest

from daily_agent.agents import dsa_writer
from daily_agent.agents.claude_code import ClaudeCodeError, ClaudeResult
from daily_agent.agents.dsa_writer import (
    DRILL_PROMPT,
    MAX_DRILLS,
    MAX_TESTS,
    MIN_TESTS,
    PROBLEM_PROMPT,
    PROBLEM_SCHEMA,
    DrillRequest,
    ProblemRequest,
    clean_code,
    clean_line,
    drill_schema,
    names_pattern,
    parse_drill,
    parse_problem,
    render_problem_request,
    write_drills,
    write_problem,
)
from daily_agent.feed.dsa_check import ANY_ORDER, CLASS, EXACT, FUNCTION, Case
from daily_agent.sources.practice_pack import DSA_PATTERNS

MODEL = "claude-code:claude-opus-5-5"

SOLUTION = """\
import heapq
from collections import Counter


def top_k_skus(orders: list[int], k: int) -> list[int]:
    counts = Counter(orders)
    return [s for s, _ in heapq.nsmallest(k, counts.items(), key=lambda p: (-p[1], p[0]))]
"""


def problem_reply(**changes) -> dict:
    """A reply as ``claude -p`` returns it for PROBLEM_SCHEMA."""
    reply = {
        "title": '"Top-K Trending SKUs."',
        "scenario": "A dark store wants the SKUs that sold most this hour.",
        "task": "Return the `k` SKU ids with the most orders, most first.",
        "signature": "def top_k_skus(orders: list[int], k: int) -> list[int]:",
        "constraints": ["1 <= len(orders) <= 10^5", "  1 <= k <= 100  ", ""],
        "examples": [
            {
                "input": "orders = [101, 101, 202], k = 1",
                "output": "[101]",
                "explanation": "101 sold twice.",
            },
            {"input": "", "output": "[1]", "explanation": "dropped: no input"},
        ],
        "clarifying_questions": [
            {"question": "How are ties broken?", "answer": "Smaller id first."},
            {"question": "", "answer": "dropped"},
        ],
        "leetcode": ["347. Top K Frequent Elements"],
        "also_patterns": ["arrays-hashing", "heap", "not-a-pattern", "arrays-hashing"],
        "time_complexity": "O(n log k)",
        "space_complexity": "O(n)",
        "key_idea": "Count, then keep the best k.",
        "brute_force": "Sort every SKU by count: O(n log n).",
        "solution": f"```python\n{SOLUTION}```",
        "entry": "top_k_skus",
        "entry_kind": "function",
        "test_adapter": "",
        "compare": "exact",
        "walkthrough": "Count: {101: 2, 202: 1}. Keep 101.",
        "edge_cases": ["k equals the number of SKUs: return them all."],
        "follow_ups": [{"question": "A stream?", "answer": "Sliding window."}],
        "tests": [
            {"input": "[[101, 101, 202], 1]", "expected": "[101]"},
            {"input": "[[7], 1]", "expected": "[7]"},
            {"input": "[[5, 5, 4, 4, 9], 2]", "expected": "[4, 5]"},
            {"input": "[[1, 2, 3], 3]", "expected": "[1, 2, 3]"},
        ],
    }
    reply.update(changes)
    return reply


def drill_item(pattern: str = "monotonic-stack", **changes) -> dict:
    item = {
        "pattern": pattern,
        "title": "Next pricier SKU on the shelf",
        "scenario": [
            "A picker walks a shelf of n SKUs, left to right, n up to 10^5.",
            "For each SKU, find the next one to its right with a higher price.",
        ],
        "question": "Which pattern gives the optimal solution?",
        "distractors": [
            {"pattern": "two-pointers", "why_not": "Nothing to move inwards."},
            {"pattern": "heap", "why_not": "O(n log n) and loses the order."},
            {"pattern": "sliding-window", "why_not": "No fixed window to slide."},
        ],
        "why": [
            '"Next greater to the right" is the signal.',
            "Each SKU is pushed and popped once: O(n).",
        ],
    }
    item.update(changes)
    return item


@pytest.fixture
def claude(monkeypatch):
    calls: list[dict] = []
    reply: dict = {}

    async def fake_run_claude(
        prompt, *, model, system_prompt, json_schema=None, timeout=None, **_
    ):
        calls.append(
            {
                "prompt": prompt,
                "model": model,
                "system": system_prompt,
                "schema": json_schema,
                "timeout": timeout,
            }
        )
        return ClaudeResult(text="", structured=reply.get("value"), cost_usd=0.05)

    monkeypatch.setattr(dsa_writer, "run_claude", fake_run_claude)
    return calls, reply


# --- problems -------------------------------------------------------------- #


async def test_writes_a_clean_problem(claude):
    calls, reply = claude
    reply["value"] = problem_reply()
    request = ProblemRequest(
        pattern="heap",
        existing=[("Rider Shift Overlaps", "Heap / priority queue"), ("Slots", "")],
    )
    problem = await write_problem(MODEL, request)

    assert problem.title == "Top-K Trending SKUs"
    assert problem.pattern == "heap" and problem.entry == "top_k_skus"
    assert problem.solution == SOLUTION.rstrip()  # the fence is gone
    assert problem.code_under_test == problem.solution
    assert problem.constraints == ["1 <= len(orders) <= 10^5", "1 <= k <= 100"]
    assert [e.input for e in problem.examples] == ["orders = [101, 101, 202], k = 1"]
    assert problem.questions == [("How are ties broken?", "Smaller id first.")]
    assert problem.also_patterns == ["arrays-hashing"]  # known, not the pattern
    assert problem.tests[0] == Case([[101, 101, 202], 1], [101])
    assert (problem.entry_kind, problem.compare) == (FUNCTION, EXACT)
    assert problem.raw is reply["value"]  # sent back as is on a retry

    [call] = calls
    assert call["model"] == MODEL and call["system"] == PROBLEM_PROMPT
    assert call["schema"] is PROBLEM_SCHEMA
    assert call["timeout"] == dsa_writer.PROBLEM_TIMEOUT_SECONDS
    payload = json.loads(call["prompt"])
    assert payload == {
        "pattern": "heap",
        "pattern_name": "Heap / priority queue",
        "difficulty": "LeetCode medium",
        "existing_problems": [
            {"title": "Rider Shift Overlaps", "pattern_note": "Heap / priority queue"},
            {"title": "Slots"},
        ],
    }


def test_a_retry_carries_the_failed_attempt_and_its_issues():
    previous = problem_reply()
    payload = json.loads(
        render_problem_request(
            ProblemRequest(
                pattern="heap",
                previous=previous,
                issues=["the optimal solution passed 3 of 4 tests", "test 4: ..."],
            )
        )
    )
    assert payload["previous_attempt"] == previous
    assert payload["issues"][0] == "the optimal solution passed 3 of 4 tests"
    many = [(f"Problem {n}", "") for n in range(100)]
    payload = json.loads(render_problem_request(ProblemRequest("heap", many)))
    assert len(payload["existing_problems"]) == dsa_writer.MAX_EXISTING


def test_design_problems_and_adapters():
    calls = [["ShelfCache", [2]], ["put", [1, 1]], ["get", [1]]]
    reply = problem_reply(
        entry="ShelfCache",
        entry_kind="class",
        compare="any_order",
        test_adapter="```python\ndef run(xs):\n    return xs\n```",
        tests=[
            {"input": json.dumps(calls), "expected": "[null, null, 1]"}
            for _ in range(MIN_TESTS)
        ],
    )
    problem = parse_problem(reply, "ds-design")
    assert problem.entry_kind == CLASS and problem.compare == ANY_ORDER
    assert problem.tests[0] == Case(calls, [None, None, 1])
    assert problem.code_under_test.endswith("\n\n\ndef run(xs):\n    return xs\n")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"tests": [{"input": "[1]", "expected": "1"}]}, "had 1 tests"),
        ({"tests": [{"input": "[[1], 1", "expected": "1"}] * 4}, "isn't valid JSON"),
        ({"tests": [{"input": '{"k": 1}', "expected": "1"}] * 4}, "isn't a JSON array"),
        (
            {
                "entry_kind": "class",
                "tests": [{"input": "[[1]]", "expected": "[]"}] * 4,
            },
            "a class test is a list of",
        ),
        ({"solution": "```python\n```"}, "had no solution"),
        ({"entry": "top k"}, "isn't a Python name"),
        ({"key_idea": "  "}, "had no key_idea"),
        ({"examples": [{"input": "", "output": "1"}]}, "had no example"),
    ],
)
def test_unusable_problem_replies_are_errors(changes, message):
    with pytest.raises(ClaudeCodeError, match=message):
        parse_problem(problem_reply(**changes), "heap")


def test_a_reply_that_is_not_an_object_is_an_error():
    with pytest.raises(ClaudeCodeError):
        parse_problem(None, "heap")


async def test_only_claude_code_and_known_patterns(claude):
    with pytest.raises(ValueError, match="Claude Code only"):
        await write_problem("google:gemini-3.8-flash", ProblemRequest("heap"))
    with pytest.raises(ValueError, match="not a DSA pattern"):
        await write_problem(MODEL, ProblemRequest("segment-tree"))
    with pytest.raises(ValueError, match="Claude Code only"):
        await write_drills("google:gemini-3.8-flash", DrillRequest(["heap"]))
    with pytest.raises(ValueError, match="drills per call"):
        await write_drills(MODEL, DrillRequest([]))
    with pytest.raises(ValueError, match="drills per call"):
        await write_drills(MODEL, DrillRequest(list(DSA_PATTERNS)[: MAX_DRILLS + 1]))
    assert claude[0] == []  # nothing reached Claude


def test_limits_live_in_the_schemas():
    props = PROBLEM_SCHEMA["properties"]
    assert set(PROBLEM_SCHEMA["required"]) == set(props)
    assert PROBLEM_SCHEMA["additionalProperties"] is False
    assert (props["tests"]["minItems"], props["tests"]["maxItems"]) == (
        MIN_TESTS,
        MAX_TESTS,
    )
    assert props["also_patterns"]["items"]["enum"] == list(DSA_PATTERNS)
    assert props["title"]["maxLength"] <= 70
    drills = drill_schema(3)["properties"]["drills"]
    assert drills["minItems"] == drills["maxItems"] == 3
    item = drills["items"]["properties"]
    assert item["pattern"]["enum"] == list(DSA_PATTERNS)
    assert item["distractors"]["minItems"] == item["distractors"]["maxItems"] == 3
    assert (item["scenario"]["minItems"], item["scenario"]["maxItems"]) == (2, 4)
    # The prompts carry the whole shared vocabulary.
    for slug, name in DSA_PATTERNS.items():
        assert f"- {slug}: {name}" in PROBLEM_PROMPT
        assert f"- {slug}: {name}" in DRILL_PROMPT


def test_cleaning_helpers():
    assert clean_code("```python\nx = 1\n```") == "x = 1"
    assert clean_code("```\nx = 1\n\n```\n") == "x = 1"
    assert clean_code("  x = 1\n\n") == "  x = 1"  # indentation is code
    assert clean_line("B)  Monotonic   stack") == "Monotonic stack"
    assert clean_line("### Answer") == "Answer"
    assert clean_line("A picker waits") == "A picker waits"  # not an option


# --- drills ---------------------------------------------------------------- #


async def test_writes_drills_in_one_call(claude):
    calls, reply = claude
    reply["value"] = {
        "drills": [
            drill_item("monotonic-stack"),
            drill_item(
                "binary-search-on-answer",
                title="Smallest rider fleet",
                distractors=[
                    {"pattern": "greedy", "why_not": "No safe local choice."},
                    {"pattern": "dp", "why_not": "No overlapping subproblems."},
                    {"pattern": "heap", "why_not": "Doesn't find a minimum capacity."},
                ],
            ),
            drill_item("monotonic-stack", title="A second stack drill"),  # dropped
        ]
    }
    request = DrillRequest(
        ["monotonic-stack", "binary-search-on-answer", "intervals"],
        recent_titles=["Old drill"],
    )
    drills = await write_drills(MODEL, request)
    assert [d.pattern for d in drills] == ["monotonic-stack", "binary-search-on-answer"]
    first = drills[0]
    assert first.title == "Next pricier SKU on the shelf"
    assert first.question.endswith("?") and len(first.scenario) == 2
    assert first.distractors[1] == ("heap", "O(n log n) and loses the order.")

    [call] = calls
    assert call["system"] == DRILL_PROMPT and call["schema"] == drill_schema(3)
    assert json.loads(call["prompt"]) == {
        "targets": [
            {"pattern": "monotonic-stack", "name": "Monotonic stack"},
            {
                "pattern": "binary-search-on-answer",
                "name": "Binary search on the answer",
            },
            {"pattern": "intervals", "name": "Intervals"},
        ],
        "recent_titles": ["Old drill"],
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"pattern": "segment-tree"},
        {"title": "Monotonic stack of prices"},  # gives the answer away
        {"title": ""},
        {"scenario": ["Only one line."]},
        {"question": "Which pattern gives the optimal solution"},
        {"why": ["Just one line."]},
        {"distractors": drill_item()["distractors"][:2]},
        {
            "distractors": [
                {"pattern": "heap", "why_not": "x" * 12},
                {"pattern": "heap", "why_not": "y" * 12},
                {"pattern": "dp", "why_not": "z" * 12},
            ]
        },
        {
            "distractors": [
                {"pattern": "monotonic-stack", "why_not": "x" * 12},
                {"pattern": "heap", "why_not": "y" * 12},
                {"pattern": "dp", "why_not": "z" * 12},
            ]
        },
        {
            "distractors": [
                {"pattern": "segment-tree", "why_not": "x" * 12},
                {"pattern": "heap", "why_not": "y" * 12},
                {"pattern": "dp", "why_not": "z" * 12},
            ]
        },
        {
            "distractors": [
                {"pattern": "greedy", "why_not": ""},
                {"pattern": "heap", "why_not": "y" * 12},
                {"pattern": "dp", "why_not": "z" * 12},
            ]
        },
    ],
)
def test_unfair_drills_are_dropped(changes):
    assert parse_drill(drill_item(**copy.deepcopy(changes))) is None


def test_drill_lines_cannot_pose_as_options_or_steps():
    drill = parse_drill(
        drill_item(
            scenario=["A) A picker walks a shelf.", "### Next pricier SKU, n <= 10^5."],
            why=["B) Next greater is the signal.", "O(n)."],
        )
    )
    assert drill.scenario == ["A picker walks a shelf.", "Next pricier SKU, n <= 10^5."]
    assert drill.why[0] == "Next greater is the signal."


async def test_a_reply_with_no_usable_drill_is_an_error(claude):
    _, reply = claude
    reply["value"] = {"drills": [drill_item(question="No question mark")]}
    with pytest.raises(ClaudeCodeError, match="no usable drill"):
        await write_drills(MODEL, DrillRequest(["monotonic-stack"]))
    reply["value"] = None
    with pytest.raises(ClaudeCodeError):
        await write_drills(MODEL, DrillRequest(["monotonic-stack"]))


def test_names_pattern_matches_whole_words():
    assert names_pattern("Rider heap", "heap")
    assert names_pattern("A priority queue of orders", "heap")
    assert not names_pattern("Cheapest basket", "heap")
    assert names_pattern("Sliding window over carts", "sliding-window")
    assert names_pattern("Two pointers on a shelf", "two-pointers")
    assert names_pattern("Some DP for slots", "dp")
    assert not names_pattern("Rider shift overlaps", "intervals")
