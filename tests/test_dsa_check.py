"""The DSA solution checker (#138): a written solution runs only in an isolated
child Python. Offline: the child is the local interpreter, no network used."""

from __future__ import annotations

from daily_agent.feed.dsa_check import (
    ANY_ORDER,
    CLASS,
    Case,
    check_solution,
    matches,
    same,
)

TOP_K = """
import heapq
from collections import Counter


def top_k_skus(orders: list[int], k: int) -> list[int]:
    counts = Counter(orders)
    # Most orders first; the smaller SKU id wins a tie.
    best = heapq.nsmallest(k, counts.items(), key=lambda p: (-p[1], p[0]))
    return [sku for sku, _ in best]
"""
TESTS = [
    Case([[101, 101, 101, 202, 202, 303], 2], [101, 202]),
    Case([[7], 1], [7]),
    Case([[5, 5, 4, 4, 9], 2], [4, 5]),
]


async def test_a_correct_solution_passes():
    result = await check_solution(TOP_K, TESTS, entry="top_k_skus")
    assert (
        result.ok and result.passed == 3 and result.summary() == ("all 3 tests passed")
    )


async def test_a_wrong_solution_fails_with_the_failing_tests():
    wrong = TOP_K.replace("-p[1], p[0]", "p[1], p[0]")
    result = await check_solution(wrong, TESTS, entry="top_k_skus")
    assert not result.ok and result.passed == 1 and not result.error
    assert result.failures[0].startswith("test 1 (input [[101, 101")
    assert "expected [101, 202], got [303, 202]" in result.failures[0]


async def test_errors_and_missing_entries_are_reported():
    crash = await check_solution(
        "def f(xs):\n    return xs[10]\n", [Case([[1]], 1)], entry="f"
    )
    assert crash.failures == [
        "test 1 (input [[1]]) raised IndexError: list index out of range"
    ]
    missing = await check_solution("def g():\n    return 1\n", [Case([], 1)], entry="f")
    assert missing.error.endswith("NameError: the solution defines no 'f'")
    broken = await check_solution("def f(:\n", [Case([], 1)], entry="f")
    assert broken.error.startswith("the solution doesn't load: SyntaxError")
    assert (await check_solution(TOP_K, [], entry="top_k_skus")).error


async def test_a_runaway_solution_is_killed():
    result = await check_solution(
        "def f():\n    while True:\n        pass\n", [Case([], 1)], entry="f", timeout=1
    )
    assert result.error == "the solution timed out after 1s" and not result.ok


async def test_the_child_has_no_network_files_or_repo():
    probes = {
        "net": "import socket\ndef f():\n    socket.socket()\n",
        "read": "def f():\n    return open('/etc/hosts').read()\n",
        "write": "def f():\n    open('out.txt', 'w').write('x')\n",
        "proc": "import os\ndef f():\n    return os.system('true')\n",
    }
    for name, code in probes.items():
        result = await check_solution(code, [Case([], None)], entry="f")
        assert "PermissionError" in result.failures[0], name
    repo = await check_solution(
        "import daily_agent\ndef f():\n    return 1\n", [Case([], 1)], entry="f"
    )
    assert "No module named 'daily_agent'" in repo.error
    paths = await check_solution(
        "import sys\ndef f():\n    return [p for p in sys.path if 'site-packages' "
        "in p or 'daily' in p]\n",
        [Case([], [])],
        entry="f",
    )
    assert paths.ok


async def test_prints_do_not_confuse_the_result():
    code = "def f(x):\n    print('@@dsa-check@@ {}')\n    return x * 2\n"
    assert (await check_solution(code, [Case([2], 4)], entry="f")).ok


async def test_a_design_problem_runs_its_calls_on_one_object():
    lru = """
from collections import OrderedDict


class ShelfCache:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.items: OrderedDict[int, int] = OrderedDict()

    def get(self, key: int) -> int:
        if key not in self.items:
            return -1
        self.items.move_to_end(key)
        return self.items[key]

    def put(self, key: int, value: int) -> None:
        self.items[key] = value
        self.items.move_to_end(key)
        if len(self.items) > self.capacity:
            self.items.popitem(last=False)
"""
    calls = [["ShelfCache", [2]], ["put", [1, 1]], ["put", [2, 2]], ["get", [1]]]
    calls += [["put", [3, 3]], ["get", [2]]]
    result = await check_solution(
        lru,
        [Case(calls, [None, None, None, 1, None, -1])],
        entry="ShelfCache",
        kind=CLASS,
    )
    assert result.ok


async def test_any_order_and_tuples():
    code = "def f():\n    return [(2, 1), (1, 0)]\n"
    exact = await check_solution(code, [Case([], [[1, 0], [2, 1]])], entry="f")
    assert not exact.ok
    loose = await check_solution(
        code, [Case([], [[1, 0], [2, 1]])], entry="f", compare=ANY_ORDER
    )
    assert loose.ok


def test_comparison_rules():
    assert same(0.1 + 0.2, 0.3) and same(2, 2.0)
    assert not same(True, 1) and not same(1, True)
    assert not same([1, 2], [1, 2, 3]) and same({"a": [1.0]}, {"a": [1]})
    assert matches([[2], [1]], [[1], [2]], ANY_ORDER)
    assert not matches([[2], [1]], [[1], [2]])
