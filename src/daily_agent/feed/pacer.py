"""Pacer — the cadence policy that keeps the feed a trickle, not a flood.

The feed builds a backlog of chapter bites in the outbox; the pacer decides how
many to release on a given run and stays silent during quiet hours. Combined with
running `feed` periodically (e.g. hourly via launchd), this trickles the backlog
out over the day instead of dumping it at once — replacing the manual `--limit`.

Deliberately simple and stateless: richer cadence (morning/EOD checkpoints, event
nudges, a real "notable enough to interrupt" bar) is left for later, once there's
real-use feedback on what the right rhythm is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Pacer:
    """How many bites to release now, given the wall-clock time.

    ``max_per_run`` caps deliveries per feed run. Quiet hours are [start, end)
    on a 24h local clock and may wrap midnight (e.g. 22→8). If ``quiet_start ==
    quiet_end`` there are no quiet hours.
    """

    max_per_run: int
    quiet_start: int
    quiet_end: int

    def in_quiet_hours(self, now: datetime) -> bool:
        if self.quiet_start == self.quiet_end:
            return False
        h = now.hour
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= h < self.quiet_end
        # Wraps past midnight (e.g. 22:00–08:00).
        return h >= self.quiet_start or h < self.quiet_end

    def allowance(self, now: datetime) -> int:
        """How many bites may be delivered right now (0 during quiet hours)."""
        if self.in_quiet_hours(now):
            return 0
        return max(0, self.max_per_run)
