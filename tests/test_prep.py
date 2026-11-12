"""Interview prep in the feed (issue #108): importing flashcards and writing
concept explainers. Offline: a fake writer stands in for Claude."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daily_agent.agents.concept_writer import ConceptRequest
from daily_agent.feed.insights_feed import enqueue_new_insights
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.outbox import Outbox
from daily_agent.feed.prep import PrepSync, card_key, concept_key
from daily_agent.feed.story_queue import MAX_ATTEMPTS
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import VideoStory
from daily_agent.sources.deep_dive import DeepDiveError

from test_deep_dive import POINT, make_folder

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def setup(tmp_path):
    db = str(tmp_path / "prep.db")
    folder = make_folder(tmp_path)
    requests: list[ConceptRequest] = []
    failing: set[str] = set()

    async def writer(request: ConceptRequest) -> VideoStory:
        if request.concept in failing:
            raise RuntimeError("claude -p exited 1\nusage limit")
        requests.append(request)
        return VideoStory(
            headline=f"Explaining {request.concept}"[:70],
            text=f"{request.concept} explained in detail.",
            tags=["databases"],
        )

    clock = Clock()
    return PrepSync(db, writer, clock=clock), folder, requests, failing, clock, db


def test_import_adds_web_only_flashcards_labelled_by_point(setup):
    sync, folder, _, _, _, db = setup
    report = sync.import_folder(folder)
    assert (report.points, report.cards_added, report.unanswered) == (2, 4, 1)
    assert report.concepts_queued == 3
    card = InsightStore(db).get(card_key("T5", 2))
    assert card.type == "interview" and card.status == "web-only"
    assert card.title == "Why move at all?"
    assert card.text.endswith("**Still open:** the business reason.")
    assert card.tags == ["t5", "surface"] and card.source_session == "prep:t5"
    assert enqueue_new_insights(InsightStore(db), Outbox(db)) == 0  # not Telegram
    entry = WebFeed(db).entry(card_key("T5", 4))
    assert entry.source == "T5 · MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7"
    assert entry.flashcard and entry.prep_level == "Deep dive"
    assert entry.snippet == "Deep dive question · tap to answer"
    assert "checker" not in entry.snippet
    assert ("interview", 4) in WebFeed(db).types()


def test_reimport_updates_answers_and_drops_removed_questions(setup):
    sync, folder, _, _, clock, db = setup
    sync.import_folder(folder)
    feed = WebFeed(db)
    feed.set_saved(card_key("T5", 1), True)
    edited = POINT.replace("with `DMS`.", "with `DMS` and reverse CDC.").replace(
        "**Q5. Explain expand/contract migrations.** _(adjacent fundamentals)_\n"
        "- **Answer:** Add the new shape, migrate, then remove the old one.\n",
        "",
    )
    (folder / "01-tranzact" / "05-mysql.md").write_text(edited, encoding="utf-8")
    clock.now += timedelta(days=1)
    report = sync.import_folder(folder)
    assert (report.cards_added, report.cards_updated, report.cards_removed) == (0, 1, 1)
    entry = WebFeed(db).entry(card_key("T5", 1))
    assert entry.text.endswith("with `DMS` and reverse CDC.") and entry.saved
    assert InsightStore(db).get(card_key("T5", 5)) is None
    again = sync.import_folder(folder)
    assert (again.cards_added, again.cards_updated, again.concepts_queued) == (0, 0, 0)


async def test_explainers_are_written_from_the_points_notes(setup):
    sync, folder, requests, _, _, db = setup
    sync.import_folder(folder)
    report = await sync.run_once(limit=2)
    assert report.written == 2 and report.remaining == 1
    first = next(r for r in requests if r.concept == "AWS DMS: full load + CDC")
    assert first.point_id == "T5" and first.group == "TranZact"
    assert first.point_name == "MySQL 8 → PostgreSQL 18 on AWS DMS + UUIDv7"
    assert first.notes == POINT and "Led the production" in first.resume_line
    explainer = InsightStore(db).get(concept_key("t5-c1"))
    assert explainer.type == "concept" and explainer.status == "web-only"
    assert explainer.title == "Explaining AWS DMS: full load + CDC"
    assert explainer.tags == ["databases", "t5"]
    assert WebFeed(db).entry(concept_key("t5-c1")).source.startswith("T5 · ")
    progress = sync.store.progress()
    assert (progress.cards, progress.explainers, progress.queued) == (4, 2, 1)


async def test_a_changed_concept_is_rewritten_in_place(setup):
    sync, folder, requests, _, clock, db = setup
    sync.import_folder(folder)
    while (await sync.run_once()).remaining:
        pass
    edited = POINT.replace(
        "- AWS DMS: full load + CDC", "- AWS DMS: CDC and reverse CDC"
    )
    edited = edited.replace("- UUIDv4 vs UUIDv7: B-tree locality\n", "")
    (folder / "01-tranzact" / "05-mysql.md").write_text(edited, encoding="utf-8")
    clock.now += timedelta(days=1)
    report = sync.import_folder(folder)
    assert report.concepts_queued == 1 and report.concepts_removed == 1
    assert InsightStore(db).get(concept_key("t5-c2")) is None
    await sync.run_once()
    assert requests[-1].concept == "AWS DMS: CDC and reverse CDC"
    assert InsightStore(db).get(concept_key("t5-c1")).title == (
        "Explaining AWS DMS: CDC and reverse CDC"
    )


async def test_failed_explainers_back_off_and_reimport_retries_them(setup):
    sync, folder, _, failing, clock, _ = setup
    failing.add("What TranZact does")
    sync.import_folder(folder)
    report = await sync.run_once()
    assert report.written == 2 and "claude -p exited 1" in report.errors[0]
    for _ in range(MAX_ATTEMPTS):
        clock.now += timedelta(hours=8)
        await sync.run_once()
    progress = sync.store.progress()
    assert progress.failed == [("What TranZact does", "writing: claude -p exited 1")]
    failing.clear()
    assert sync.import_folder(folder).concepts_queued == 1
    assert (await sync.run_once()).written == 1
    assert sync.store.progress().failed == []


def test_import_errors(setup, tmp_path):
    sync, *_ = setup
    with pytest.raises(DeepDiveError):
        sync.import_folder(tmp_path / "nowhere")
