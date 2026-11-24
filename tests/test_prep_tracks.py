"""Prep tracks, the shared prep-card store, and self-grading (issue #130).
Offline: tmp_path SQLite (or Postgres schemas in CI)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.prep_grades import GOT, MISSED, SHAKY, GradeStore, next_gap
from daily_agent.feed.prep_tracks import (
    CARD_TYPE,
    DESIGN,
    DESIGN_TYPE,
    DRILL_TYPE,
    DSA,
    PREP_TYPES,
    PROBLEM_TYPE,
    RESUME,
    PrepItem,
    PrepItems,
    Step,
    item_key,
    join_steps,
    split_steps,
    track_of,
    types_for,
)
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import Insight

T0 = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)

PROBLEM = """Purchase events stream in as `(sku, t)`.

```python
### not a step: inside a code fence
```

### Pattern
Sliding time window + count map + heap.

### Complexity
O(U log k) per query.
"""


def test_every_prep_type_has_one_track():
    assert PREP_TYPES == ("interview", "concept", "dsa", "drill", "design")
    assert [track_of(t) for t in PREP_TYPES] == [
        RESUME,
        RESUME,
        DSA,
        DSA,
        DESIGN,
    ]
    assert track_of("gotcha") is None and types_for(DSA) == ("dsa", "drill")


def test_steps_split_on_headings_outside_code():
    prompt, steps = split_steps(PROBLEM)
    assert prompt.startswith("Purchase events") and "not a step" in prompt
    assert steps == [
        Step("Pattern", "Sliding time window + count map + heap."),
        Step("Complexity", "O(U log k) per query."),
    ]
    assert split_steps(join_steps(prompt, steps)) == (prompt, steps)
    assert split_steps("Just a question?") == ("Just a question?", [])


def test_keys_are_path_safe():
    assert item_key("DSA Blinkit R1", "10 — Top-K Trending SKUs!") == (
        "prep:dsa-blinkit-r1:10-top-k-trending-skus"
    )


def _problem(n: int, **kw) -> PrepItem:
    return PrepItem(
        key=item_key("dsa-pack", f"p{n}"),
        type=PROBLEM_TYPE,
        title=f"Problem {n}",
        text=PROBLEM,
        tags=["Sliding Window", "heap", "heap"],
        **kw,
    )


def test_put_adds_updates_and_replaces_its_own_cards(tmp_path):
    db = tmp_path / "prep.db"
    items = PrepItems(db)
    other = InsightStore(db)
    other.add(  # a resume flashcard from another source must survive
        Insight(key="prep:t5-q1", text="A", title="Q", type=CARD_TYPE, captured_at=T0)
    )
    report = items.put("dsa-pack", "Blinkit DSA pack", [_problem(1), _problem(2)],
                       track=DSA, now=T0)  # fmt: skip
    assert (report.added, report.updated, report.removed) == (2, 0, 0)
    card = other.get(item_key("dsa-pack", "p1"))
    assert card.type == "dsa" and card.status == "web-only"
    assert card.tags == ["sliding-window", "heap"]
    assert card.source_session == "prep:dsa-pack" and card.captured_at == T0

    changed = _problem(1)
    changed.text = PROBLEM + "\n### Solution\nUse a deque."
    report = items.put("dsa-pack", "Blinkit DSA pack", [changed], replace=True, now=T0)
    assert (report.added, report.updated, report.removed) == (0, 1, 1)
    assert "deque" in other.get(item_key("dsa-pack", "p1")).text
    assert other.get(item_key("dsa-pack", "p2")) is None
    assert other.get("prep:t5-q1") is not None
    assert items.labels() == {"prep:dsa-pack": "Blinkit DSA pack"}

    assert items.remove_source("dsa-pack") == 1
    assert items.labels() == {}


def test_put_refuses_cards_that_are_not_prep(tmp_path):
    items = PrepItems(tmp_path / "prep.db")
    with pytest.raises(ValueError):
        items.put("x", "X", [PrepItem("insight:x", PROBLEM_TYPE, "t", "x")])
    with pytest.raises(ValueError):
        items.put("x", "X", [PrepItem("prep:x:y", "gotcha", "t", "x")])


def test_new_tracks_reach_the_interview_tab_with_their_source(tmp_path):
    db = tmp_path / "prep.db"
    PrepItems(db).put("dsa-pack", "Blinkit DSA pack", [_problem(1)], now=T0)
    feed = WebFeed(db)
    (entry,) = feed.edition_page(feed.new_edition(interview=True, now=T0)).entries
    assert (entry.track, entry.source, entry.flashcard) == (
        DSA,
        "Blinkit DSA pack",
        False,
    )
    # For you stays free of prep.
    assert feed.edition_page(feed.new_edition(now=T0)).entries == []


# --- grading ----------------------------------------------------------------- #


def test_grades_reschedule_the_card(tmp_path):
    db = tmp_path / "prep.db"
    PrepItems(db).put("dsa-pack", "Pack", [_problem(1)], now=T0)
    key = item_key("dsa-pack", "p1")
    grades = GradeStore(db)
    feed = WebFeed(db)

    assert grades.grade(key, MISSED, now=T0) == T0 + timedelta(days=1)
    assert grades.grade(key, SHAKY, now=T0 + timedelta(days=1)) == T0 + timedelta(
        days=4
    )
    # Got-it gaps grow while got-it grades run on: 7, 21, then 60 days.
    t = T0 + timedelta(days=4)
    assert grades.grade(key, GOT, now=t) == t + timedelta(days=7)
    assert grades.grade(key, GOT, now=t + timedelta(days=7)) == t + timedelta(days=28)
    assert grades.grade(key, GOT, now=t + timedelta(days=28)) == t + timedelta(days=88)
    entry = feed.entry(key)
    assert entry.revisit is False  # grading isn't a view
    assert [g.grade for g in grades.history()] == [MISSED, SHAKY, GOT, GOT, GOT]
    assert grades.latest()[key].grade == GOT


def test_grading_needs_a_known_prep_card(tmp_path):
    db = tmp_path / "prep.db"
    InsightStore(db).add(Insight(key="insight:x", text="x", captured_at=T0))
    grades = GradeStore(db)
    with pytest.raises(KeyError):
        grades.grade("insight:x", GOT, now=T0)
    with pytest.raises(KeyError):
        grades.grade("prep:nope", GOT, now=T0)
    with pytest.raises(ValueError):
        grades.grade("prep:nope", "great", now=T0)


def test_next_gap_caps_the_got_streak():
    assert next_gap(GOT, 9) == timedelta(days=60)
    assert next_gap(MISSED, 3) == timedelta(days=1)


def test_tag_stats_and_weak_tags_use_each_cards_latest_grade(tmp_path):
    db = tmp_path / "prep.db"
    cards = [
        PrepItem(item_key("p", "a"), PROBLEM_TYPE, "A", "x", ["heap", "blinkit"]),
        PrepItem(item_key("p", "b"), PROBLEM_TYPE, "B", "x", ["dp", "blinkit"]),
        PrepItem(item_key("p", "c"), PROBLEM_TYPE, "C", "x", ["dp", "blinkit"]),
        PrepItem(item_key("p", "d"), DRILL_TYPE, "D", "x", ["bfs"]),
        PrepItem(item_key("p", "e"), DESIGN_TYPE, "E", "x", ["caching"]),
    ]
    PrepItems(db).put("p", "Pack", cards, now=T0)
    grades = GradeStore(db)
    grades.grade(item_key("p", "a"), MISSED, now=T0)
    grades.grade(item_key("p", "a"), GOT, now=T0 + timedelta(hours=1))  # latest wins
    grades.grade(item_key("p", "b"), MISSED, now=T0)
    grades.grade(item_key("p", "c"), SHAKY, now=T0)
    grades.grade(item_key("p", "d"), SHAKY, now=T0)
    grades.grade(item_key("p", "e"), MISSED, now=T0)

    stats = {s.tag: s for s in grades.tag_stats(types_for(DSA))}
    assert set(stats) == {"heap", "blinkit", "dp", "bfs"}
    dp = stats["dp"]
    assert (dp.cards, dp.graded, dp.missed, dp.shaky, dp.got) == (2, 2, 1, 1, 0)
    assert dp.weakness == 0.75
    assert grades.weak_tags(types_for(DSA), ignore=["blinkit"]) == ["dp", "bfs"]
    assert grades.weak_tags(types_for(DESIGN)) == ["caching"]


def test_a_new_track_card_opens_in_the_reader(tmp_path):
    from urllib.parse import quote

    from starlette.testclient import TestClient

    from daily_agent.web.app import create_app

    db = tmp_path / "prep.db"
    PrepItems(db).put("dsa-pack", "Blinkit DSA pack", [_problem(1)], now=T0)
    client = TestClient(create_app(db))
    page = client.get("/i/" + quote(item_key("dsa-pack", "p1"), safe=""))
    assert page.status_code == 200
    assert "Problem 1" in page.text and "Blinkit DSA pack" in page.text
    assert "item_key" not in page.text
    interview = client.get("/?view=interview")
    assert interview.status_code == 200 and "Problem 1" in interview.text
