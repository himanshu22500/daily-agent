"""Reading a resume deep-dive folder (issue #108). Offline: tmp_path fixtures
in the same format as the real notes."""

from __future__ import annotations

from pathlib import Path

import pytest

from daily_agent.sources.deep_dive import (
    DeepDiveError,
    parse_point,
    read_deep_dive,
    tracker_rows,
)

README = """\
# Resume Deep Dive

| # | Point | File | Status |
|---|-------|------|--------|
| **TranZact** (SWE 2, Nov 2025 – Present, Mumbai) ||||
| T0 | Role overview | [01-tranzact/00-role-overview.md](01-tranzact/00-role-overview.md) | ⬜ |
| T5 | MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7 | [01-tranzact/05-mysql.md](01-tranzact/05-mysql.md) | 🟨 |
| T6 | Escapes the folder | [../outside.md](../outside.md) | ⬜ |
| T7 | Missing file | [01-tranzact/07-missing.md](01-tranzact/07-missing.md) | ⬜ |
| **Technical skills** ||||
| S1 | Languages (Python, `SQL`) | [04-technical-skills/01-languages.md](04-technical-skills/01-languages.md) | ⬜ |
"""

POINT = """\
# MySQL 8 → PostgreSQL 18 migration + UUIDv7 keys

> **Resume line (T5):** Led the production **MySQL 8 → PostgreSQL 18** migration.

**Status:** ⬜ Not started

---

## 1. The story (STAR)
- **Situation:**

## 4. Likely interview questions
### Surface level (screening / HR / hiring manager)

**Q1. Walk me through this migration in two minutes.**
- **Answer:** We moved prod from MySQL to Postgres with `DMS`.

**Q2. Why move at all?**
- **Answer:** ⬜ OPEN (partial). Context: the MySQL master and replica were both at 98–100% CPU at peak. _Discuss: the business reason._

**Q3. How big was the user base?**
- **Answer:** ⬜ OPEN — _Discuss: real user numbers._
- _Reference design — confirm (your earlier draft):_ about 10k users **[FILL]**.

### Deep dive (technical round)

**Q4. How did you make the SQL safe?**
- **Answer:** A checker rejects writes.
- **Known weakness:** spaces are stripped first.
  - DB role with **SELECT-only**
- **Answer:** ⬜ OPEN (partial) — impact and status. _Discuss: who uses it._

### Curveballs / probing ("why not X?", "what broke?", "how did you measure?")

**Q5. Explain expand/contract migrations.** _(adjacent fundamentals)_
- **Answer:** Add the new shape, migrate, then remove the old one.

## 5. Concepts to master
- AWS DMS: full load + CDC
- UUIDv4 vs UUIDv7: B-tree locality
-

## 6. Trade-offs and alternatives considered
- **Q9. Not a question.**
"""


def make_folder(root: Path) -> Path:
    folder = root / "deep-dive"
    (folder / "01-tranzact").mkdir(parents=True)
    (folder / "04-technical-skills").mkdir()
    (folder / "README.md").write_text(README, encoding="utf-8")
    (folder / "01-tranzact" / "05-mysql.md").write_text(POINT, encoding="utf-8")
    (folder / "01-tranzact" / "00-role-overview.md").write_text(
        "# TranZact: role overview\n\n> **Resume line (T0):** SWE 2 @ TranZact\n\n"
        "## 5. Concepts to master\n- What TranZact does\n",
        encoding="utf-8",
    )
    (folder / "04-technical-skills" / "01-languages.md").write_text(
        "# Skill: languages\n\n## 4. Likely interview questions\n"
        "**Q1. Why Python?**\n- **Answer:** It fits.\n",
        encoding="utf-8",
    )
    (root / "outside.md").write_text("# Not mine\n", encoding="utf-8")
    return folder


def test_tracker_rows_know_their_job():
    rows = tracker_rows(README)
    assert [r.id for r in rows] == ["T0", "T5", "T6", "T7", "S1"]
    t5 = rows[1]
    assert t5.name == "MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7"
    assert t5.path == "01-tranzact/05-mysql.md"
    assert (t5.group, t5.job) == ("TranZact", True)
    assert (rows[-1].group, rows[-1].job) == ("Technical skills", False)
    assert rows[-1].name == "Languages (Python, SQL)"


def test_parse_point_cards_and_concepts():
    cards, concepts, unanswered = parse_point(POINT, "T5")
    assert [c.number for c in cards] == [1, 2, 4, 5]
    assert unanswered == 1  # Q3: nothing written, only a draft to confirm
    q1, q2, q4, q5 = cards
    assert q1.level == "surface" and q1.answer == (
        "We moved prod from MySQL to Postgres with `DMS`."
    )
    assert q1.still_open == ""
    assert q2.answer.startswith("Context: the MySQL master")
    assert q2.still_open == "the business reason"
    assert q4.level == "deep-dive"
    assert q4.answer == (
        "A checker rejects writes.\n• **Known weakness:** spaces are stripped first.\n"
        "• DB role with **SELECT-only**"
    )
    assert q4.still_open == "impact and status; who uses it"
    assert q5.level == "curveball"
    assert q5.question == "Explain expand/contract migrations."
    assert [c.text for c in concepts] == [
        "AWS DMS: full load + CDC",
        "UUIDv4 vs UUIDv7: B-tree locality",
    ]
    assert [c.number for c in concepts] == [1, 2]


def test_read_deep_dive_takes_only_job_points_inside_the_folder(tmp_path):
    deep = read_deep_dive(make_folder(tmp_path))
    assert [p.id for p in deep.points] == ["T0", "T5"]  # T6 escapes, T7 missing
    t5 = deep.points[1]
    assert t5.label == "T5 · MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7"
    assert t5.title == "MySQL 8 → PostgreSQL 18 migration + UUIDv7 keys"
    assert t5.resume_line == "Led the production MySQL 8 → PostgreSQL 18 migration."
    assert t5.group == "TranZact" and t5.path == "01-tranzact/05-mysql.md"
    assert t5.notes == POINT
    assert len(deep.cards) == 4 and deep.unanswered == 1
    assert [c.text for c in deep.concepts][0] == "What TranZact does"
    everything = read_deep_dive(tmp_path / "deep-dive", jobs_only=False)
    assert [p.id for p in everything.points] == ["T0", "T5", "S1"]
    assert everything.cards[-1].question == "Why Python?"


def test_not_a_deep_dive_folder(tmp_path):
    with pytest.raises(DeepDiveError, match="README"):
        read_deep_dive(tmp_path)
    (tmp_path / "README.md").write_text("# Just notes\n", encoding="utf-8")
    with pytest.raises(DeepDiveError, match="tracker"):
        read_deep_dive(tmp_path)
