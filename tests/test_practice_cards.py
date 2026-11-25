"""Practice cards from videos and study sessions (issue #139). Offline: Claude
calls are faked, YouTube is a stub, and every transcript is synthetic."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.agents import insight_extractor, video_writer
from daily_agent.agents.claude_code import ClaudeCodeError, ClaudeResult
from daily_agent.config import Settings
from daily_agent.db import make_engine, session_scope
from daily_agent.feed.insights_capture import collect_file
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.insights_sweep import sweep_insights
from daily_agent.feed.practice_cards import (
    MAX_VIDEO_CARDS,
    STUDY_LABEL,
    PracticeCards,
    VideoCardRow,
    card_text,
    card_type,
    clean_cards,
    in_study_folder,
    is_study_session,
    study_card_key,
    video_card_prefix,
)
from daily_agent.feed.prep_tracks import DESIGN, DSA, PrepItems, split_steps
from daily_agent.feed.transcripts import TranscriptMessage, parse_line
from daily_agent.feed.videos import VideoSync, cards_from_videos, video_key
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import (
    InsightCandidate,
    InsightExtraction,
    PrepCard,
    VideoStory,
)
from daily_agent.sources.youtube import ChannelFeed, Transcript, Video, channel_ref

NOW = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
CHANNEL = "UCHelloInterview000000a"  # synthetic channel id

RECALL = {
    "track": "design",
    "question": "Why put a cache in front of a read-heavy database, and what "
    "does it cost you?",
    "answer": "It serves hot reads from memory and sheds load from the "
    "database; the cost is staleness and invalidation work.",
    "why_it_matters": "Caching comes up in almost every design round.",
    "tags": ["Caching", "system-design", "#consistency"],
}
DRILL = {
    "track": "dsa",
    "question": "Longest substring without repeating characters: what keeps "
    "the window valid as it slides?",
    "answer": "Move the left edge past the last index of the repeated char, "
    "so the window never holds a duplicate.",
    "why_it_matters": "",
    "tags": ["Sliding Window", "hash-map", "strings", "dsa"],
}
WALKTHROUGH = {
    "track": "design",
    "question": "Design an event scheduler that runs millions of one-off jobs "
    "at their due time.",
    "answer": "",
    "why_it_matters": "",
    "steps": [
        {"title": "Requirements", "body": "Schedule, cancel, run at least once."},
        {"title": "Core entities", "body": "Job, Execution."},
        {"title": "High-level design", "body": "Time-bucketed queue + workers."},
    ],
    "tags": ["scheduling", "queues"],
}


# --- cleaning and rendering ------------------------------------------------ #


def test_cards_are_cleaned_to_canonical_tags_and_tracks():
    raw = [
        RECALL,
        DRILL,
        {
            **DRILL,
            "question": "  longest SUBSTRING without repeating characters: "
            "what keeps the window valid as it slides?",
        },
        {
            **RECALL,
            "track": "System Design",
            "question": "How does consistent "
            "hashing limit remapping when a node joins?",
        },
        {**RECALL, "track": "frontend"},  # unknown track
        {**RECALL, "question": "Why?"},  # too short
        {**RECALL, "answer": ""},  # nothing to reveal
        "junk",
    ]
    cards = clean_cards(raw, limit=10)
    assert [c.track for c in cards] == [DESIGN, DSA, DESIGN]
    assert cards[0].tags == ["caching", "consistency"]  # track names dropped
    assert cards[1].tags == ["sliding-window", "arrays-hashing"]  # patterns only
    assert clean_cards(raw, limit=1) == cards[:1]
    assert clean_cards(None, limit=5) == [] and clean_cards({"a": 1}, limit=5) == []


def test_a_recall_card_reveals_its_answer_then_why_it_matters():
    card = clean_cards([RECALL], limit=1)[0]
    assert card_type(card) == "design"
    prompt, steps = split_steps(card_text(card))
    assert prompt == RECALL["question"]
    assert [s.title for s in steps] == ["Answer", "Why it matters"]
    assert steps[0].body == RECALL["answer"]

    drill = clean_cards([DRILL], limit=1)[0]
    assert card_type(drill) == "drill"
    assert [s.title for s in split_steps(card_text(drill))[1]] == ["Answer"]


def test_a_design_walkthrough_reveals_its_own_steps():
    card = clean_cards([WALKTHROUGH], limit=1)[0]
    _, steps = split_steps(card_text(card))
    assert [s.title for s in steps] == [
        "Requirements",
        "Core entities",
        "High-level design",
    ]
    # A walkthrough needs two steps; DSA cards never get steps.
    assert (
        clean_cards([{**WALKTHROUGH, "steps": WALKTHROUGH["steps"][:1]}], limit=1) == []
    )
    dsa = clean_cards([{**DRILL, "steps": WALKTHROUGH["steps"]}], limit=1)[0]
    assert dsa.steps == [] and dsa.answer


def test_a_stray_heading_in_an_answer_cannot_start_a_step():
    card = clean_cards(
        [{**DRILL, "answer": "Use a set.\n### Not a step\n```py\n### code\n```"}],
        limit=1,
    )[0]
    _, steps = split_steps(card_text(card))
    assert [s.title for s in steps] == ["Answer"]
    assert "**Not a step**" in steps[0].body and "### code" in steps[0].body


# --- videos ---------------------------------------------------------------- #


@pytest.fixture
def claude(monkeypatch):
    calls: list[dict] = []
    reply: dict = {}

    async def fake_run_claude(prompt, *, model, system_prompt, json_schema=None, **_):
        calls.append({"prompt": prompt, "system": system_prompt, "schema": json_schema})
        return ClaudeResult(text="", structured=reply.get("value"), cost_usd=0.01)

    monkeypatch.setattr(video_writer, "run_claude", fake_run_claude)
    return calls, reply


VIDEO = Video(
    video_id="hiVid0000_A",
    title="Caching in system design interviews",
    published_at=NOW - timedelta(days=2),
    description="A synthetic description.",
    channel_id=CHANNEL,
    channel="Hello Interview",
)


async def test_the_story_call_also_returns_interview_cards(claude):
    calls, reply = claude
    reply["value"] = {
        "headline": "Caches trade freshness for read throughput",
        "article": "The video explains where a cache helps. " * 5,
        "tags": ["caching"],
        "interview_cards": [RECALL, DRILL, {**RECALL, "track": "?"}],
    }
    story = await video_writer.write_story("claude-code:x", VIDEO, Transcript("t"))
    assert [c.track for c in story.cards] == [DESIGN, DSA]
    [call] = calls
    cards_schema = call["schema"]["properties"]["interview_cards"]
    assert cards_schema["maxItems"] == MAX_VIDEO_CARDS
    assert cards_schema["items"]["properties"]["track"]["enum"] == [DSA, DESIGN]
    assert "steps" not in cards_schema["items"]["properties"]
    assert "interview_cards" in call["system"] and "sliding-window" in call["system"]
    assert call["system"].rstrip().endswith("description, or transcript.")


async def test_cards_only_call_reads_the_stored_story(claude):
    calls, reply = claude
    reply["value"] = {"interview_cards": [RECALL]}
    cards = await video_writer.write_cards(
        "claude-code:x", VIDEO, "Caches trade freshness", "The stored article."
    )
    assert [c.question for c in cards] == [RECALL["question"]]
    payload = json.loads(calls[0]["prompt"])
    assert payload["article"] == "The stored article."
    assert payload["article_headline"] == "Caches trade freshness"
    assert "transcript" not in payload
    assert calls[0]["schema"]["required"] == ["interview_cards"]
    # The cards-only prompt is about the stored article, not captions.
    assert "transcript, too" not in calls[0]["system"]
    assert calls[0]["system"].rstrip().endswith("description, or article.")

    reply["value"] = {"cards": []}  # a broken reply is an error, not "no cards"
    with pytest.raises(ClaudeCodeError):
        await video_writer.write_cards("claude-code:x", VIDEO, "h", "a")
    with pytest.raises(ValueError):
        await video_writer.write_cards("google:gemini", VIDEO, "h", "a")


class StubYouTube:
    def __init__(self, videos: list[Video], channel_id: str = CHANNEL):
        self.videos = videos
        self.channel_id = channel_id

    async def resolve(self, link):
        return channel_ref(self.channel_id)

    async def feed(self, feed_url):
        return ChannelFeed(title=self.videos[0].channel, videos=self.videos)

    async def transcript(self, video_id):
        return Transcript(text=f"captions of {video_id}", language="en")


def _upload(n: int, *, channel_id: str = CHANNEL, channel="Hello Interview") -> Video:
    return Video(
        video_id=f"hiVid{n:06d}",
        title=f"Interview video {n}",
        published_at=NOW - timedelta(days=n),
        description="",
        channel_id=channel_id,
        channel=channel,
    )


def _sync(db: str, videos: list[Video], cards_for: dict[str, list[dict]]):
    """A sync whose writer gives the videos in ``cards_for`` those cards."""

    async def writer(video: Video, transcript) -> VideoStory:
        return VideoStory(
            headline=f"Story of {video.title}",
            text=f"What {video.title} teaches.",
            cards=clean_cards(cards_for.get(video.video_id, []), limit=5),
        )

    return VideoSync(
        db,
        StubYouTube(videos, videos[0].channel_id),
        writer,
        backfill=False,
        tz=timezone.utc,
        clock=lambda: NOW,
    )


async def test_a_video_story_brings_its_cards_into_the_interview_tab(tmp_path):
    db = str(tmp_path / "cards.db")
    interview, news = _upload(1), _upload(2)
    sync = _sync(db, [interview, news], {interview.video_id: [RECALL, DRILL]})
    await sync.follow("@HelloInterview")
    report = await sync.run_once()
    assert report.written == 2

    prefix = video_card_prefix(CHANNEL, interview.video_id)
    store = InsightStore(db)
    design, drill = store.get(prefix + "1"), store.get(prefix + "2")
    assert (design.type, drill.type) == ("design", "drill")
    assert design.status == "web-only" and design.title == RECALL["question"]
    assert design.captured_at == interview.published_at
    assert design.source_session == "prep:yt-uchellointerview000000a"
    assert [s.title for s in split_steps(design.text)[1]] == [
        "Answer",
        "Why it matters",
    ]
    assert drill.tags == ["sliding-window", "arrays-hashing"]
    # The video without interview content brought none.
    assert not [
        i
        for i in store.all()
        if i.key.startswith(f"prep:yt-{CHANNEL.lower()}:{news.video_id}")
    ]
    assert PracticeCards(db).done_videos() == {interview.video_id, news.video_id}

    feed = WebFeed(db)
    entries = feed.edition_page(feed.new_edition(interview=True, now=NOW)).entries
    assert {(e.source, e.track) for e in entries} == {
        ("▶ Hello Interview", DESIGN),
        ("▶ Hello Interview", DSA),
    }
    # The cards stay out of For you.
    for_you = feed.edition_page(feed.new_edition(now=NOW)).entries
    assert not {e.type for e in for_you} & {"design", "drill"}


def test_writing_a_video_again_never_duplicates_its_cards(tmp_path):
    db = str(tmp_path / "cards.db")
    cards = PracticeCards(db)
    first = cards.put_video(
        "abcDEF_1234", CHANNEL, "Hello Interview", clean_cards([RECALL, DRILL], limit=5)
    )
    assert (first.added, first.removed) == (2, 0)
    # A video id differing only in case keeps its own cards.
    cards.put_video(
        "ABCdef_1234", CHANNEL, "Hello Interview", clean_cards([DRILL], limit=5)
    )

    again = cards.put_video(
        "abcDEF_1234", CHANNEL, "Hello Interview", clean_cards([RECALL], limit=5)
    )
    assert (again.added, again.updated, again.removed) == (0, 0, 1)
    keys = sorted(i.key for i in InsightStore(db).all())
    assert keys == [
        video_card_prefix(CHANNEL, "ABCdef_1234") + "1",
        video_card_prefix(CHANNEL, "abcDEF_1234") + "1",
    ]
    assert PrepItems(db).labels() == {
        "prep:yt-uchellointerview000000a": "▶ Hello Interview"
    }


def test_a_video_without_cards_adds_no_source(tmp_path):
    db = str(tmp_path / "cards.db")
    report = PracticeCards(db).put_video("newsVid0001", "UCnews", "Tech News", [])
    assert (report.added, report.removed) == (0, 0)
    assert PrepItems(db).sources() == []
    assert PracticeCards(db).done_videos() == {"newsVid0001"}


# --- the backfill ---------------------------------------------------------- #


async def _stories(db: str, videos: list[Video]) -> None:
    """Video stories as written before cards existed."""
    sync = _sync(db, videos, {})
    await sync.follow("@channel")
    await sync.run_once(limit=len(videos))
    _forget_cards(db)


def _forget_cards(db: str) -> None:
    with session_scope(make_engine(db)) as session:
        session.execute(delete(VideoCardRow))


async def test_the_backfill_writes_cards_from_stored_stories_and_skips_done(tmp_path):
    db = str(tmp_path / "cards.db")
    videos = [_upload(n) for n in range(1, 5)]
    await _stories(db, videos)
    PracticeCards(db).put_video(videos[0].video_id, CHANNEL, "Hello Interview", [])
    seen: list[tuple[str, str, str]] = []

    async def write(video, headline, text):
        seen.append((video.video_id, headline, text))
        return clean_cards(
            [RECALL] if video.video_id != videos[2].video_id else [], limit=5
        )

    report = await cards_from_videos(db, write, limit=2, clock=lambda: NOW)
    # Newest first, the done one skipped, the stored story passed along.
    assert [v for v, _, _ in seen] == [videos[1].video_id, videos[2].video_id]
    assert seen[0][1:] == (
        "Story of Interview video 2",
        "What Interview video 2 teaches.",
    )
    assert (report.videos, report.cards, report.remaining) == (2, 1, 1)

    report = await cards_from_videos(db, write, clock=lambda: NOW)
    assert [v for v, _, _ in seen][2:] == [videos[3].video_id]
    assert (report.videos, report.remaining) == (1, 0)
    assert (await cards_from_videos(db, write)).videos == 0
    assert len(PracticeCards(db).done_videos()) == 4


async def test_the_backfill_filters_channels_and_keeps_failed_videos_to_do(tmp_path):
    db = str(tmp_path / "cards.db")
    await _stories(db, [_upload(1), _upload(2)])
    await _stories(db, [_upload(3, channel_id="UCother", channel="Fireship")])
    tried: list[str] = []

    async def failing(video, headline, text):
        tried.append(video.video_id)
        raise RuntimeError("status_code: 503\ntrace")

    report = await cards_from_videos(
        db, failing, channels=["hello"], max_consecutive_failures=5
    )
    assert tried == [_upload(1).video_id, _upload(2).video_id]  # not Fireship
    assert report.videos == 0 and report.remaining == 2
    assert report.errors[0] == "Interview video 1: status_code: 503"
    assert PracticeCards(db).done_videos() == set()

    tried.clear()
    report = await cards_from_videos(
        db, failing, channels=["UCother"], max_consecutive_failures=1
    )
    assert tried == [_upload(3).video_id] and not report.stopped_early
    report = await cards_from_videos(db, failing, max_consecutive_failures=1)
    assert report.stopped_early and len(report.errors) == 1


# --- study sessions ------------------------------------------------------- #


def test_study_folders_match_whole_names_or_paths():
    folders = ["interview-prep", "~/notes/sd"]
    home = os.path.expanduser("~")
    assert in_study_folder("/Users/me/interview-prep", folders)
    assert in_study_folder("/Users/me/Interview-Prep/dsa/", folders)
    assert in_study_folder(f"{home}/notes/sd/chat", folders)
    assert not in_study_folder("/Users/me/interview-prep-tools", folders)
    assert not in_study_folder(f"{home}/notes/sdk", folders)
    assert not in_study_folder("", folders)

    def msg(cwd: str) -> TranscriptMessage:
        return TranscriptMessage("s", "", "", "user", "hi", cwd=cwd)

    assert is_study_session(
        [msg("/x/daily-agent"), msg("/x/interview-prep")], "interview-prep"
    )
    assert not is_study_session([msg("/x/interview-prep")], " , ")


def test_transcript_messages_carry_the_session_cwd():
    line = json.dumps(
        {
            "type": "user",
            "sessionId": "s",
            "cwd": "/x/interview-prep",
            "message": {"content": "Let's practise"},
        }
    )
    assert parse_line(line).cwd == "/x/interview-prep"
    assert (
        parse_line(json.dumps({"type": "user", "message": {"content": "x"}})).cwd == ""
    )


def _messages(*texts: str, cwd: str = "/x/work") -> list[TranscriptMessage]:
    return [
        TranscriptMessage("s1", "main", "2026-11-25T08:00:00Z", "user", t, cwd=cwd)
        for t in texts
    ]


async def test_a_study_folder_tells_the_extractor_and_forces_study(monkeypatch):
    sent: dict = {}

    async def fake_run(prompt, **kw):
        sent.update(prompt=prompt, **kw)
        return ClaudeResult(text="", structured={"candidates": []}, cost_usd=0.0)

    monkeypatch.setattr(insight_extractor, "run_claude", fake_run)
    found = await insight_extractor.extract_insights(
        "claude-code:x", _messages("hi"), feedback="- [gotcha] x", study=True
    )
    assert found.study_session  # whatever the model said
    assert insight_extractor.STUDY_LINE in sent["prompt"]
    assert "- [gotcha] x" in sent["prompt"]  # ratings still reach the model
    cards = sent["json_schema"]["properties"]["prep_cards"]
    assert "steps" in cards["items"]["properties"]
    assert "INTERVIEW STUDY" in sent["system_prompt"]

    await insight_extractor.extract_insights("claude-code:x", _messages("hi"))
    assert insight_extractor.STUDY_LINE not in sent["prompt"]


def _transcript(
    tmp_path: Path, *texts: str, cwd: str, age: float = 3600, session: str = "s1"
) -> Path:
    path = tmp_path / "projects" / "-x" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "type": "user",
                "sessionId": session,
                "cwd": cwd,
                "timestamp": "2026-11-25T08:00:00Z",
                "message": {"content": t},
            }
        )
        for t in texts
    ]
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (time.time() - age, time.time() - age))
    return path


CANDIDATE = InsightCandidate(
    text="Consistent hashing remaps only about 1/n of keys.",
    canonical_key="consistent-hashing-remap",
    type="general",
    score=0.7,
)


async def test_a_study_batch_stores_cards_and_no_insights(tmp_path):
    path = _transcript(
        tmp_path, "Let's do a mock design round", "insight: marked by hand", cwd="/x"
    )
    store = InsightStore(tmp_path / "db.sqlite")

    async def extractor(messages):
        return InsightExtraction(
            candidates=[CANDIDATE],
            study_session=True,
            prep_cards=[PrepCard(**RECALL), PrepCard(**WALKTHROUGH)],
        )

    result = await collect_file(store, path, "insight:", extractor)
    assert (result.cards, result.extracted, result.marked) == (2, 0, 1)
    assert store.get("insight:consistent-hashing-remap") is None
    card = store.get(study_card_key(WALKTHROUGH["question"]))
    assert card.type == "design" and card.source_session == "prep:study"
    assert card.captured_at == datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc)
    assert PrepItems(store.db_path).labels() == {"prep:study": STUDY_LABEL}

    # The same question studied in another session is the same card.
    other = _transcript(tmp_path, "Again", cwd="/x", session="s2")
    again = await collect_file(store, other, "insight:", extractor)
    assert again.cards == 0
    assert len([i for i in store.all() if i.key.startswith("prep:study:")]) == 2


async def test_work_sessions_keep_their_insights(tmp_path):
    root = tmp_path / "projects"
    _transcript(tmp_path, "Fix the flaky test", cwd="/x/daily-agent")
    store = InsightStore(tmp_path / "db.sqlite")

    async def extractor(messages):
        # Cards on a work session are ignored: only study batches bring cards.
        return InsightExtraction(
            candidates=[CANDIDATE], prep_cards=[PrepCard(**RECALL)]
        )

    swept = await sweep_insights(store, root, "insight:", extractor, settle_seconds=600)
    assert (swept.captured.extracted, swept.captured.cards) == (1, 0)
    assert store.get("insight:consistent-hashing-remap") is not None
    assert PrepItems(store.db_path).sources() == []


async def test_the_cli_extractor_knows_study_folders(tmp_path, monkeypatch):
    seen: list[bool] = []

    async def fake_extract(model, messages, *, feedback, project, study):
        seen.append(study)
        return InsightExtraction()

    monkeypatch.setattr(cli, "extract_insights", fake_extract)
    settings = Settings(
        db_path=str(tmp_path / "x.db"), database_url="", prep_study_paths="prep, ~/sd"
    )
    extract = cli._extractor(settings, [])
    await extract(_messages("a", cwd="/Users/me/prep"))
    await extract(_messages("b", cwd="/Users/me/daily-agent"))
    await extract(_messages("c", cwd=os.path.expanduser("~/sd/event-scheduler")))
    assert seen == [True, False, True]
    assert Settings(_env_file=None).prep_study_paths == "interview-prep"


def _cli(monkeypatch, tmp_path, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("DAILY_AGENT_INSIGHTS_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DAILY_AGENT_MODEL", "test")
    monkeypatch.setenv("DAILY_AGENT_VIDEO_MODEL", "claude-code:v")
    # Spend is per process; other tests' fake calls mustn't show up here.
    monkeypatch.setattr(cli.SPEND, "calls", 0)
    monkeypatch.setattr(cli.SPEND, "cost_usd", 0.0)
    cli.get_settings.cache_clear()
    try:
        return CliRunner().invoke(cli.app, list(args))
    finally:
        cli.get_settings.cache_clear()


def test_the_sweep_reports_study_cards(monkeypatch, tmp_path):
    _transcript(
        tmp_path, "Mock interview: design a URL shortener", cwd="/x/interview-prep"
    )

    async def fake_extract(model, messages, *, feedback, project, study):
        assert study  # the transcript's cwd is in the default study folder
        return InsightExtraction(
            candidates=[CANDIDATE], study_session=study, prep_cards=[PrepCard(**RECALL)]
        )

    monkeypatch.setattr(cli, "extract_insights", fake_extract)
    result = _cli(
        monkeypatch,
        tmp_path,
        "insights",
        "sweep",
        "--max-titles",
        "0",
        "--max-covers",
        "0",
    )
    assert result.exit_code == 0, result.output
    assert "captured 0 new (0 marker, 0 extracted) and 1 study card(s)" in result.output
    assert "queued 0 for Telegram" in result.output


def test_the_backfill_command(monkeypatch, tmp_path):
    import asyncio

    db = str(tmp_path / "cli.db")
    asyncio.run(_stories(db, [_upload(1), _upload(2)]))
    models: list[str] = []

    async def fake_cards(model, video, headline, text):
        models.append(model)
        return clean_cards([RECALL], limit=5)

    monkeypatch.setattr(video_writer, "write_cards", fake_cards)
    # The feed server has no claude CLI: nothing is tried there.
    monkeypatch.setattr(cli, "claude_available", lambda: False)
    result = _cli(monkeypatch, tmp_path, "prep", "cards-from-videos")
    assert result.exit_code == 1 and "No claude CLI here" in result.output
    assert models == []

    monkeypatch.setattr(cli, "claude_available", lambda: True)
    result = _cli(monkeypatch, tmp_path, "prep", "cards-from-videos", "--limit", "1")
    assert result.exit_code == 0, result.output
    assert "Wrote cards for 1 video(s): 1 card(s) added; 1 video(s) still to do" in (
        result.output
    )
    result = _cli(
        monkeypatch, tmp_path, "prep", "cards-from-videos", "--channel", "nobody"
    )
    assert "Wrote cards for 0 video(s): 0 card(s) added." in result.output
    assert models == ["claude-code:v"]
    assert video_key(_upload(1).video_id) in {i.key for i in InsightStore(db).all()}
