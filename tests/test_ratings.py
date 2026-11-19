"""Rating feed items 👍 / 👎 and teaching the extractor with it (issue #126).
All offline: tmp_path SQLite and a monkeypatched Claude call."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daily_agent.agents import insight_extractor
from daily_agent.agents.claude_code import ClaudeResult
from daily_agent.cli import _extractor
from daily_agent.config import Settings
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.ratings import (
    DOWN,
    UP,
    Ratings,
    clean_reasons,
    extraction_feedback,
)
from daily_agent.feed.transcripts import TranscriptMessage
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import Insight

T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)


def _insight(db: str, key: str, title: str, *, type="gotcha", session="s1") -> None:
    InsightStore(db).add(
        Insight(
            key=key,
            text=f"About {title}.",
            title=title,
            type=type,
            source_session=session,
            captured_at=T0,
        )
    )


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "ratings.db")
    # Session s1 was in project "daily-agent", s2 in "tz-repotring".
    store = InsightStore(path)
    store.set_cursor(str(tmp_path / "-Users-me-daily-agent" / "s1.jsonl"), 1)
    store.set_cursor(str(tmp_path / "-Users-me-tz-repotring" / "s2.jsonl"), 1)
    _insight(path, "insight:curl", "curl checks don't record views", type="technique")
    _insight(path, "insight:pg", "Postgres misestimated rows 653x", session="s2")
    _insight(path, "insight:pwa", "Stale PWA pages show fragments unstyled")
    return path


def test_rate_replace_and_clear(db):
    ratings = Ratings(db)
    assert ratings.get("insight:curl") is None
    ratings.rate("insight:curl", DOWN, now=T0)
    assert ratings.set_reasons("insight:curl", ["source", "basic", "nonsense", "basic"])
    rating = ratings.get("insight:curl")
    assert rating.down and not rating.up
    assert rating.reasons == ("basic", "source")  # known codes, the sheet's order
    assert rating.rated_at == T0
    # A new rating replaces the old one, reasons and all.
    ratings.rate("insight:curl", UP, now=T0 + timedelta(minutes=1))
    rating = ratings.get("insight:curl")
    assert rating.up and rating.reasons == ()
    assert not ratings.set_reasons("insight:curl", ["basic"])  # only a 👎 has reasons
    assert not ratings.set_reasons("insight:nope", ["basic"])
    assert ratings.clear("insight:curl").up
    assert ratings.get("insight:curl") is None and ratings.clear("insight:curl") is None
    with pytest.raises(ValueError):
        ratings.rate("insight:curl", "meh")


def test_clean_reasons():
    assert clean_reasons(["repeat ", "type", "x", ""]) == ["repeat", "type"]
    assert clean_reasons([]) == []


def test_recent_is_newest_first_by_kind_and_prefix(db):
    ratings = Ratings(db)
    ratings.rate("insight:curl", DOWN, now=T0)
    ratings.rate("insight:pwa", DOWN, now=T0 + timedelta(hours=1))
    ratings.rate("insight:pg", UP, now=T0)
    ratings.rate("video:abcdefghijk", DOWN, now=T0 + timedelta(hours=2))
    assert [r.key for r in ratings.recent(DOWN, limit=5)] == [
        "video:abcdefghijk",
        "insight:pwa",
        "insight:curl",
    ]
    assert [r.key for r in ratings.recent(DOWN, limit=1, prefix="insight:")] == [
        "insight:pwa"
    ]
    assert [r.key for r in ratings.recent(UP, limit=5)] == ["insight:pg"]


def test_no_ratings_no_feedback(db):
    assert extraction_feedback(db) == ""
    Ratings(db).rate("video:abcdefghijk", DOWN)  # not the extractor's
    assert extraction_feedback(db) == ""


def test_extraction_feedback_names_items_reasons_projects_and_types(db):
    feed = WebFeed(db)
    curl_src = feed.entry("insight:curl").source
    pg_src = feed.entry("insight:pg").source
    assert curl_src and pg_src and curl_src != pg_src
    ratings = Ratings(db)
    ratings.rate("insight:curl", DOWN, now=T0)
    ratings.set_reasons("insight:curl", ["basic", "source", "type"])
    ratings.rate("insight:pwa", DOWN, now=T0 + timedelta(minutes=1))
    ratings.rate("insight:pg", UP, now=T0)
    ratings.rate("insight:gone", DOWN, now=T0)  # its item was deleted: skipped
    note = extraction_feedback(db)
    assert note.splitlines() == [
        "Not useful (the reader removed these from the feed):",
        f"- [gotcha · {curl_src}] Stale PWA pages show fragments unstyled",
        f"- [technique · {curl_src}] curl checks don't record views (too basic)",
        "Useful (the reader wants more like these):",
        f"- [gotcha · {pg_src}] Postgres misestimated rows 653x",
        f"The reader wants fewer insights from these projects: {curl_src}. For a "
        "session in one of them, keep only clearly durable, non-obvious insights.",
        "The reader wants fewer insights of these types: technique.",
    ]


def _messages(session="s1") -> list[TranscriptMessage]:
    return [TranscriptMessage(session, "main", "2026-11-14T09:00:00Z", "user", "hi")]


def test_the_prompt_carries_the_project_and_the_feedback():
    prompt = insight_extractor._render(
        _messages(), feedback="Not useful (…):\n- [gotcha] x", project="daily-agent"
    )
    assert "PROJECT: daily-agent" in prompt
    assert "READER FEEDBACK:\nNot useful (…):\n- [gotcha] x" in prompt
    assert prompt.index("READER FEEDBACK") < prompt.index("MESSAGES:")
    plain = insight_extractor._render(_messages())
    assert "PROJECT" not in plain and "READER FEEDBACK" not in plain
    assert "READER FEEDBACK" in insight_extractor._SYSTEM_PROMPT


async def test_extract_insights_sends_the_feedback_to_claude(monkeypatch):
    sent = {}

    async def fake_run(prompt, **kw):
        sent["prompt"] = prompt
        return ClaudeResult(text="", structured={"candidates": []}, cost_usd=0.0)

    monkeypatch.setattr(insight_extractor, "run_claude", fake_run)
    await insight_extractor.extract_insights(
        "claude-code:sonnet", _messages(), feedback="- [gotcha] x", project="p"
    )
    assert "PROJECT: p" in sent["prompt"] and "- [gotcha] x" in sent["prompt"]


async def test_the_cli_extractor_knows_each_sessions_project(db, tmp_path, monkeypatch):
    Ratings(db).rate("insight:curl", DOWN)
    seen = []

    async def fake_extract(model, messages, *, feedback, project):
        seen.append((project, feedback))
        return []

    monkeypatch.setattr("daily_agent.cli.extract_insights", fake_extract)
    files = [
        tmp_path / "-Users-me-daily-agent" / "s1.jsonl",
        tmp_path / "-Users-me-tz-repotring" / "s2.jsonl",
    ]
    extract = _extractor(Settings(db_path=db, database_url=""), files)
    await extract(_messages("s2"))
    await extract(_messages("unknown"))
    (project, feedback), (none, _) = seen
    assert project == WebFeed(db).entry("insight:pg").source and none == ""
    assert "curl checks don't record views" in feedback


async def test_the_cli_extractor_goes_on_without_ratings(tmp_path, monkeypatch):
    def broken(db):
        raise RuntimeError("database is down")

    monkeypatch.setattr("daily_agent.cli.extraction_feedback", broken)
    seen = []

    async def fake_extract(model, messages, *, feedback, project):
        seen.append(feedback)
        return []

    monkeypatch.setattr("daily_agent.cli.extract_insights", fake_extract)
    await _extractor(Settings(db_path=str(tmp_path / "x.db"), database_url=""), [])(
        _messages()
    )
    assert seen == [""]
