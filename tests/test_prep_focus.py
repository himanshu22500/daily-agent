"""Interview-prep focus (issue #162): work insights off, Interview tab home.

With ``work_insights`` off, capture stores no work insights (study sessions
still become practice cards), the feed hides the ones already stored, and the
sweep writes nothing for them. ``web_home`` picks the tab ``/`` opens.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.agents import insight_extractor
from daily_agent.agents.claude_code import ClaudeResult
from daily_agent.config import Settings
from daily_agent.feed.insights_capture import collect_file
from daily_agent.feed.insights_store import InsightStore, is_work_insight
from daily_agent.feed.slides import SlideStore, generate_slides, picked
from daily_agent.feed.transcripts import TranscriptMessage
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import (
    Insight,
    InsightCandidate,
    InsightExtraction,
    PrepCard,
)
from daily_agent.web.app import create_app

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=1)
WORK = ("insight:gotcha-a", "insight:repo-b")
OTHER = ("video:vid1", "prep:design:cache")


def _item(key: str, *, type: str, minutes: int) -> Insight:
    return Insight(
        key=key,
        text=f"About {key}, at some length so it reads like a story.",
        type=type,
        tags=["caching"],
        captured_at=T0,
        stored_at=T0 + timedelta(minutes=minutes),
    )


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "focus.db")
    store = InsightStore(path)
    store.add(_item(WORK[0], type="gotcha", minutes=1))
    store.add(_item(WORK[1], type="repo", minutes=2))
    store.add(_item(OTHER[0], type="video", minutes=3))
    store.add(_item(OTHER[1], type="design", minutes=4))
    return path


def _keys(entries) -> set[str]:
    return {e.key for e in entries}


def test_defaults_put_interview_prep_first():
    settings = Settings(_env_file=None)
    assert settings.work_insights is False
    assert settings.web_home == "interview"
    assert is_work_insight("insight:x") and not is_work_insight("prep:x")


# --- the feed ----------------------------------------------------------------- #


def test_the_feed_shows_work_insights_until_they_are_turned_off(db):
    assert _keys(WebFeed(db).page(view="latest").entries) == {*WORK, OTHER[0]}


def test_with_work_insights_off_no_list_shows_them(db):
    on, off = WebFeed(db), WebFeed(db, work_insights=False)
    for key in WORK:
        on.set_saved(key, True)
    on.set_hidden(WORK[0], True)
    on.set_saved(OTHER[0], True)

    assert _keys(off.page(view="latest").entries) == {OTHER[0]}
    assert _keys(off.page(view="saved").entries) == {OTHER[0]}
    assert off.page(view="hidden").entries == []
    assert _keys(off.page(view="latest", query="About").entries) == set(OTHER)
    assert off.types() == [("video", 1)]
    assert off.unseen_count() == 1
    assert off.count_published_after(T0) == 1
    assert _keys(off.related(OTHER[0])) == set()  # the only other item is prep

    for mix in (0.0, 0.5):
        edition = off.new_edition(prep_mix=mix, now=NOW)
        assert not any(map(is_work_insight, _keys(off.edition_page(edition).entries)))

    # A direct link (an old Telegram message) still opens one.
    assert off.entry(WORK[1]) is not None


def test_an_edition_built_before_the_switch_drops_work_insights(db):
    edition = WebFeed(db).new_edition(now=NOW)
    off = WebFeed(db, work_insights=False)
    assert _keys(off.edition_page(edition).entries) == {OTHER[0]}


def test_hidden_work_insights_never_set_the_next_revisit(db):
    on = WebFeed(db)
    on.record_views([WORK[0]], session_id="s1", now=NOW)
    assert on.next_revisit_at(now=NOW) is not None
    assert WebFeed(db, work_insights=False).next_revisit_at(now=NOW) is None


# --- the app ------------------------------------------------------------------ #


def _client(db: str, **kwargs) -> TestClient:
    return TestClient(create_app(db, allowed_hosts=None, **kwargs))


def _tab_links(body: str) -> dict[str, str]:
    return {
        name: html.unescape(href)
        for href, name in re.findall(r'class="tab[^"]*" href="([^"]+)">([^<]+)<', body)
    }


def test_home_opens_the_interview_tab_and_links_for_you_by_name(db):
    client = _client(db, home="interview", work_insights=False)
    home = client.get("/").text
    assert 'data-view="interview"' in home
    assert OTHER[1] in home and WORK[0] not in home
    tabs = _tab_links(home)
    assert tabs["Interview"] == "/" and tabs["For you"] == "/?view=for-you"

    for_you = client.get("/?view=for-you").text
    assert 'data-view="for-you"' in for_you
    assert not any(key in for_you for key in WORK)

    switched = client.post("/prefs/prep-mode", data={"on": "0"}, follow_redirects=False)
    assert switched.headers["location"] == "/?view=for-you"


def test_for_you_stays_home_by_default(db):
    body = _client(db).get("/").text
    assert 'data-view="for-you"' in body
    assert _tab_links(body)["For you"] == "/"


def test_an_unknown_home_tab_is_refused(db):
    with pytest.raises(ValueError, match="unknown home tab"):
        create_app(db, home="news")


# --- capture ------------------------------------------------------------------ #

CANDIDATE = InsightCandidate(
    text="Postgres needs ORDER BY tiebreakers for stable keyset paging.",
    canonical_key="keyset-tiebreakers",
    type="gotcha",
    score=0.8,
)
CARD = PrepCard(
    track="design",
    question="How does consistent hashing limit remapping?",
    answer="Only about 1/n of keys move when a node joins.",
    tags=["consistent-hashing"],
)


def _transcript(tmp_path: Path, *texts: str, cwd: str = "/x/work") -> Path:
    path = tmp_path / "projects" / "-x" / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "s1",
                    "cwd": cwd,
                    "timestamp": "2026-11-25T08:00:00Z",
                    "message": {"content": t},
                }
            )
            for t in texts
        )
        + "\n"
    )
    os.utime(path, (time.time() - 3600, time.time() - 3600))
    return path


async def test_with_work_off_a_work_session_stores_nothing(tmp_path):
    path = _transcript(tmp_path, "Fix the pager", "insight: tiebreakers matter")
    store = InsightStore(tmp_path / "db.sqlite")

    async def extractor(messages):
        return InsightExtraction(candidates=[CANDIDATE])

    result = await collect_file(store, path, "insight:", extractor, work=False)
    assert (result.new, result.marked, result.extracted) == (0, 0, 0)
    assert store.all() == []
    assert store.cursor(str(path)) == 2  # read, so it isn't read again


async def test_with_work_off_a_study_session_still_brings_cards(tmp_path):
    path = _transcript(tmp_path, "Mock round: design a cache")
    store = InsightStore(tmp_path / "db.sqlite")

    async def extractor(messages):
        return InsightExtraction(study_session=True, prep_cards=[CARD])

    result = await collect_file(store, path, "insight:", extractor, work=False)
    assert (result.cards, result.new) == (1, 0)
    assert [i.type for i in store.all()] == ["design"]


def _messages() -> list[TranscriptMessage]:
    return [TranscriptMessage("s1", "main", "2026-11-25T08:00:00Z", "user", "hi")]


async def test_the_extractor_only_looks_for_study_when_work_is_off(monkeypatch):
    sent: dict = {}

    async def fake_run(prompt, **kw):
        sent.update(prompt=prompt, **kw)
        # The model ignores the brief: the candidates are dropped anyway.
        return ClaudeResult(
            text="",
            structured={"candidates": [CANDIDATE.model_dump()]},
            cost_usd=0.0,
        )

    monkeypatch.setattr(insight_extractor, "run_claude", fake_run)
    found = await insight_extractor.extract_insights(
        "claude-code:x", _messages(), feedback="- [gotcha] x", work=False
    )
    assert found.candidates == []
    assert sent["system_prompt"] == insight_extractor.system_prompt(work=False)
    assert "INTERVIEW STUDY" in sent["system_prompt"]
    assert "durable engineering insights" not in sent["system_prompt"]
    assert "READER FEEDBACK" not in sent["prompt"]
    assert insight_extractor.prompt_chars(
        _messages(), work=False
    ) < insight_extractor.prompt_chars(_messages())


async def test_the_cli_extractor_skips_ratings_when_work_is_off(tmp_path, monkeypatch):
    def no_ratings(db):
        raise AssertionError("ratings steer insights; not read when they're off")

    seen = []

    async def fake_extract(model, messages, *, feedback, project, study, work):
        seen.append((feedback, work))
        return InsightExtraction()

    monkeypatch.setattr(cli, "extraction_feedback", no_ratings)
    monkeypatch.setattr(cli, "extract_insights", fake_extract)
    settings = Settings(db_path=str(tmp_path / "x.db"), database_url="")
    await cli._extractor(settings, [])(_messages())
    assert seen == [("", False)]


def _sweep(monkeypatch, tmp_path, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("DAILY_AGENT_INSIGHTS_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DAILY_AGENT_MODEL", "test")
    monkeypatch.delenv("DAILY_AGENT_WORK_INSIGHTS", raising=False)
    cli.get_settings.cache_clear()
    try:
        return CliRunner().invoke(cli.app, ["insights", "sweep", *args])
    finally:
        cli.get_settings.cache_clear()


def test_the_sweep_by_default_keeps_study_cards_and_draws_nothing_for_work(
    monkeypatch, tmp_path
):
    _transcript(tmp_path, "Fix the pager", "insight: by hand")
    store = InsightStore(tmp_path / "cli.db")
    store.add(_item("insight:old-no-cover", type="gotcha", minutes=1))

    async def fake_extract(model, messages, **kw):
        assert kw["work"] is False
        return InsightExtraction(candidates=[CANDIDATE])

    async def never(*_args, **_kwargs):
        raise AssertionError("no covers or slides for hidden work insights")

    monkeypatch.setattr(cli, "extract_insights", fake_extract)
    monkeypatch.setattr(cli, "draw_cover", never)
    monkeypatch.setattr(cli, "write_slides", never)
    result = _sweep(monkeypatch, tmp_path, "--max-titles", "0")
    assert result.exit_code == 0, result.output
    assert "captured 0 new (0 marker, 0 extracted)" in result.output
    assert "Covers" not in result.output
    assert [i.key for i in store.all()] == ["insight:old-no-cover"]


async def test_slides_skip_work_insights_when_they_are_off(tmp_path):
    db = str(tmp_path / "s.db")
    store = InsightStore(db)
    work = next(f"insight:w{n}" for n in range(200) if picked(f"insight:w{n}"))
    video = next(f"video:v{n}" for n in range(200) if picked(f"video:v{n}"))
    long = " ".join(["A sentence long enough to tell as slides."] * 8)
    for key, type in ((work, "gotcha"), (video, "video")):
        store.add(Insight(key=key, text=long, type=type, captured_at=T0))
    written = []

    async def write(record, feedback):
        written.append(record["key"])
        raise RuntimeError("stop here")

    await generate_slides(store, SlideStore(db), write, work=False)
    assert written == [video]
