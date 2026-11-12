"""Interview flashcards and concept explainers in the web feed (issue #108).
Offline: a fake explainer writer."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient
from test_deep_dive import make_folder

from daily_agent.agents.concept_writer import ConceptRequest
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.prep import PrepSync, card_key, concept_key, position_in_point
from daily_agent.models import Insight, VideoStory
from daily_agent.web.app import create_app


async def _writer(request: ConceptRequest) -> VideoStory:
    return VideoStory(
        headline=f"About {request.concept}"[:70],
        text="What it is and why it matters.\n\nHow it showed up in your work.",
        tags=["databases"],
    )


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "web.db")


@pytest.fixture
def client(db, tmp_path) -> TestClient:
    sync = PrepSync(db, _writer)
    sync.import_folder(make_folder(tmp_path))
    return TestClient(create_app(db, prep=sync))


def test_flashcard_cards_never_show_the_answer(client):
    page = client.get("/?view=interview").text
    assert "How did you make the SQL safe?" in page
    assert "Deep dive question · tap to answer" in page
    assert '<p class="snippet">A checker rejects writes' not in page
    assert "T5 · MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7" in page
    # The question is the card: no decorative cover.
    card = page[page.index('class="story t-interview') :]
    card = card[: card.index("</article>")]
    assert "is-flashcard" in card and 'class="cover' not in card


def test_the_reader_folds_the_answer_until_tapped(client):
    reader = client.get(f"/i/{card_key('T5', 2)}").text
    assert '<h1 class="reader-h1">Why move at all?</h1>' in reader
    assert "Interview question · <strong>T5 · MySQL 8" in reader
    assert '<p class="dateline">Surface' in reader
    folded = reader.index('<details class="answer">')
    assert reader.index("Show answer") > folded
    # The visible answer is inside the fold (share/copy carry it as data only).
    shown = reader.index('<div class="reader-text"><p>Context: the MySQL master')
    assert shown > folded
    assert "<strong>Still open:</strong> the business reason." in reader
    assert 'class="reader-figure"' not in reader  # no cover for a flashcard


async def test_concept_explainers_read_like_articles(client, db):
    await PrepSync(db, _writer).run_once()
    reader = client.get(f"/i/{concept_key('t5-c1')}").text
    assert "Concept from your resume · <strong>T5 · MySQL 8" in reader
    assert '<div class="reader-text"><p>What it is and why it matters.</p>' in reader
    assert '<details class="answer">' not in reader
    assert concept_key("t5-c1") in client.get("/?view=interview").text
    assert concept_key("t5-c1") not in client.get("/?view=latest").text


def test_sources_page_shows_prep_progress(client):
    page = client.get("/sources").text
    assert "Interview prep" in page
    assert "4 flashcards · 0 explainers written · 3 still to write" in page
    assert 'href="/?view=interview"' in page


def test_background_loop_writes_the_explainers(db, tmp_path):
    sync = PrepSync(db, _writer)
    sync.import_folder(make_folder(tmp_path))
    app = create_app(db, prep=sync, prep_loop=True, video_idle_seconds=30)
    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "3 explainers written" in client.get("/sources").text:
                break
            time.sleep(0.05)
        assert "4 flashcards · 3 explainers written" in client.get("/sources").text


T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def _keys(page: str) -> list[str]:
    keys = re.findall(r'data-key="([^"]+)"', page)
    return list(dict.fromkeys(keys))


@pytest.fixture
def mixed(db, tmp_path) -> TestClient:
    """Prep from two points (a job point and a skills point), plus one coding
    insight; a page holds 3 cards."""
    InsightStore(db).add(
        Insight(key="insight:uv-sync", text="uv sync prunes extras.", captured_at=T0)
    )
    sync = PrepSync(db, _writer)
    sync.import_folder(make_folder(tmp_path), jobs_only=False)
    return TestClient(create_app(db, prep=sync, page_size=3))


async def test_prep_lives_only_in_the_interview_tab(mixed, db):
    await PrepSync(db, _writer).run_once()  # the explainers, too
    for url in ("/", "/?view=latest"):
        page = mixed.get(url).text
        assert _keys(page) == ["insight:uv-sync"]
        assert ">Interview</a>" not in page.split('class="chips"')[1]  # no type chip
        assert ">Concept</a>" not in page
    assert ">1 unseen<" in mixed.get("/").text  # prep isn't counted
    assert mixed.get("/new-count?since=2000-01-01T00:00:00").json() == {"count": 1}
    # The tab itself, next to the others.
    assert '<a class="tab" href="/?view=interview">Interview</a>' in mixed.get("/").text
    # Old links to the Interview chip land on the tab.
    moved = mixed.get("/?view=latest&type=interview", follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"] == "/?view=interview"
    # Search still finds flashcards (by question or answer), and a saved one
    # shows in Saved.
    assert card_key("T5", 4) in _keys(mixed.get("/?view=latest&q=SQL safe").text)
    assert card_key("T5", 4) in _keys(mixed.get("/?view=latest&q=checker").text)
    mixed.post(f"/items/{card_key('T5', 4)}/save")
    assert _keys(mixed.get("/?view=saved").text) == [card_key("T5", 4)]


async def test_interview_tab_is_in_resume_point_order(mixed, db):
    await PrepSync(db, _writer).run_once()
    first = mixed.get("/?view=interview").text
    chips = re.findall(r'class="chip[^"]*"[^>]*>([^<]+)</a>', first)
    assert chips == [
        "All points",
        "T0 · Role overview",
        "T5 · MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7",
        "S1 · Languages (Python, SQL)",
    ]
    seen, url = [], "/?view=interview"
    while url:
        page = mixed.get(url + ("&partial=1" if seen else "")).text
        seen += _keys(page)
        more = re.search(r'class="more" href="([^"]+)"', page)
        url = more.group(1).replace("&amp;", "&") if more else None
    # Questions in order, then the point's explainers; tracker order of points.
    assert seen == [
        concept_key("t0-c1"),
        card_key("T5", 1),
        card_key("T5", 2),
        card_key("T5", 4),
        card_key("T5", 5),
        concept_key("t5-c1"),
        concept_key("t5-c2"),
        card_key("S1", 1),
    ]
    one = mixed.get("/?view=interview&point=s1").text
    assert _keys(one) == [card_key("S1", 1)]
    assert 'class="chip chip-point on"' in one


def test_questions_sort_by_number_then_explainers():
    keys = ["prep:t5-c1", "prep:t5-q10", "prep:t5-q2", "prep:t5-q1"]
    assert sorted(keys, key=position_in_point) == [
        "prep:t5-q1",
        "prep:t5-q2",
        "prep:t5-q10",
        "prep:t5-c1",
    ]


def test_read_next_keeps_prep_and_insights_apart(mixed):
    def read_next(url: str) -> str:
        return mixed.get(url).text.split('<section class="read-next"')[1]

    under_card = read_next(f"/i/{card_key('T5', 1)}")
    assert "insight:uv-sync" not in under_card and "prep%3At5-q2" in under_card
    assert "prep%3A" not in mixed.get("/i/insight:uv-sync").text
