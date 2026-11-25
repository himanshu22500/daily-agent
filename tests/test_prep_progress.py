"""The /progress page (issue #136): tracks, the DSA pattern grid, design
topics, weak spots, the streak and the countdown. Offline: tmp_path SQLite
(or Postgres schemas in CI)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from starlette.testclient import TestClient

from daily_agent.feed.prep_grades import GOT, MISSED, SHAKY, GradeStore
from daily_agent.feed.prep_progress import (
    DSA_PATTERNS,
    DayTally,
    PrepProgress,
    Progress,
    Tile,
    interview_day,
    local_zone,
    streak,
)
from daily_agent.feed.prep_tracks import (
    CARD_TYPE,
    DESIGN,
    DESIGN_TYPE,
    DRILL_TYPE,
    DSA,
    PROBLEM_TYPE,
    RESUME,
    PrepItem,
    PrepItems,
    item_key,
)
from daily_agent.feed.web_feed import WebFeed
from daily_agent.web.app import create_app

IST = local_zone("Asia/Kolkata")
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)  # 17:30 in Kolkata
TODAY = date(2026, 11, 25)


def key(name: str) -> str:
    return item_key("pack", name)


def _seed(db) -> None:
    cards = [
        PrepItem(
            key("p1"),
            PROBLEM_TYPE,
            "P1",
            "x",
            ["sliding-window", "heap", "blinkit", "must-do"],
        ),  # fmt: skip
        PrepItem(key("p2"), PROBLEM_TYPE, "P2", "x", ["dp", "blinkit", "hard"]),
        PrepItem(key("p3"), PROBLEM_TYPE, "P3", "x", ["dp"]),
        PrepItem(key("q1"), DRILL_TYPE, "Q1", "x", ["two-pointers"]),
        PrepItem(key("d1"), DESIGN_TYPE, "D1", "x", ["caching", "rate-limiting"]),
        PrepItem(key("d2"), DESIGN_TYPE, "D2", "x", ["caching", "blinkit"]),
        PrepItem(key("r1"), CARD_TYPE, "R1", "x", ["deep-dive"]),
    ]
    PrepItems(db).put("pack", "Pack", cards, now=NOW - timedelta(days=10))
    grades = GradeStore(db)
    grades.grade(key("p1"), MISSED, now=NOW - timedelta(days=3))
    grades.grade(key("p1"), GOT, now=NOW - timedelta(days=1))  # latest wins
    grades.grade(key("p2"), MISSED, now=NOW - timedelta(hours=2))
    grades.grade(key("p3"), SHAKY, now=NOW - timedelta(hours=1))
    grades.grade(key("d1"), MISSED, now=NOW - timedelta(days=2))
    WebFeed(db).record_views([key("d2")], session_id="s1", now=NOW)


def test_tracks_count_cards_seen_graded_and_the_got_it_rate(tmp_path):
    db = tmp_path / "p.db"
    _seed(db)
    tracks = {t.track: t for t in PrepProgress(db).tracks()}
    assert list(tracks) == [RESUME, DSA, DESIGN]
    dsa = tracks[DSA]
    assert (dsa.cards, dsa.seen, dsa.graded, dsa.got, dsa.got_rate) == (4, 3, 3, 1, 33)
    design = tracks[DESIGN]
    assert (design.cards, design.seen, design.graded, design.got_rate) == (2, 2, 1, 0)
    resume = tracks[RESUME]
    assert (resume.cards, resume.seen, resume.graded, resume.got_rate) == (
        1,
        0,
        0,
        None,
    )
    assert resume.label == "Resume" and dsa.ungraded == 1


def test_the_pattern_grid_topics_and_weak_spots(tmp_path):
    db = tmp_path / "p.db"
    _seed(db)
    p = PrepProgress(db).build(now=NOW, zone=IST)

    assert [t.tag for t in p.patterns] == list(DSA_PATTERNS)  # canonical, in order
    tiles = {t.tag: t for t in p.patterns}
    dp = tiles["dp"]
    assert (dp.cards, dp.got, dp.shaky, dp.missed, dp.level) == (2, 0, 1, 1, "weak")
    assert tiles["sliding-window"].level == "strong"
    assert tiles["two-pointers"].level == "new"
    gap = tiles["arrays-hashing"]
    assert (gap.cards, gap.level, gap.level_label) == (0, "gap", "No cards yet")
    assert dp.label == "Dynamic programming" and "hard" not in tiles

    topics = {t.tag: t for t in p.topics}
    assert set(topics) == {"caching", "rate-limiting"}  # never a source tag
    caching = topics["caching"]
    assert (caching.cards, caching.missed, caching.level) == (2, 1, "weak")
    assert (
        caching.label == "Caching" and topics["rate-limiting"].label == "Rate limiting"
    )

    # Weakest first; "hard" isn't a pattern and "blinkit" names a source.
    assert [(t.tag, t.track) for t in p.weak] == [
        ("caching", DESIGN),
        ("rate-limiting", DESIGN),
        ("dp", DSA),
    ]
    assert p.weak[2].track_label == "DSA"


def test_mixed_and_strong_levels():
    assert Tile("x", DSA, cards=4, got=3, shaky=1).level == "strong"  # 0.125
    assert Tile("x", DSA, cards=3, got=2, missed=1).level == "mixed"  # 0.33
    assert Tile("x", DSA, cards=2, shaky=2).level == "mixed"  # 0.5
    assert Tile("x", DSA, cards=3, got=1, missed=2).level == "weak"  # 0.67
    assert Tile("x", DSA, cards=5, got=1).ungraded == 4


def test_streak_and_the_week_go_by_local_days(tmp_path):
    db = tmp_path / "p.db"
    _seed(db)
    grades = GradeStore(db)
    # 19:00 UTC on the 24th is 00:30 on the 25th in Kolkata: today.
    grades.grade(key("q1"), GOT, now=datetime(2026, 11, 24, 19, 0, tzinfo=timezone.utc))  # fmt: skip
    p = PrepProgress(db).build(now=NOW, zone=IST)
    # Graded on the 25th (3), 24th (p1), 23rd (d1) and 22nd (p1).
    assert p.streak == 4
    assert [d.day for d in p.week] == [TODAY - timedelta(days=n) for n in range(6, -1, -1)]  # fmt: skip
    today = p.today_tally
    assert (today.got, today.shaky, today.missed, today.total) == (1, 1, 1, 3)
    assert [d.total for d in p.week] == [0, 0, 0, 1, 1, 1, 3]
    assert p.week_max == 3 and today.label == "Wed 25 Nov"

    days = {TODAY - timedelta(days=1), TODAY - timedelta(days=2)}
    assert streak(days, TODAY) == 2  # not broken until today is over
    assert streak(days | {TODAY - timedelta(days=4)}, TODAY) == 2
    assert streak(set(), TODAY) == 0


def test_a_bad_time_zone_falls_back_to_utc(tmp_path, caplog):
    assert str(local_zone(" Asia/Kolkata ")) == "Asia/Kolkata"
    for name in ("Asia/Kolkatta", "", "../etc"):
        assert local_zone(name) is timezone.utc
    assert "Unknown time zone 'Asia/Kolkatta'" in caplog.text
    # A typo doesn't stop the feed server; the page goes by UTC days.
    client = TestClient(create_app(tmp_path / "p.db", local_tz="Nowhere/Land"))
    assert client.get("/progress").status_code == 200


def test_the_countdown():
    assert interview_day("2026-12-18") == date(2026, 12, 18)
    assert interview_day("") is None and interview_day("next week") is None

    def progress(day):
        return Progress(TODAY, day, 0, [], [], [], [], [DayTally(TODAY)])

    assert progress(date(2026, 12, 18)).days_left == 23
    assert progress(TODAY).days_left == 0
    assert progress(TODAY - timedelta(days=1)).days_left is None
    assert progress(None).days_left is None


def test_the_progress_page(tmp_path):
    db = tmp_path / "p.db"
    _seed(db)
    client = TestClient(create_app(db, interview_date="2099-01-01"))
    page = client.get("/progress")
    assert page.status_code == 200
    html = page.text
    assert "<title>Progress · Insights</title>" in html
    assert "days to go" in html and f"{date(2099, 1, 1):%a} 1 Jan" in html
    assert 'href="/?view=interview&amp;track=dsa&amp;tag=dp"' in html
    assert 'href="/?view=interview&amp;track=design&amp;tag=caching"' in html
    assert 'href="/?view=interview&amp;track=dsa"' in html  # the track row
    assert '<div class="tile lv-gap">' in html and "No cards yet" in html
    assert 'class="tile lv-weak"' in html and "1 shaky · 1 missed" in html
    assert "33% got it" in html and '<ol class="week">' in html

    # No interview date, no countdown; a bad one is ignored the same way.
    for value in ("", "soon"):
        html = TestClient(create_app(db, interview_date=value)).get("/progress").text
        assert "to go" not in html and "day streak" in html


def test_an_empty_progress_page(tmp_path):
    html = TestClient(create_app(tmp_path / "empty.db")).get("/progress").text
    assert "No design cards yet." in html and "Nothing weak yet." in html
    assert "No grades this week." in html
    assert html.count('<div class="tile lv-gap">') == len(DSA_PATTERNS)


def test_the_interview_tab_links_to_progress(tmp_path):
    client = TestClient(create_app(tmp_path / "w.db"))
    assert 'href="/progress">Progress</a>' in client.get("/?view=interview").text
    assert 'href="/progress"' not in client.get("/").text
