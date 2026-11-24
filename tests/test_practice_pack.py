"""DSA practice packs and talk-track notes as prep cards (issue #135).

Offline: the packs are small made-up fixtures written by each test into
tmp_path, and cards land in a tmp_path SQLite database."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.prep_grades import GOT, GradeStore
from daily_agent.feed.prep_tracks import (
    DESIGN_TYPE,
    PROBLEM_TYPE,
    PrepItems,
    Step,
    item_key,
    split_steps,
)
from daily_agent.feed.web_feed import WebFeed
from daily_agent.sources.practice_pack import (
    BRUTE_STEP,
    DSA_PATTERNS,
    DSA_STEPS,
    EDGES_STEP,
    KEY_IDEA_STEP,
    OPTIMAL_STEP,
    PATTERN_STEP,
    WALKTHROUGH_STEP,
    PackError,
    answer_step,
    interview_question,
    notes_source,
    notes_topic,
    pack_source,
    parse_index,
    pattern_slugs,
    problem_title,
    read_notes,
    read_pack,
)

FENCE = "```"

README = """# Acme — Round 1 Practice Pack

Three made-up problems. Try each problem first.

## Short on time? Do these first
**#02** and #03. They cover DP and graphs.

## Index

| # | Problem | Pattern (don't peek) | Target |
|---|---|---|---|
| 01 | [Busiest Hour](problems/01-busiest-hour.md) · [ans](answers/01-busiest-hour.md) | fixed sliding window | O(n) |
| 02 | [Cheapest Basket](problems/02-cheapest-basket.md) · [ans](answers/02-cheapest-basket.md) | 0/1 knapsack | O(n·W) |
| 03 | [Shelf Order](problems/03-shelf-order.md) | topological sort (Kahn's); DFS | O(V+E) |
| 04 | [Ghost Problem](problems/04-ghost.md) | heap | O(n log n) |

## In the room
1. **Brute force first**, then optimise.
"""

PROBLEM_1 = f"""# 01 — Busiest Hour

## Scenario
Orders arrive once a minute as counts.

## Task
Return the busiest window of `k` minutes.

## Example
{FENCE}python
### not a step: a comment inside a fence
print(busiest([1, 2, 3], 2))
{FENCE}

### Questions to ask
- Is `k` always at most n?
"""

# The usual layout: a bold preamble, then ``## `` sections.
ANSWER_1 = f"""# 01 — Busiest Hour: Answer

**Pattern:** fixed-size sliding window.
**Standard problem:** a max-sum window.

## Translating the wording
"Busiest hour" means the window with the largest sum.

## Intuition
Slide the window: add one minute, drop one.

## Brute force: O(n·k)
Sum every window.

## Optimal: O(n) time, O(1) space
{FENCE}python
def busiest(xs, k):
    ### a heading-looking comment that stays in the code
    best = cur = sum(xs[:k])
    return best
{FENCE}

### Alternative: prefix sums
Subtract two prefix sums.

## Faster in practice (mention only)
Vectorise it.

## Walkthrough: [1, 2, 3], k = 2
Windows 3, then 5.

## Edge cases
- `k` larger than n.

## Follow-ups
- Streaming input.
"""

PROBLEM_2 = """# 02. Cheapest Basket
Pick items under a weight limit for the lowest total price.
"""

# Another layout: ``### `` sections, other names, out of the usual order,
# and the file ends inside an unclosed code fence.
ANSWER_2 = f"""# Cheapest Basket (answer)

### Key idea
Each item is in or out.

### Solution
{FENCE}python
def best(items, cap):
    return 0
{FENCE}

### Pattern
0/1 knapsack.

### Dry run
Two items, capacity 3.

### Common mistakes
Looping the capacity upwards.

### Naive approach
Try every subset.
{FENCE}python
def every_subset(items):
    return []
"""

PROBLEM_3 = """# 03 — Shelf Order

## Task
Order shelves so every rule holds.
"""

ORPHAN_ANSWER = "# 05 — Orphan: Answer\n\n## Intuition\nNothing.\n"


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _pack(tmp_path: Path, **extra: str) -> Path:
    return _write(
        tmp_path / "acme-dsa-round1",
        {
            "README.md": README,
            "problems/01-busiest-hour.md": PROBLEM_1,
            "answers/01-busiest-hour.md": ANSWER_1,
            "problems/02-cheapest-basket.md": PROBLEM_2,
            "answers/02-cheapest-basket.md": ANSWER_2,
            "problems/03-shelf-order.md": PROBLEM_3,
            "answers/05-orphan.md": ORPHAN_ANSWER,
            **extra,
        },
    )


def _cards(pack) -> dict[str, object]:
    return {item.key.rsplit(":", 1)[1][:2]: item for item in pack.items}


# --- patterns ------------------------------------------------------------- #


def test_the_pattern_vocabulary_is_the_shared_one():
    assert list(DSA_PATTERNS) == [
        "arrays-hashing",
        "two-pointers",
        "sliding-window",
        "prefix-sum",
        "binary-search",
        "binary-search-on-answer",
        "stack",
        "monotonic-stack",
        "linked-list",
        "intervals",
        "heap",
        "greedy",
        "bfs",
        "dfs",
        "backtracking",
        "topological-sort",
        "union-find",
        "shortest-path",
        "trees",
        "trie",
        "dp",
        "knapsack",
        "bit-manipulation",
        "ds-design",
        "math",
    ]
    assert all(DSA_PATTERNS.values())


@pytest.mark.parametrize(
    ("text", "slugs"),
    [
        ("fixed sliding window", ["sliding-window"]),
        ("variable window, ≤ K distinct", ["sliding-window"]),
        ("two-sum hash; two pointers", ["arrays-hashing", "two-pointers"]),
        ("binary search on answer", ["binary-search-on-answer"]),
        ("binary search on time", ["binary-search-on-answer"]),
        (
            "binary search + greedy (aggressive cows)",
            ["binary-search-on-answer", "greedy"],
        ),
        ("merge intervals + bisect", ["binary-search", "intervals"]),
        ("two heaps (Meeting Rooms III)", ["intervals", "heap"]),
        ("sweep line (Meeting Rooms II)", ["intervals"]),
        (
            "time window + count map + heap",
            ["arrays-hashing", "sliding-window", "heap"],
        ),
        ("monotonic stack", ["monotonic-stack"]),
        ("balanced brackets with a stack", ["stack"]),
        (
            "LRU: hash map + doubly linked list",
            ["arrays-hashing", "linked-list", "ds-design"],
        ),
        ("hash maps + min-heap, lazy expiry", ["arrays-hashing", "heap", "ds-design"]),
        ("unbounded knapsack / coin change", ["dp", "knapsack"]),
        ("subset-sum DP, min sum ≥ target", ["dp", "knapsack"]),
        ("Dijkstra; Bellman-Ford K rounds", ["shortest-path"]),
        ("multi-source BFS by level", ["bfs"]),
        ("topological sort (Kahn's)", ["topological-sort"]),
        ("Union-Find", ["union-find"]),
        ("trie with cached top K", ["trie", "ds-design"]),
        ("prefix sums + hash map", ["arrays-hashing", "prefix-sum"]),
        ("backtracking over subsets", ["backtracking"]),
        ("inorder walk of a BST", ["trees"]),
        ("XOR of all values", ["bit-manipulation"]),
        ("gcd of the lengths", ["math"]),
        ("something new", []),
    ],
)
def test_pattern_text_maps_to_canonical_slugs(text, slugs):
    assert pattern_slugs(text) == slugs
    assert set(slugs) <= set(DSA_PATTERNS)


# --- reading a pack ----------------------------------------------------------- #


def test_the_index_gives_patterns_targets_and_the_priority_problems():
    index = parse_index(README)
    assert index.title == "Acme — Round 1 Practice Pack" and index.company == "Acme"
    assert index.must_do == {2, 3}  # "Try each problem first." names none
    first = index.rows[0]
    assert (first.number, first.name, first.stem) == (
        1,
        "Busiest Hour",
        "01-busiest-hour",
    )
    assert (first.pattern, first.target) == ("fixed sliding window", "O(n)")
    assert [r.number for r in index.rows] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("heading", "step"),
    [
        ("Pattern", PATTERN_STEP),
        ("Related problems", PATTERN_STEP),
        ("Translating the wording", KEY_IDEA_STEP),
        ("Intuition: A", KEY_IDEA_STEP),
        ("Key idea", KEY_IDEA_STEP),
        ("Brute force 2: DP, O(n²), a good middle answer", BRUTE_STEP),
        ("Naive approach", BRUTE_STEP),
        ("Optimal (b): O(X · k) time", OPTIMAL_STEP),
        ("Optimal for reads: trie with a cached top K", OPTIMAL_STEP),
        ("Solution", OPTIMAL_STEP),
        ("Walkthrough B: [1, 2, 4], M = 3", WALKTHROUGH_STEP),
        ("Dry run", WALKTHROUGH_STEP),
        ("2. Edge cases", EDGES_STEP),
        ("Follow-ups (system-design flavour)", EDGES_STEP),
        ("Common mistakes", EDGES_STEP),
        ("Scaling notes (store-flavoured follow-ups)", EDGES_STEP),
        ("**Edge cases**", EDGES_STEP),
        # Unknown: the section stays with the step before it.
        ("Faster reads (mention, code only if asked)", None),
        ("Middle answer: sort once, then bisect", None),
        ("Quickselect (average O(n))", None),
    ],
)
def test_answer_headings_map_to_steps(heading, step):
    assert answer_step(heading) == step


def test_problem_titles_drop_the_number():
    assert problem_title("10 — Top-K Trending SKUs") == "Top-K Trending SKUs"
    assert problem_title("02. Cheapest Basket") == "Cheapest Basket"
    assert problem_title("3Sum Closest") == "3Sum Closest"
    assert problem_title("07 - Riders: Answer") == "Riders"


def test_a_pack_becomes_one_card_per_problem(tmp_path):
    pack = read_pack(_pack(tmp_path), "dsa-acme")
    assert (pack.label, pack.tags, pack.must_do) == ("Acme DSA pack", ["acme"], 2)
    cards = _cards(pack)
    assert sorted(cards) == ["01", "02", "03"]
    one = cards["01"]
    assert one.key == item_key("dsa-acme", "01-busiest-hour")
    assert (one.type, one.title) == (PROBLEM_TYPE, "Busiest Hour")
    assert one.tags == ["sliding-window", "acme"]

    prompt, steps = split_steps(one.text)
    # The statement without its H1; its headings read as bold lines, and the
    # code fence (with a "###" line in it) is untouched.
    assert prompt.startswith("**Scenario**\nOrders arrive")
    assert "Busiest Hour" not in prompt and "**Questions to ask**" in prompt
    assert f"{FENCE}python\n### not a step: a comment inside a fence\n" in prompt
    assert [s.title for s in steps] == list(DSA_STEPS)
    body = {s.title: s.body for s in steps}
    assert body[PATTERN_STEP] == (
        "**Pattern:** fixed-size sliding window.\n"
        "**Standard problem:** a max-sum window.\n\n"
        "**Target complexity:** O(n)"
    )
    assert body[KEY_IDEA_STEP] == (
        "**Translating the wording**\n\n"
        '"Busiest hour" means the window with the largest sum.\n\n'
        "**Intuition**\n\nSlide the window: add one minute, drop one."
    )
    assert body[BRUTE_STEP] == "**Brute force: O(n·k)**\n\nSum every window."
    optimal = body[OPTIMAL_STEP]
    assert optimal.startswith("**Optimal: O(n) time, O(1) space**\n\n")
    assert (
        f"{FENCE}python\ndef busiest(xs, k):\n"
        "    ### a heading-looking comment that stays in the code\n"
    ) in optimal
    # A sub-heading and an unknown section stay in the step they follow.
    assert "**Alternative: prefix sums**\nSubtract two prefix sums." in optimal
    assert optimal.endswith("**Faster in practice (mention only)**\n\nVectorise it.")
    assert body[WALKTHROUGH_STEP].startswith("**Walkthrough: [1, 2, 3], k = 2**")
    assert body[EDGES_STEP] == (
        "**Edge cases**\n\n- `k` larger than n.\n\n**Follow-ups**\n\n- Streaming input."
    )


def test_answers_in_another_layout_still_fill_the_steps(tmp_path):
    two = _cards(read_pack(_pack(tmp_path), "dsa-acme"))["02"]
    assert two.title == "Cheapest Basket"
    assert two.tags == ["dp", "knapsack", "acme", "must-do"]
    prompt, steps = split_steps(two.text)
    assert prompt == "Pick items under a weight limit for the lowest total price."
    # Back in the usual order, whatever order the file used.
    assert [s.title for s in steps] == list(DSA_STEPS)
    body = {s.title: s.body for s in steps}
    assert body[PATTERN_STEP] == "0/1 knapsack.\n\n**Target complexity:** O(n·W)"
    assert body[KEY_IDEA_STEP] == "Each item is in or out."
    # The file ended inside a code fence: it's closed, so the steps after it
    # in the card still split.
    assert body[BRUTE_STEP] == (
        f"**Naive approach**\n\nTry every subset.\n{FENCE}python\n"
        f"def every_subset(items):\n    return []\n{FENCE}"
    )
    assert body[OPTIMAL_STEP].startswith(f"{FENCE}python\ndef best(items, cap):")
    assert body[WALKTHROUGH_STEP] == "Two items, capacity 3."
    assert body[EDGES_STEP] == "**Common mistakes**\n\nLooping the capacity upwards."


def test_a_pack_reports_what_it_could_not_match(tmp_path):
    pack = read_pack(_pack(tmp_path), "dsa-acme")
    three = _cards(pack)["03"]
    assert three.tags == ["dfs", "topological-sort", "acme", "must-do"]
    assert split_steps(three.text) == (
        "**Task**\nOrder shelves so every rule holds.",
        [],
    )
    assert pack.warnings == [
        "03-shelf-order.md: no answer file",
        "answers/05-orphan.md: no matching problem",
        "index row 4: no problem file",
    ]


def test_a_pack_without_an_index_reads_patterns_from_the_answers(tmp_path):
    root = _write(
        tmp_path / "bare",
        {
            "problems/1-alpha.md": PROBLEM_1,
            "answers/1-alpha.md": ANSWER_1,
        },
    )
    pack = read_pack(root, pack_source(root), tags=["Mock Round"])
    (card,) = pack.items
    assert card.key == "prep:dsa-bare:1-alpha"
    assert card.tags == ["sliding-window", "mock-round"]
    assert pack.label == "bare"
    assert pack.warnings == ["no README.md: no index, so no pattern tags from it"]
    _, steps = split_steps(card.text)
    assert steps[0] == Step(
        PATTERN_STEP,
        "**Pattern:** fixed-size sliding window.\n"
        "**Standard problem:** a max-sum window.",
    )


def test_not_a_pack(tmp_path):
    with pytest.raises(PackError):
        read_pack(tmp_path / "missing", "x")
    (tmp_path / "empty").mkdir()
    with pytest.raises(PackError):
        read_pack(tmp_path / "empty", "x")


# --- talk-track notes ----------------------------------------------------------- #

NOTES = f"""# URL shortener: interview talk track

Lead with the API, then storage.

## 1. Clarify first (2 min)
- Custom aliases? Expiry?

## 2. Storage
| Store | Why |
|---|---|
| KV | fast lookups |

### Sharding
By a hash of the short code.

## 3. Code points
- Base62 the id:
  {FENCE}python
  ### stays: inside a fence
  def encode(n): ...
  {FENCE}
"""


def test_notes_topic_and_question():
    assert notes_topic("Rate limiter: interview talk track") == "Rate limiter"
    assert notes_topic("Chat system — notes") == "Chat system"
    assert notes_topic("Caching cheat sheet") == "Caching"
    assert notes_topic("Consistent hashing") == "Consistent hashing"
    assert interview_question("Rate limiter") == (
        "Design a rate limiter. Talk me through it."
    )
    assert interview_question("URL shortener").startswith("Design a URL shortener.")
    assert interview_question("API gateway").startswith("Design an API gateway.")
    assert interview_question("Event bus").startswith("Design an event bus.")
    assert interview_question("design a chat app") == (
        "Design a chat app. Talk me through it."
    )
    assert interview_question("How does Kafka scale?") == "How does Kafka scale?"


def test_a_notes_file_becomes_one_card(tmp_path):
    path = _write(tmp_path, {"url_shortener_notes.md": NOTES}) / (
        "url_shortener_notes.md"
    )
    assert notes_source(path) == "notes-url-shortener"
    notes = read_notes(path, "notes-url-shortener", tags=["storage"])
    card = notes.item
    assert card.key == "prep:notes-url-shortener:url-shortener-notes"
    assert (card.type, card.title) == (
        DESIGN_TYPE,
        "URL shortener: interview talk track",
    )
    assert card.tags == ["storage", "url-shortener"]
    prompt, steps = split_steps(card.text)
    assert prompt == "Design a URL shortener. Talk me through it."
    assert [s.title for s in steps] == [
        "Overview",
        "Clarify first (2 min)",
        "Storage",
        "Code points",
    ]
    assert steps[0].body == "Lead with the API, then storage."
    assert steps[2].body == (
        "| Store | Why |\n|---|---|\n| KV | fast lookups |\n\n"
        "**Sharding**\nBy a hash of the short code."
    )
    assert f"  {FENCE}python\n  ### stays: inside a fence\n" in steps[3].body

    asked = read_notes(path, "n", question="Shorten links at scale?", track="dsa")
    assert split_steps(asked.item.text)[0] == "Shorten links at scale?"
    assert asked.item.type == PROBLEM_TYPE
    with pytest.raises(PackError):
        read_notes(path, "n", track="cooking")
    empty = _write(tmp_path, {"empty.md": "# Empty\n"}) / "empty.md"
    with pytest.raises(PackError):
        read_notes(empty, "n")


# --- CLI: import, re-import, keep reader state -------------------------------- #


@pytest.fixture
def run(tmp_path, monkeypatch):
    db = tmp_path / "prep.db"
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(db))
    cli.get_settings.cache_clear()

    def invoke(*args: str):
        result = CliRunner().invoke(cli.app, ["prep", *args])
        result.text = " ".join(result.output.split())
        return result

    yield invoke, db
    cli.get_settings.cache_clear()


def test_import_pack_is_rerunnable_and_keeps_reader_state(run, tmp_path):
    invoke, db = run
    folder = _pack(tmp_path)
    args = ["import-pack", str(folder), "--source", "dsa-acme-r1"]

    first = invoke(*args)
    assert first.exit_code == 0, first.output
    assert "Acme DSA pack (dsa-acme-r1): 3 problem(s), 2 must-do." in first.text
    assert "3 added, 0 updated, 0 removed" in first.text
    assert "03-shelf-order.md: no answer file" in first.text
    key = item_key("dsa-acme-r1", "01-busiest-hour")
    store = InsightStore(db)
    added_at = store.get(key).captured_at
    assert WebFeed(db).entry(key).source == "Acme DSA pack"

    again = invoke(*args)
    assert "0 added, 0 updated, 0 removed" in again.text

    GradeStore(db).grade(key, GOT)
    answer = folder / "answers/01-busiest-hour.md"
    answer.write_text(ANSWER_1.replace("Sum every window.", "Sum each window."))
    (folder / "problems/03-shelf-order.md").unlink()
    edited = invoke(*args)
    assert "2 problem(s)" in edited.text
    assert "0 added, 1 updated, 1 removed" in edited.text
    card = store.get(key)
    assert "Sum each window." in card.text and card.captured_at == added_at
    assert GradeStore(db).latest([key])[key].grade == GOT
    assert store.get(item_key("dsa-acme-r1", "03-shelf-order")) is None


def test_import_pack_options_and_errors(run, tmp_path):
    invoke, db = run
    folder = _pack(tmp_path)
    result = invoke(
        "import-pack", str(folder), "--label", "Mock pack", "--tags", "mock, round-1"
    )
    assert result.exit_code == 0, result.output
    assert PrepItems(db).labels() == {"prep:dsa-acme-dsa-round1": "Mock pack"}
    card = InsightStore(db).get(item_key("dsa-acme-dsa-round1", "01-busiest-hour"))
    assert card.tags == ["sliding-window", "acme", "mock", "round-1"]

    # A folder with no problems is an error, and leaves the source's cards alone.
    (tmp_path / "empty").mkdir()
    failed = invoke(
        "import-pack", str(tmp_path / "empty"), "--source", "dsa-acme-dsa-round1"
    )
    assert failed.exit_code == 1 and "no problems/*.md" in failed.text
    assert InsightStore(db).get(card.key) is not None


def test_import_notes_is_rerunnable(run, tmp_path):
    invoke, db = run
    path = (
        _write(tmp_path, {"url_shortener_notes.md": NOTES}) / "url_shortener_notes.md"
    )
    first = invoke("import-notes", str(path), "--tags", "storage, caching")
    assert first.exit_code == 0, first.output
    assert (
        "URL shortener: interview talk track (notes-url-shortener, System design): "
        '"Design a URL shortener. Talk me through it." + 4 step(s). '
        "1 added, 0 updated, 0 removed."
    ) in first.text
    key = item_key("notes-url-shortener", "url_shortener_notes")
    card = InsightStore(db).get(key)
    assert card.type == DESIGN_TYPE
    assert card.tags == ["storage", "caching", "url-shortener"]
    assert PrepItems(db).labels() == {"prep:notes-url-shortener": "Interview notes"}

    again = invoke("import-notes", str(path), "--tags", "storage, caching")
    assert "0 added, 0 updated, 0 removed" in again.text
    asked = invoke(
        "import-notes",
        str(path),
        "--tags",
        "storage, caching",
        "--question",
        "Design TinyURL.",
        "--label",
        "My notes",
    )
    assert "0 added, 1 updated, 0 removed" in asked.text
    assert split_steps(InsightStore(db).get(key).text)[0] == "Design TinyURL."
    assert PrepItems(db).labels() == {"prep:notes-url-shortener": "My notes"}

    bad = invoke("import-notes", str(path), "--track", "cooking")
    assert bad.exit_code == 1 and "unknown track" in bad.text
