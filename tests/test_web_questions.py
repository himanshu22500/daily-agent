"""The "Ask about this" box in the reader (issue #119), through Starlette's
TestClient. Offline: a fake answerer."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient
from test_deep_dive import make_folder

from daily_agent.agents.question_answerer import AskRequest
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.prep import PrepSync, card_key
from daily_agent.feed.questions import QuestionStore, QuestionSync
from daily_agent.models import Insight
from daily_agent.web.app import create_app

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
FETCH = {"X-Requested-With": "fetch"}
ASK = "/items/insight%3Aretries/ask"


async def _answer(request: AskRequest) -> str:
    return f"Because **{request.headline}**.\n\n<script>alert(1)</script>"


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "web.db")
    InsightStore(path).add(
        Insight(
            key="insight:retries",
            text="Retry with backoff.",
            title="Back off before you retry",
            type="gotcha",
            captured_at=T0,
        )
    )
    return path


@pytest.fixture
def client(db) -> TestClient:
    return TestClient(create_app(db))


def test_the_reader_has_an_ask_box(client):
    page = client.get("/i/insight:retries").text
    assert '<section class="ask" id="ask" data-ask aria-label="Ask about this">' in page
    assert f'action="{ASK}" data-ask-form' in page
    assert 'placeholder="Ask Claude about this…"' in page
    # A pill composer with a send button; it posts without JS too.
    assert '<button type="submit" class="send" aria-label="Ask">' in page
    assert "Answered on your Mac · Telegram pings you when it's ready" in page
    # Under the text, before Read next / topics.
    assert page.index('class="reader-text"') < page.index('class="ask"')


def test_asking_returns_the_thread_with_the_question_waiting(client, db):
    res = client.post(ASK, data={"question": "Why back off?"}, headers=FETCH)
    assert res.status_code == 200
    assert '<p class="qa-q">Why back off?</p>' in res.text
    assert 'Waiting for your Mac <span class="dim">· Telegram will ping you' in res.text
    assert 'class="skeleton"' in res.text
    assert f'data-ask-poll="{ASK}"' in res.text  # the page keeps checking
    (question,) = QuestionStore(db).thread("insight:retries")
    assert question.question == "Why back off?" and question.is_open


def test_asking_without_js_redirects_to_the_question(client, db):
    res = client.post(ASK, data={"question": "Why?"}, follow_redirects=False)
    (question,) = QuestionStore(db).thread("insight:retries")
    assert res.status_code == 303
    assert res.headers["location"] == f"/i/insight%3Aretries#ask-{question.id}"
    page = client.get(res.headers["location"]).text
    assert f'id="ask-{question.id}"' in page


def test_an_empty_question_is_refused_and_kept_in_the_box(client, db):
    res = client.post(ASK, data={"question": "  "}, headers=FETCH)
    assert res.status_code == 400
    assert "Type a question first." in res.text
    res = client.post(ASK, data={"question": "x" * 1001})
    assert res.status_code == 400
    assert "too long" in res.text and "x" * 1001 in res.text  # the full reader page
    assert '<h1 class="reader-h1">Back off before you retry</h1>' in res.text
    assert QuestionStore(db).thread("insight:retries") == []


def test_cross_site_and_unknown_items_are_refused(client):
    res = client.post(
        ASK, data={"question": "Why?"}, headers={"Origin": "https://evil.example"}
    )
    assert res.status_code == 403
    assert (
        client.post("/items/insight%3Anope/ask", data={"question": "?"}).status_code
        == 404
    )
    assert client.get("/items/insight%3Anope/ask").status_code == 404


async def test_an_answer_shows_under_its_question_safely(client, db):
    QuestionStore(db).ask("insight:retries", "Why back off?", now=T0)
    await QuestionSync(db, _answer).run_once()
    fragment = client.get(ASK, headers=FETCH).text
    assert "data-ask-poll" not in fragment  # nothing open: no more checking
    assert "<p>Because <strong>Back off before you retry</strong>.</p>" in fragment
    assert "<script>" not in fragment and "&lt;script&gt;" in fragment
    assert (
        '<span class="spark" aria-hidden="true">✦</span> Claude <span class="dim">·'
        in fragment
    )
    assert 'class="skeleton"' not in fragment


def test_failed_and_retrying_questions_say_so(client, db):
    store = QuestionStore(db)
    retrying = store.ask("insight:retries", "One?", now=T0)
    store.claim(retrying.id, now=T0)
    store.fail_attempt(retrying.id, "claude error 529: overloaded", now=T0)
    failed = store.ask("insight:retries", "Two?", now=T0)
    store.give_up(failed.id, "the item is no longer in the feed")
    fragment = client.get(ASK).text
    assert 'Couldn\'t answer yet <span class="dim">· trying again ' in fragment
    assert '<p class="qa-note">Claude error 529: overloaded</p>' in fragment
    assert "Claude couldn't answer this</p>" in fragment
    assert "The item is no longer in the feed. Try asking again." in fragment


def test_flashcards_get_the_box_below_the_folded_answer(db, tmp_path):
    async def no_explainers(request):
        raise AssertionError("not written here")

    PrepSync(db, no_explainers).import_folder(make_folder(tmp_path))
    page = TestClient(create_app(db)).get(f"/i/{card_key('T5', 2)}").text
    assert page.index('<details class="answer">') < page.index('class="ask"')
    assert 'placeholder="Ask Claude about this question…"' in page


def test_the_background_loop_answers_a_new_question(db):
    sync = QuestionSync(db, _answer)
    app = create_app(db, questions=sync, question_loop=True, question_poll_seconds=30)
    with TestClient(app) as client:
        client.post(ASK, data={"question": "Why back off?"}, headers=FETCH)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "Because" in client.get(ASK).text:
                break
            time.sleep(0.05)
        assert "<p>Because <strong>Back off" in client.get(ASK).text
