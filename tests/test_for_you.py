"""For you edition ranking: pure logic under a seeded RNG."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from daily_agent.feed.for_you import (
    FRESH_WINDOW,
    Candidate,
    build_edition,
    revisit_interval,
)

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
OLD = NOW - FRESH_WINDOW - timedelta(days=10)


def _c(
    key: str,
    *,
    type: str = "gotcha",
    score: float = 0.5,
    session: str = "",
    published: datetime = OLD,
    views: int = 0,
    due: datetime | None = None,
    saved: bool = False,
) -> Candidate:
    return Candidate(
        key=key,
        type=type,
        score=score,
        source_session=session or f"s-{key}",
        published_at=published,
        view_count=views,
        due_at=due,
        saved=saved,
    )


def test_revisit_gaps_grow_and_saved_comes_back_sooner():
    assert [revisit_interval(n, saved=False).days for n in range(1, 7)] == [
        1,
        3,
        7,
        21,
        60,
        60,
    ]
    assert revisit_interval(3, saved=True) < revisit_interval(3, saved=False)


def test_fresh_unseen_leads_then_backlog_with_due_revisits_mixed_in():
    fresh = [_c(f"f{i}", published=NOW - timedelta(hours=i + 1)) for i in range(2)]
    backlog = [
        _c(f"b{i}", type=["repo", "gotcha", "technique"][i % 3]) for i in range(9)
    ]
    due = [_c(f"d{i}", views=1, due=NOW - timedelta(hours=1)) for i in range(2)]
    cooling = [_c("cool", views=2, due=NOW + timedelta(days=2))]
    keys = build_edition(fresh + backlog + due + cooling, now=NOW, rng=random.Random(3))

    assert set(keys[:2]) == {"f0", "f1"}
    assert "cool" not in keys  # seen, not yet due: no repeat
    assert len(keys) == 13
    revisit_slots = [i for i, k in enumerate(keys) if k.startswith("d")]
    # Mixed in among the backlog, not dumped at the end.
    assert revisit_slots and min(revisit_slots) < len(keys) - 2


def test_same_seed_same_order_different_seeds_differ():
    pool = [_c(f"k{i}", type=["repo", "gotcha"][i % 2]) for i in range(12)]
    one = build_edition(pool, now=NOW, rng=random.Random(42))
    assert one == build_edition(pool, now=NOW, rng=random.Random(42))
    others = {
        tuple(build_edition(pool, now=NOW, rng=random.Random(s))) for s in range(10)
    }
    assert len(others) > 5


def test_breaks_up_type_runs_and_same_session_neighbours():
    pool = [_c(f"g{i}", type="gotcha") for i in range(6)] + [
        _c(f"r{i}", type="repo") for i in range(3)
    ]
    pool += [_c(f"x{i}", type="technique", session="shared") for i in range(3)]
    by_key = {c.key: c for c in pool}
    for seed in range(20):
        keys = build_edition(pool, now=NOW, rng=random.Random(seed))
        types = [by_key[k].type for k in keys]
        sessions = [by_key[k].source_session for k in keys]
        # 6 of 12 are gotchas, so runs of 3 are avoidable and must not happen.
        assert not any(types[i] == types[i + 1] == types[i + 2] for i in range(10))
        assert not any(sessions[i] == sessions[i + 1] == "shared" for i in range(11))


def test_higher_score_tends_to_come_first():
    pool = [_c("star", score=1.0, type="repo")] + [
        _c(f"k{i}", score=0.0, type=["gotcha", "technique"][i % 2]) for i in range(9)
    ]
    first_half = sum(
        "star" in build_edition(pool, now=NOW, rng=random.Random(s))[:5]
        for s in range(200)
    )
    assert first_half > 120  # ~50% by chance; weighted well above that


def test_nothing_eligible_means_empty_edition():
    cooling = [_c("a", views=1, due=NOW + timedelta(hours=5))]
    assert build_edition(cooling, now=NOW, rng=random.Random(0)) == []


def test_no_type_runs_in_a_long_feed_dominated_by_one_type():
    # Real-world shape: ~300 insights, about half of them gotchas. A short
    # look-ahead used to let skipped gotchas pile up and force runs mid-feed.
    kinds = (
        ["gotcha"] * 150 + ["repo"] * 80 + ["architecture"] * 45 + ["technique"] * 25
    )
    pool = [_c(f"k{i}", type=t) for i, t in enumerate(kinds)]
    by_key = {c.key: c for c in pool}
    for seed in range(10):
        types = [
            by_key[k].type
            for k in build_edition(pool, now=NOW, rng=random.Random(seed))
        ]
        assert not any(
            types[i] == types[i + 1] == types[i + 2] for i in range(len(types) - 2)
        )
