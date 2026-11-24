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

**Prep mode** (issue #132) blends interview prep into the edition
(``blend_edition``): a share of the slots goes to prep cards, spread evenly,
and the work stories keep only the best of the remaining slots.
"""

from __future__ import annotations

import math
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
# Prep mode: an edition is as long as it would be without prep (one slot per
# work story that is unseen or due), but never shorter than this while there
# is prep to show, so being caught up on work still leaves a practice run.
PREP_MIN_SLOTS = 30


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


def _showable(c: Candidate, now: datetime) -> bool:
    """Unseen, or seen and due again: what an edition may include."""
    return not c.seen or (c.due_at is not None and c.due_at <= now)


def best_work(
    candidates: Sequence[Candidate], count: int, *, now: datetime
) -> list[Candidate]:
    """The ``count`` work stories most worth reading, in their original order.

    Higher score first; at equal score, unseen stories from the last few days,
    then older unseen ones, then due revisits; then the newest; the key breaks
    any remaining tie. Callers pass only showable candidates.
    """

    def rank(c: Candidate) -> tuple:
        fresh = not c.seen and c.published_at >= now - FRESH_WINDOW
        score = max(0.0, min(c.score, 1.0))
        return (-score, not fresh, c.seen, -c.published_at.timestamp(), c.key)

    keep = {c.key for c in sorted(candidates, key=rank)[: max(count, 0)]}
    return [c for c in candidates if c.key in keep]


def _is_prep_slot(slot: int, mix: float) -> bool:
    """Whether slot ``slot`` (0-based) is a prep slot: spreads ``mix`` evenly,
    work first (0.5 -> work, prep, work, prep; 1/3 -> work, work, prep)."""
    return math.floor((slot + 1) * mix + 1e-9) > math.floor(slot * mix + 1e-9)


def blend_edition(
    work: Sequence[Candidate],
    prep: Sequence[Candidate],
    *,
    mix: float,
    now: datetime,
    rng: random.Random,
    min_slots: int = PREP_MIN_SLOTS,
) -> list[str]:
    """A For you edition with interview prep mixed in (prep mode, issue #132).

    The edition has as many slots as there are work stories to show (unseen
    or due), at least ``min_slots``. A ``mix`` share of them goes to prep
    cards, ordered like the Interview tab's deck (``build_edition``: unseen
    first, seen cards only when due). Work stories fill the rest, trimmed to
    the best ones (``best_work``) and ordered as For you always orders them.
    Prep is spread evenly through the edition while both last; when one runs
    out, the other fills its slots. Hidden items must be excluded by the
    caller. ``mix`` <= 0 is plain ``build_edition`` over the work stories.
    """
    mix = min(max(mix, 0.0), 1.0)
    if mix <= 0:
        return build_edition(work, now=now, rng=rng)
    work = [c for c in work if _showable(c, now)]
    prep = [c for c in prep if _showable(c, now)]
    slots = max(len(work), min_slots)
    prep_slots = math.floor(slots * mix + 1e-9)  # how many _is_prep_slot picks
    work_count = min(len(work), slots - min(len(prep), prep_slots))
    prep_count = min(len(prep), slots - work_count)
    work_keys = build_edition(best_work(work, work_count, now=now), now=now, rng=rng)
    prep_keys = build_edition(prep, now=now, rng=rng)[:prep_count]
    out: list[str] = []
    w, p = iter(work_keys), iter(prep_keys)
    left_w, left_p = len(work_keys), len(prep_keys)
    while left_w or left_p:
        if left_p and (not left_w or _is_prep_slot(len(out), mix)):
            out.append(next(p))
            left_p -= 1
        else:
            out.append(next(w))
            left_w -= 1
    return out
