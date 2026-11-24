"""Prep mode: interview prep blended into For you (issue #132). All offline."""

from __future__ import annotations

import html
import random
import re
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from daily_agent.feed.for_you import (
    FRESH_WINDOW,
    Candidate,
    best_work,
    blend_edition,
    build_edition,
)
from daily_agent.db import session_scope
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.preferences import Preferences
from daily_agent.feed.prep import CARD_TYPE, CONCEPT_TYPE
from daily_agent.feed.ratings import DOWN, Ratings
from daily_agent.feed.web_feed import (
    _NOT_PREP,
    PREP_TYPES,
    WebFeed,
    WebFeedStateRow,
    _candidate,
)
from daily_agent.models import Insight
from daily_agent.web.app import create_app

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
OLD = NOW - FRESH_WINDOW - timedelta(days=10)
FETCH = {"X-Requested-With": "fetch"}


# --- the pure blend ------------------------------------------------------------ #
def _c(
    key: str,
    *,
    type: str = "gotcha",
    score: float = 0.5,
    published: datetime = OLD,
    views: int = 0,
    due: datetime | None = None,
) -> Candidate:
    return Candidate(
        key=key,
        type=type,
        score=score,
        source_session=f"s-{key}",
        published_at=published,
        view_count=views,
        due_at=due,
        saved=False,
    )


def _work(n: int, **kwargs) -> list[Candidate]:
    kinds = ("gotcha", "repo", "technique")
    return [_c(f"w{i}", type=kinds[i % 3], **kwargs) for i in range(n)]


def _prep(n: int, **kwargs) -> list[Candidate]:
    return [
        _c(f"p{i}", type=PREP_TYPES[i % len(PREP_TYPES)], **kwargs) for i in range(n)
    ]


def _pattern(keys: list[str]) -> str:
    return "".join("P" if k.startswith("p") else "W" for k in keys)


def test_half_the_slots_go_to_prep_spread_evenly():
    work, prep = _work(40), _prep(100)
    for seed in range(5):
        keys = blend_edition(work, prep, mix=0.5, now=NOW, rng=random.Random(seed))
        # As long as the edition would be without prep, every other card prep.
        assert len(keys) == 40 and len(set(keys)) == 40
        assert _pattern(keys) == "WP" * 20


def test_the_ratio_is_a_setting():
    keys = blend_edition(
        _work(30), _prep(100), mix=1 / 3, now=NOW, rng=random.Random(1)
    )
    assert _pattern(keys) == "WWP" * 10
    keys = blend_edition(_work(40), _prep(100), mix=0.25, now=NOW, rng=random.Random(1))
    assert _pattern(keys) == "WWWP" * 10


def test_work_is_trimmed_to_the_best_stories():
    strong = [_c(f"w-hi{i}", score=0.9) for i in range(10)]
    weak = [_c(f"w-lo{i}", score=0.1) for i in range(10)]
    due = [_c("w-due", score=0.1, views=1, due=NOW - timedelta(hours=1))]
    cooling = [_c("w-cool", score=1.0, views=1, due=NOW + timedelta(days=1))]
    keys = blend_edition(
        strong + weak + due + cooling,
        _prep(50),
        mix=0.5,
        now=NOW,
        rng=random.Random(3),
        min_slots=0,
    )
    work = [k for k in keys if k.startswith("w")]
    # 21 stories can be shown; prep takes 10 of the 21 slots.
    assert len(keys) == 21 and len(work) == 11
    assert {f"w-hi{i}" for i in range(10)} <= set(work)
    assert "w-cool" not in keys  # seen and not due yet: never


def test_best_work_prefers_score_then_unseen_then_newest():
    fresh = _c("fresh", score=0.5, published=NOW - timedelta(hours=1))
    older = _c("older", score=0.5)
    revisit = _c("revisit", score=0.5, views=1, due=NOW - timedelta(hours=1))
    star = _c("star", score=0.9)
    pool = [revisit, older, fresh, star]
    assert best_work(pool, 1, now=NOW) == [star]
    assert best_work(pool, 2, now=NOW) == [fresh, star]  # original order kept
    assert best_work(pool, 3, now=NOW) == [older, fresh, star]
    assert best_work(pool, 0, now=NOW) == []


def test_prep_follows_the_interview_rotation():
    unseen = _prep(3)
    due = [_c("p-due", type=CARD_TYPE, views=1, due=NOW - timedelta(hours=1))]
    cooling = [_c("p-cool", type=CARD_TYPE, views=2, due=NOW + timedelta(days=3))]
    for seed in range(5):
        keys = blend_edition(
            _work(6),
            unseen + due + cooling,
            mix=0.5,
            now=NOW,
            rng=random.Random(seed),
            min_slots=6,
        )
        prep = [k for k in keys if k.startswith("p")]
        # Three prep slots: unseen cards first; the due one waits its turn.
        assert sorted(prep) == ["p0", "p1", "p2"]
        assert "p-cool" not in keys


def test_when_prep_runs_short_work_fills_its_slots():
    keys = blend_edition(_work(40), _prep(3), mix=0.5, now=NOW, rng=random.Random(0))
    assert len(keys) == 40
    assert _pattern(keys) == "WPWPWP" + "W" * 34  # only 3 stories trimmed


def test_when_work_runs_short_prep_fills_a_practice_run():
    keys = blend_edition(_work(2), _prep(100), mix=0.5, now=NOW, rng=random.Random(0))
    assert _pattern(keys) == "WPWP" + "P" * 26  # at least PREP_MIN_SLOTS
    assert blend_edition([], _prep(4), mix=0.5, now=NOW, rng=random.Random(0)) != []
    assert blend_edition([], [], mix=0.5, now=NOW, rng=random.Random(0)) == []


def test_no_prep_or_no_mix_is_the_plain_edition():
    work = [_c(f"x{i}", score=i / 20) for i in range(20)]
    work += [_c("fresh", published=NOW - timedelta(hours=2))]
    work += [_c("due", views=1, due=NOW - timedelta(hours=1))]
    for seed in range(5):
        plain = build_edition(work, now=NOW, rng=random.Random(seed))
        for prep, mix in (([], 0.5), (_prep(9), 0.0), (_prep(9), -1.0)):
            assert (
                blend_edition(work, prep, mix=mix, now=NOW, rng=random.Random(seed))
                == plain
            )


# --- the store ------------------------------------------------------------------ #
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def _insight(key: str, *, hours: int, type: str = "gotcha", score: float = 0.5):
    return Insight(
        key=key,
        text=f"Lesson {key}.",
        type=type,
        tags=[],
        score=score,
        source_session=f"sess-{key}",
        git_branch="main",
        captured_at=T0,
        stored_at=T0 + timedelta(hours=hours),
    )


def _card(n: int, *, hours: int = 0) -> Insight:
    """A flashcard, or every 4th one a concept explainer."""
    card = n % 4 != 0
    return Insight(
        key=f"prep:t1-q{n}",
        text=f"Context: answer {n}." if card else f"What concept {n} is.",
        title=f"Question {n}?",
        type=CARD_TYPE if card else CONCEPT_TYPE,
        tags=["surface"],
        score=0.5,
        source_session="prep:t1",
        git_branch="",
        captured_at=T0,
        stored_at=T0 + timedelta(hours=hours),
    )


def _seed(db: str, *, work: int = 12, prep: int = 12) -> WebFeed:
    store = InsightStore(db)
    kinds = ("gotcha", "repo", "technique")
    for i in range(work):
        store.add(_insight(f"insight:w{i}", hours=i, type=kinds[i % 3], score=i / work))
    for n in range(1, prep + 1):
        store.add(_card(n, hours=n))
    return WebFeed(db)


def _all(feed: WebFeed, edition: str) -> list[str]:
    keys, offset = [], 0
    while offset is not None:
        page = feed.edition_page(edition, offset=offset, limit=5)
        keys += [e.key for e in page.entries]
        offset = page.next_offset
    return keys


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "prep-mode.db")


def test_off_is_exactly_todays_for_you(db):
    feed = _seed(db)
    later = T0 + timedelta(days=2)
    feed.record_views(["insight:w1", "insight:w2"], session_id="x", now=T0)
    feed.record_views(["insight:w3"], session_id="x", now=later)  # not due yet
    feed.set_hidden("insight:w4", True)
    # What today's For you ranks: every visible item that isn't prep.
    with session_scope(feed._engine) as session:
        rows = session.exec(
            feed._base().where(WebFeedStateRow.dismissed_at.is_(None)).where(_NOT_PREP)
        ).all()
    todays = [_candidate(*row) for row in rows]
    for seed in range(5):
        expected = build_edition(todays, now=later, rng=random.Random(seed))
        for kwargs in ({}, {"prep_mix": 0.0}):
            edition = feed.new_edition(now=later, rng=random.Random(seed), **kwargs)
            assert _all(feed, edition) == expected
    assert not any(k.startswith("prep:") for k in expected)


def test_on_blends_prep_and_never_shows_hidden_or_thumbed_down(db):
    feed = _seed(db, work=12, prep=40)
    feed.set_hidden("insight:w11", True)  # "Hide this"
    feed.set_hidden("prep:t1-q5", True)
    # 👎 is a rating plus a hide (feed/ratings.py); the app does both.
    Ratings(db).rate("insight:w10", DOWN)
    feed.set_hidden("insight:w10", True)
    keys = _all(feed, feed.new_edition(prep_mix=0.5, now=T0, rng=random.Random(2)))
    assert {"insight:w11", "insight:w10", "prep:t1-q5"}.isdisjoint(keys)
    # 10 work stories can be shown, but the edition has at least 30 slots:
    # all 10 work stories, every other card prep while they last.
    assert len(keys) == 30 and len(set(keys)) == 30
    assert _pattern_of(keys)[:20] == "WP" * 10
    assert _pattern_of(keys)[20:] == "P" * 10


def _pattern_of(keys: list[str]) -> str:
    return "".join("P" if k.startswith("prep:") else "W" for k in keys)


def test_a_type_chip_and_the_interview_tab_ignore_prep_mode(db):
    feed = _seed(db)
    repo = _all(feed, feed.new_edition(type="repo", prep_mix=0.5, now=T0))
    assert repo and all(k.startswith("insight:") for k in repo)
    deck = _all(feed, feed.new_edition(interview=True, prep_mix=0.5, now=T0))
    assert len(deck) == 12 and all(k.startswith("prep:") for k in deck)


def test_next_revisit_includes_prep_only_in_prep_mode(db):
    feed = _seed(db, work=1, prep=1)
    feed.record_views(["prep:t1-q1"], session_id="x", now=T0)
    assert feed.next_revisit_at(now=T0) is None
    assert feed.next_revisit_at(prep=True, now=T0) == T0 + timedelta(days=1)


def test_the_setting_persists_and_defaults_to_on(db):
    assert Preferences(db).prep_mode() is True
    Preferences(db).set_prep_mode(False)
    assert Preferences(db).prep_mode() is False  # a new connection agrees
    Preferences(db).set_prep_mode(True)
    assert Preferences(db).prep_mode() is True
    prefs = Preferences(db)
    prefs.set("other", "x")
    prefs.set("other", "y")
    assert prefs.get("other") == "y" and prefs.get("missing") is None


# --- the page ------------------------------------------------------------------- #
@pytest.fixture
def client(db) -> TestClient:
    _seed(db, work=6, prep=12)
    return TestClient(create_app(db, page_size=4))


def _keys(body: str) -> list[str]:
    return re.findall(r'data-card data-swap data-key="([^"]+)"', body)


def _more(body: str) -> str | None:
    found = re.search(r'href="([^"]+)" data-more', body)
    return html.unescape(found.group(1)) if found else None


def _whole(client: TestClient, first: str) -> tuple[list[str], str]:
    """Every card of one edition, following Load more; and all its HTML."""
    bodies = [first]
    while more := _more(bodies[-1]):
        bodies.append(client.get(more + "&partial=1").text)
    whole = "".join(bodies)
    return _keys(whole), whole


def _switch(body: str) -> str:
    return re.search(r'<form class="prep-switch".*?</form>', body, re.S).group(0)


def test_the_switch_is_on_by_default_and_only_on_for_you(client):
    page = client.get("/").text
    switch = _switch(page)
    assert 'role="switch" aria-checked="true"' in switch
    assert 'name="on" value="0"' in switch and "Prep mode" in switch
    for url in ("/?view=latest", "/?view=interview", "/?view=saved"):
        assert "prep-switch" not in client.get(url).text


def test_for_you_with_prep_mode_on(client, db):
    keys, whole = _whole(client, client.get("/").text)
    assert len(keys) == len(set(keys)) == 18  # 6 stories + 12 prep: all fit
    assert _pattern_of(keys) == "WP" * 6 + "P" * 6
    # Prep cards are the Interview tab's cards: the answer stays hidden.
    assert whole.count("is-flashcard") == 9  # and 3 concept explainers
    assert whole.count("Surface question · tap to answer") == 9
    assert '<p class="snippet">Context: answer' not in whole
    assert '<p class="snippet">What concept 4 is.</p>' in whole
    # The end of the mix.
    assert "That's the end of this mix." in whole
    assert "Shuffle a new mix" in whole


def test_load_more_and_an_expired_edition_keep_the_mix(client):
    first = client.get("/").text
    gone = client.get("/?session=gone&offset=4&partial=1")
    assert gone.status_code == 410 and "expired" in gone.text
    rebuilt, _ = _whole(client, client.get("/?session=gone").text)
    assert any(k.startswith("prep:") for k in rebuilt)
    assert _pattern_of(rebuilt)[:4] == "WPWP"
    assert len(_whole(client, first)[0]) == 18


def test_the_end_of_the_mix_names_the_next_revisit(client, db):
    page = client.get("/").text
    keys, _ = _whole(client, page)
    client.post("/seen", json={"session": "s", "keys": keys[:-1]}, headers=FETCH)
    last = client.get("/").text
    assert _keys(last) == keys[-1:]
    assert "That's the end of this mix." in last
    assert "The next card comes back in" in last


def test_turning_it_off_restores_todays_for_you(client, db, monkeypatch):
    res = client.post("/prefs/prep-mode", data={"on": "0"}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/"
    assert Preferences(db).prep_mode() is False

    asked = []
    real = WebFeed.new_edition

    def spy(self, **kwargs):
        asked.append(kwargs)
        return real(self, **kwargs)

    monkeypatch.setattr(WebFeed, "new_edition", spy)
    page = client.get("/").text
    assert asked == [{"type": None, "prep_mix": 0.0}]  # today's call
    assert 'aria-checked="false"' in _switch(page)
    assert 'name="on" value="1"' in _switch(page)
    keys, last = _whole(client, page)
    assert sorted(keys) == [f"insight:w{i}" for i in range(6)]
    assert "You're all caught up." in last and "end of this mix" not in last

    client.post("/prefs/prep-mode", data={"on": "1"})
    assert any(k.startswith("prep:") for k in _whole(client, client.get("/").text)[0])


def test_the_switch_refuses_cross_site_and_bad_values(client, db):
    evil = {"Origin": "https://evil.example"}
    res = client.post("/prefs/prep-mode", data={"on": "0"}, headers=evil)
    assert res.status_code == 403 and Preferences(db).prep_mode() is True
    assert client.post("/prefs/prep-mode", data={"on": "maybe"}).status_code == 400
    assert client.post("/prefs/prep-mode").status_code == 400


def test_a_thumbs_down_leaves_the_mix(client, db):
    card = "prep:t1-q1"
    assert client.post(f"/items/{card}/down", headers=FETCH).status_code == 200
    story = "insight:w5"
    assert client.post(f"/items/{story}/down", headers=FETCH).status_code == 200
    keys, _ = _whole(client, client.get("/").text)
    assert card not in keys and story not in keys and len(keys) == 16


def test_a_type_chip_shows_just_that_type(db):
    _seed(db, work=6, prep=12)
    client = TestClient(create_app(db, page_size=1))  # so it pages
    first = client.get("/?type=repo").text
    assert "type=repo" in _more(first)
    keys, whole = _whole(client, first)
    assert sorted(keys) == ["insight:w1", "insight:w4"]
    assert "You're all caught up." in whole and "end of this mix" not in whole


def test_a_zero_mix_hides_the_switch(db):
    _seed(db, work=3, prep=3)
    client = TestClient(create_app(db, prep_mix=0.0))
    page = client.get("/").text
    assert "prep-switch" not in page
    assert all(k.startswith("insight:") for k in _keys(page))
