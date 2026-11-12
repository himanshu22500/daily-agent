"""Interview flashcards and concept explainers in the web feed (issue #108).
Offline: a fake explainer writer."""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient
from test_deep_dive import make_folder

from daily_agent.agents.concept_writer import ConceptRequest
from daily_agent.feed.prep import PrepSync, card_key, concept_key
from daily_agent.models import VideoStory
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
    page = client.get("/?view=latest&type=interview").text
    assert "How did you make the SQL safe?" in page
    assert "Deep dive question · tap to answer" in page
    assert '<p class="snippet">A checker rejects writes' not in page
    assert ">Interview</a>" in page  # the type chip
    assert "T5 · MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7" in page


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
    assert ">Concept</a>" in client.get("/?view=latest").text


def test_sources_page_shows_prep_progress(client):
    page = client.get("/sources").text
    assert "Interview prep" in page
    assert "4 flashcards · 0 explainers written · 3 still to write" in page
    assert 'href="/?view=latest&amp;type=interview"' in page


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
