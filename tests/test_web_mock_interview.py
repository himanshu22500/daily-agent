""" "Interview me" in the Ask box of prep cards (issue #133), through
Starlette's TestClient. Offline: a fake interviewer."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from daily_agent import cli
from daily_agent.agents.mock_interviewer import (
    VERDICT,
    InterviewReply,
    InterviewRequest,
)
from daily_agent.config import Settings
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.mock_interviews import InterviewStore, InterviewSync
from daily_agent.feed.prep_tracks import PREP_TYPES
from daily_agent.feed.questions import QuestionStore, QuestionSync
from daily_agent.models import Insight
from daily_agent.web.app import create_app

T0 = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
FETCH = {"X-Requested-With": "fetch"}
KEY = "prep:t1-q1"
PAGE = "/i/prep:t1-q1"
MOCK = "/items/prep%3At1-q1/interview"
ASK = "/items/prep%3At1-q1/ask"


async def _interviewer(request: InterviewRequest) -> InterviewReply:
    if request.task == VERDICT:
        return InterviewReply(
            question="Clear on the why.\n\n**What was missing**\n• How you measured it",
            grade="shaky",
        )
    return InterviewReply(
        question=f"Question {request.asked + 1}: <script>alert(1)</script>?",
        feedback="" if request.task == "open" else "Good, but **why** that index?",
    )


async def _answer(request) -> str:
    return "An answer."


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "web.db")
    store = InsightStore(path)
    store.add(
        Insight(
            key=KEY,
            text="We moved reads to a replica and kept writes on the primary.",
            title="How did you scale the reads?",
            type="interview",
            tags=["t1", "deep-dive"],
            captured_at=T0,
        )
    )
    store.add(
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
def sync(db) -> InterviewSync:
    return InterviewSync(db, _interviewer)


@pytest.fixture
def client(db, sync) -> TestClient:
    return TestClient(
        create_app(db, questions=QuestionSync(db, _answer, interviews=sync))
    )


def _interview_id(html: str) -> str:
    return re.search(r'name="interview" value="(m[0-9a-f]+)"', html).group(1)


def test_prep_cards_offer_interview_me_and_other_items_dont(client):
    page = client.get(PAGE).text
    assert (
        f'<form class="mock-start" method="post" action="{MOCK}" data-ask-form>' in page
    )
    assert 'name="do" value="start"' in page and "Interview me</button>" in page
    assert "4 questions, then a mid-level verdict" in page
    # The Ask composer is still there, first.
    assert page.index('placeholder="Ask Claude about this question…"') < page.index(
        "Interview me"
    )
    other = client.get("/i/insight:retries").text
    assert "Interview me" not in other and "/interview" not in other


@pytest.mark.parametrize("prep_type", PREP_TYPES)
def test_every_prep_type_offers_interview_me(db, prep_type):
    key = f"prep:any:{prep_type}"
    InsightStore(db).add(
        Insight(
            key=key,
            text="Design a URL shortener.",
            title="URL shortener",
            type=prep_type,
            captured_at=T0,
        )
    )
    client = TestClient(create_app(db))
    assert "Interview me</button>" in client.get(f"/i/{key}").text
    res = client.post(
        f"/items/{key.replace(':', '%3A')}/interview",
        data={"do": "start"},
        headers=FETCH,
    )
    assert res.status_code == 200 and 'data-ask-mode="mock"' in res.text


def test_starting_shows_the_interview_waiting_for_the_mac(client, db):
    res = client.post(MOCK, data={"do": "start"}, headers=FETCH)
    assert res.status_code == 200
    html = res.text
    assert 'data-ask-mode="mock"' in html and f'data-ask-poll="{ASK}"' in html
    assert '<span class="mock-badge">Mock interview</span>' in html
    assert '<span class="mock-count">Question 1 of 4</span>' in html
    assert html.count("<i></i>") == 3 + 3  # dots (1 of 4 on) + the shimmer
    assert "Waiting for your Mac" in html and 'class="skeleton"' in html
    # While it runs, the composer answers the interviewer.
    assert 'name="answer"' in html and 'name="question"' not in html
    assert 'value="end" formnovalidate' in html and "End &amp; grade" in html
    assert "Start over" in html and "Interview me" not in html
    (interview,) = InterviewStore(db).for_item(KEY)
    assert _interview_id(html) == interview.id


def test_start_over_while_the_mac_is_away(client, db):
    first = _interview_id(client.post(MOCK, data={"do": "start"}, headers=FETCH).text)
    res = client.post(
        MOCK, data={"interview": first, "do": "start", "answer": ""}, headers=FETCH
    )
    assert res.status_code == 200 and _interview_id(res.text) != first
    assert '<span class="mock-count">Stopped after 0 of 4</span>' in res.text
    assert '<p class="mock-sys">Stopped before the interviewer replied</p>' in res.text
    assert res.text.count('class="skeleton"') == 1  # only the new opening waits
    old, new = InterviewStore(db).for_item(KEY)
    assert old.id == first and old.turns[0].skipped and new.waiting


async def test_a_whole_interview_in_the_ask_box(client, sync):
    interview_id = _interview_id(
        client.post(MOCK, data={"do": "start"}, headers=FETCH).text
    )
    await sync.run_once()
    html = client.get(ASK, headers=FETCH).text
    assert "data-ask-poll" not in html  # nothing open: no more checking
    assert '<span class="spark" aria-hidden="true">✦</span> Interviewer' in html
    assert "<script>" not in html and "&lt;script&gt;" in html  # model output
    assert 'placeholder="Your answer…"' in html

    for n in range(1, 4):
        res = client.post(
            MOCK,
            data={"interview": interview_id, "answer": f"Answer {n}", "do": "reply"},
            headers=FETCH,
        )
        assert res.status_code == 200
        assert f'<p class="qa-q">Answer {n}</p>' in res.text
        assert 'class="skeleton"' in res.text and "data-ask-poll" in res.text
        await sync.run_once()
    html = client.get(ASK).text
    assert '<span class="mock-count">Question 4 of 4</span>' in html
    assert '<div class="mock-feedback"><p>Good, but <strong>why</strong>' in html

    # The last answer ends it; the box goes back to asking.
    res = client.post(
        MOCK,
        data={"interview": interview_id, "answer": "Answer 4", "do": "reply"},
        headers=FETCH,
    )
    assert "Grading…" in res.text and 'data-ask-mode="grading"' in res.text
    assert "Telegram pings you with the verdict" in res.text
    assert 'name="question"' in res.text and "Interview me" not in res.text
    await sync.run_once()
    html = client.get(ASK).text
    assert "data-ask-mode" not in html  # the poll swaps the whole box back
    assert '<span class="mock-count">Graded · 4 questions</span>' in html
    assert (
        '<p class="grade-chip g-shaky">Suggested self-grade: <strong>Shaky</strong></p>'
        in html
    )
    assert "<strong>What was missing</strong><br>• How you measured it" in html
    assert "Interview me" in html and 'name="question"' in html


async def test_end_and_grade_and_its_message(client, sync, db):
    interview_id = _interview_id(
        client.post(MOCK, data={"do": "start"}, headers=FETCH).text
    )
    res = client.post(
        MOCK, data={"interview": interview_id, "do": "end", "answer": ""}, headers=FETCH
    )
    # Waiting for the opening question: nothing to end yet.
    assert res.status_code == 409 and "still on your last answer" in res.text
    await sync.run_once()
    res = client.post(
        MOCK, data={"interview": interview_id, "do": "end", "answer": ""}, headers=FETCH
    )
    assert res.status_code == 400
    assert "Answer at least one question first" in res.text
    res = client.post(
        MOCK,
        data={"interview": interview_id, "do": "end", "answer": "Replicas."},
        headers=FETCH,
    )
    assert res.status_code == 200 and "Grading…" in res.text
    assert '<p class="qa-q">Replicas.</p>' in res.text


async def test_ending_without_an_answer_says_so(client, sync):
    interview_id = _interview_id(
        client.post(MOCK, data={"do": "start"}, headers=FETCH).text
    )
    await sync.run_once()
    client.post(
        MOCK, data={"interview": interview_id, "answer": "Replicas."}, headers=FETCH
    )
    await sync.run_once()
    res = client.post(
        MOCK, data={"interview": interview_id, "do": "end"}, headers=FETCH
    )
    assert '<p class="mock-sys">You ended the interview</p>' in res.text


def test_without_js_steps_redirect_to_the_turn(client, db):
    res = client.post(MOCK, data={"do": "start"}, follow_redirects=False)
    assert res.status_code == 303
    (interview,) = InterviewStore(db).for_item(KEY)
    opening = interview.turns[0]
    assert res.headers["location"] == f"/i/prep%3At1-q1#turn-{opening.id}"
    page = client.get(res.headers["location"]).text
    assert f'id="turn-{opening.id}"' in page and f'id="mock-{interview.id}"' in page


async def test_a_refused_answer_is_kept_in_the_box(client, sync):
    interview_id = _interview_id(
        client.post(MOCK, data={"do": "start"}, headers=FETCH).text
    )
    res = client.post(
        MOCK, data={"interview": interview_id, "answer": "Too soon"}, headers=FETCH
    )
    assert res.status_code == 409
    assert "The interviewer is still on your last answer." in res.text
    assert ">Too soon</textarea>" in res.text
    await sync.run_once()
    res = client.post(
        MOCK, data={"interview": interview_id, "answer": "x" * 2001}
    )  # without JS: the whole reader page
    assert res.status_code == 400 and "too long" in res.text
    assert '<h1 class="reader-h1">How did you scale the reads?</h1>' in res.text


def test_mock_interviews_are_for_prep_cards_only(client):
    res = client.post(
        "/items/insight%3Aretries/interview", data={"do": "start"}, headers=FETCH
    )
    assert res.status_code == 400
    assert "Mock interviews are for interview-prep cards." in res.text
    assert client.post(MOCK, data={"do": "dance"}, headers=FETCH).status_code == 400


def test_cross_site_and_unknown_items_are_refused(client):
    res = client.post(
        MOCK, data={"do": "start"}, headers={"Origin": "https://evil.example"}
    )
    assert res.status_code == 403
    assert (
        client.post("/items/prep%3Anope/interview", data={"do": "start"}).status_code
        == 404
    )
    assert client.get(MOCK).status_code == 405


async def test_questions_and_interviews_share_the_thread_in_time_order(
    client, sync, db
):
    client.post(ASK, data={"question": "What's a replica?"}, headers=FETCH)
    time.sleep(0.01)
    client.post(MOCK, data={"do": "start"}, headers=FETCH)
    html = client.get(ASK).text
    assert html.index("What&#39;s a replica?") < html.index("Mock interview")
    assert len(QuestionStore(db).thread(KEY)) == 1


def test_the_question_count_comes_from_the_setting(db):
    sync = InterviewSync(db, _interviewer, questions=2)
    app = create_app(db, questions=QuestionSync(db, _answer, interviews=sync))
    client = TestClient(app)
    assert "2 questions, then a mid-level verdict" in client.get(PAGE).text
    html = client.post(MOCK, data={"do": "start"}, headers=FETCH).text
    assert '<span class="mock-count">Question 1 of 2</span>' in html


def test_the_cli_wires_the_interviewer_with_the_setting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("DAILY_AGENT_INTERVIEW_QUESTIONS", "3")
    sync = cli._question_sync(Settings())
    assert sync.interviews is not None and sync.interviews.questions == 3


def test_the_background_loop_runs_a_turn(db):
    sync = QuestionSync(db, _answer, interviews=InterviewSync(db, _interviewer))
    app = create_app(db, questions=sync, question_loop=True, question_poll_seconds=30)
    with TestClient(app) as client:
        client.post(MOCK, data={"do": "start"}, headers=FETCH)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "Interviewer <span" in client.get(ASK).text:
                break
            time.sleep(0.05)
        assert "Question 1: &lt;script&gt;" in client.get(ASK).text
