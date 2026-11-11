"""The "For you" edition: a fresh, non-repeating order every time the feed opens.

Pure ranking logic (no I/O), so it is deterministic under a seeded RNG and easy
to test. ``WebFeed`` gathers the candidates and persists the resulting order.

An edition is built from three pools:

1. **fresh**: unseen insights published within ``FRESH_WINDOW``; they lead.
2. **backlog**: older unseen insights.
3. **revisits**: seen insights whose spaced-repetition gap has elapsed. A seen
   insight returns after 1d, 3d, 1w, 3w, then 2mo; saved ones sooner.

Seen insights that are not yet due are left out entirely, which is what keeps
the feed from repeating. Each pool is weighted-shuffled (higher score is more
likely to come early, never guaranteed), revisits are mixed into the backlog
rather than dumped at the end, and a final pass breaks up runs of one type or
of consecutive insights from the same coding session.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Sequence

# Days until a seen insight may resurface, indexed by how many times it has been
# seen (1st view -> 1 day, 2nd -> 3 days, ...). The last gap repeats.
INTERVAL_DAYS = (1, 3, 7, 21, 60)
SAVED_INTERVAL_DAYS = (1, 2, 4, 10, 30)
FRESH_WINDOW = timedelta(days=3)
# After the fresh block, every Nth slot goes to a due revisit when one is left.
REVISIT_EVERY = 4
MAX_TYPE_RUN = 2


@dataclass(frozen=True)
class Candidate:
    key: str
    type: str
    score: float
    source_session: str
    published_at: datetime
    view_count: int
    due_at: datetime | None
    saved: bool

    @property
    def seen(self) -> bool:
        return self.view_count > 0


def revisit_interval(view_count: int, *, saved: bool) -> timedelta:
    """Gap before an insight seen ``view_count`` times may come back."""
    days = SAVED_INTERVAL_DAYS if saved else INTERVAL_DAYS
    return timedelta(days=days[min(max(view_count, 1), len(days)) - 1])


def _weighted_shuffle(
    items: Sequence[Candidate],
    weight: Callable[[Candidate], float],
    rng: random.Random,
) -> list[Candidate]:
    """Random order biased toward heavier items (Efraimidis–Spirakis keys)."""
    keyed = [(rng.random() ** (1.0 / max(weight(c), 1e-6)), c) for c in items]
    keyed.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in keyed]


def _interleave(
    primary: list[Candidate], extra: list[Candidate], every: int
) -> list[Candidate]:
    """Put an ``extra`` item in every ``every``-th slot while both remain."""
    out: list[Candidate] = []
    p, e = list(primary), list(extra)
    while p or e:
        if e and (not p or (len(out) + 1) % every == 0):
            out.append(e.pop(0))
        else:
            out.append(p.pop(0))
    return out


def _fits(placed: list[Candidate], candidate: Candidate) -> bool:
    run = placed[-MAX_TYPE_RUN:]
    if len(run) == MAX_TYPE_RUN and all(c.type == candidate.type for c in run):
        return False
    last = placed[-1] if placed else None
    if last and last.source_session and last.source_session == candidate.source_session:
        return False
    return True


def _diversify(
    items: list[Candidate], placed: list[Candidate] | None = None
) -> list[Candidate]:
    """Greedy re-order: avoid type runs and back-to-back same-session items.

    Each slot takes the *earliest* remaining item that fits, so the weighted
    order is kept wherever it already satisfies the constraints and items are
    only pulled forward when needed (searching the whole queue, since skipped
    items of a common type pile up at its front). If the most common remaining
    type could no longer be spread out later (more than ``MAX_TYPE_RUN`` per
    other item), it is placed now, so the tail never gets a forced long run.
    When nothing fits at all, the next item goes in as-is.
    """
    history = list(placed or [])
    out: list[Candidate] = []
    pending = list(items)
    remaining = Counter(c.type for c in pending)
    while pending:
        so_far = history + out
        top_type, top = remaining.most_common(1)[0]
        forced = top > MAX_TYPE_RUN * (len(pending) - top)
        pick = None
        if forced:
            pick = next(
                (
                    i
                    for i, c in enumerate(pending)
                    if c.type == top_type and _fits(so_far, c)
                ),
                None,
            )
        if pick is None:
            pick = next((i for i, c in enumerate(pending) if _fits(so_far, c)), 0)
        chosen = pending.pop(pick)
        remaining[chosen.type] -= 1
        out.append(chosen)
    return out


def build_edition(
    candidates: Sequence[Candidate], *, now: datetime, rng: random.Random
) -> list[str]:
    """Order the keys for one feed edition. Hidden insights must be excluded."""
    fresh, backlog, revisits = [], [], []
    for c in candidates:
        if not c.seen:
            (fresh if c.published_at >= now - FRESH_WINDOW else backlog).append(c)
        elif c.due_at is not None and c.due_at <= now:
            revisits.append(c)

    def by_score(c: Candidate) -> float:
        return 0.5 + max(0.0, min(c.score, 1.0))

    def by_overdue(c: Candidate) -> float:
        overdue_days = (now - c.due_at).total_seconds() / 86400 if c.due_at else 0
        return (1.0 + min(overdue_days, 30.0) / 10) * (1.5 if c.saved else 1.0)

    lead = _diversify(_weighted_shuffle(fresh, by_score, rng))
    rest = _interleave(
        _weighted_shuffle(backlog, by_score, rng),
        _weighted_shuffle(revisits, by_overdue, rng),
        REVISIT_EVERY,
    )
    return [c.key for c in lead + _diversify(rest, placed=lead)]
