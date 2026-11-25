"""Slide stories (issue #158): selection, validation, storage, generation,
the writer's prompt, the sweep, and how cards and the route show them. Offline."""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.agents import slide_writer
from daily_agent.agents.claude_code import ClaudeCodeError, ClaudeResult
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.slides import (
    SlideStore,
    generate_slides,
    picked,
    slide_mode,
    validate_slides,
    wants_slides,
)
from daily_agent.models import Insight
from daily_agent.web.app import create_app

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
LONG = "A long enough lesson to be told as a short story in slides. " * 6


def svg(fill: str = "#274690") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 1000">'
        f'<rect width="800" height="1000" fill="{fill}"/>'
        + '<rect x="1" y="1" width="2" height="2" fill="#123456"/>' * 12
        + "</svg>"
    )


def story(n: int = 3) -> list[dict]:
    return [{"caption": f"Slide number {i + 1}", "svg": svg()} for i in range(n)]


def picked_keys(prefix: str, count: int, *, want: bool = True) -> list[str]:
    keys, i = [], 0
    while len(keys) < count:
        key = f"{prefix}{i}"
        if picked(key) == want:
            keys.append(key)
        i += 1
    return keys


def insight(key: str, *, type: str = "gotcha", text: str = LONG, minutes: int = 0):
    return Insight(
        key=key,
        text=text,
        type=type,
        tags=["t"],
        captured_at=T0,
        stored_at=T0 + timedelta(minutes=minutes),
    )


# --- which items ------------------------------------------------------------- #


def test_about_one_in_four_items_is_picked_and_the_pick_is_stable():
    keys = [f"insight:lesson-{i}" for i in range(4000)]
    share = sum(map(picked, keys)) / len(keys)
    assert 0.22 < share < 0.28
    assert [picked(k) for k in keys[:50]] == [picked(k) for k in keys[:50]]


@pytest.mark.parametrize(
    ("key", "type", "mode"),
    [
        ("insight:x", "gotcha", "story"),
        ("video:dQw4w9WgXcQ", "video", "story"),
        ("prep:notes:x", "concept", "story"),
        ("prep:yt:x", "design", "practice"),  # set up, never answered
        ("prep:yt:x", "interview", None),  # flashcard: answer stays hidden
        ("prep:dsa:x", "dsa", None),
        ("prep:dsa:x", "drill", None),
        ("ig:Cpost000001", "instagram", None),  # has its own slides (#157)
    ],
)
def test_slide_mode_by_kind(key, type, mode):
    assert slide_mode(insight(key, type=type)) == mode


def test_short_items_never_get_slides():
    key = picked_keys("insight:s", 1)[0]
    assert wants_slides(insight(key))
    assert not wants_slides(insight(key, text="Too short to tell."))
    assert not wants_slides(insight(picked_keys("insight:s", 1, want=False)[0]))


# --- validation and storage -------------------------------------------------- #


def test_valid_slides_pass():
    assert validate_slides(story(3)) == [] and validate_slides(story(5)) == []


@pytest.mark.parametrize(
    ("slides", "problem"),
    [
        (story(2), "need 3-5 slides, got 2"),
        (story(6), "need 3-5 slides, got 6"),
        ("nope", "need 3-5 slides, got 0"),
        ([*story(2), {"caption": "", "svg": svg()}], "slide 3: empty caption"),
        ([*story(2), {"caption": "x" * 81, "svg": svg()}], "over 80 characters"),
        ([*story(2), {"caption": "ok"}], "slide 3: no svg"),
        (
            [
                *story(2),
                {"caption": "ok", "svg": svg().replace("0 0 800 1000", "0 0 800 400")},
            ],
            'slide 3: viewBox must be "0 0 800 1000"',
        ),
        (
            [
                *story(2),
                {"caption": "ok", "svg": svg().replace("</svg>", "<script/></svg>")},
            ],
            "slide 3: element <script> is not allowed",
        ),
    ],
)
def test_invalid_slides_are_refused_with_reasons(slides, problem):
    assert any(problem in reason for reason in validate_slides(slides))


def test_store_keeps_clean_captions_and_replaces(tmp_path):
    store = SlideStore(tmp_path / "s.db")
    raw = story(3)
    raw[0]["caption"] = '  "**Bold** claim"  '
    assert store.put("insight:a", raw, source="model:x") == []
    got = store.get("insight:a")
    assert [s.caption for s in got] == [
        "Bold claim",
        "Slide number 2",
        "Slide number 3",
    ]
    version, captions = store.summary("insight:a")
    assert captions[0] == "Bold claim" and version
    assert store.put("insight:a", story(4)) == []
    assert len(store.get("insight:a")) == 4
    assert store.put("insight:b", story(1)) and store.get("insight:b") == []
    assert store.keys() == {"insight:a"} and store.summary("nope") is None


# --- generation -------------------------------------------------------------- #


def test_generation_writes_picked_items_newest_first_with_retries(tmp_path):
    db = tmp_path / "g.db"
    store, slides = InsightStore(db), SlideStore(db)
    old, new, flaky, broken = picked_keys("insight:g", 4)
    skipped = picked_keys("insight:g", 1, want=False)[0]
    for minutes, key in enumerate([old, new, flaky, broken, skipped]):
        store.add(insight(key, minutes=minutes))
    store.add(insight(picked_keys("prep:card", 1)[0], type="interview"))
    calls: list[tuple[str, list | None]] = []

    async def write(record, feedback):
        calls.append((record["key"], feedback))
        assert record["mode"] == "story" and record["text"] == LONG
        if record["key"] == broken:
            raise ClaudeCodeError("claude timed out after 480s")
        if record["key"] == flaky and feedback is None:
            return story(2)
        return story(3)

    run = asyncio.run(generate_slides(store, slides, write, source="model:t", limit=3))
    # newest three: broken (3), flaky (2), new (1); old waits for the next run
    assert {key for key, _ in calls} == {broken, flaky, new}
    assert (flaky, ["need 3-5 slides, got 2"]) in calls
    assert run.written == 2 and list(run.failed) == [broken]
    assert slides.keys() == {flaky, new}

    asyncio.run(generate_slides(store, slides, write, limit=10))
    assert slides.keys() == {flaky, new, old}  # never the unpicked or the flashcard


# --- the writer -------------------------------------------------------------- #


def test_writer_prompt_and_call(monkeypatch):
    seen = {}

    async def fake_run(prompt, *, model, system_prompt, json_schema, timeout):
        seen.update(prompt=prompt, model=model, schema=json_schema)
        return ClaudeResult(text="", structured={"slides": story(3)}, cost_usd=0.0)

    monkeypatch.setattr(slide_writer, "run_claude", fake_run)
    record = {
        "title": "T",
        "text": "Body",
        "type": "design",
        "tags": ["a"],
        "mode": "practice",
    }
    got = asyncio.run(
        slide_writer.write_slides("claude-code:opus", record, ["slide 2: no svg"])
    )
    assert got == story(3)
    assert "Mode: practice" in seen["prompt"] and "- slide 2: no svg" in seen["prompt"]
    assert seen["schema"]["properties"]["slides"]["maxItems"] == 5
    with pytest.raises(ClaudeCodeError):
        asyncio.run(slide_writer.write_slides("google:gemini", record))


# --- the web ------------------------------------------------------------------ #


@pytest.fixture
def feed(tmp_path):
    db = str(tmp_path / "web.db")
    store = InsightStore(db)
    store.add(insight("insight:with-slides", minutes=2))
    store.add(insight("insight:plain", minutes=1))
    raw = story(3)
    raw[0]["caption"] = "Retries & <timeouts> multiply"
    SlideStore(db).put("insight:with-slides", raw)
    return db, TestClient(create_app(db))


def test_a_slide_story_card_shows_the_strip_with_captions(feed):
    _db, client = feed
    page = client.get("/?view=latest").text
    cards = re.findall(r'<article class="story.*?</article>', page, re.S)
    by_key = {re.search(r'data-key="([^"]+)"', c).group(1): c for c in cards}
    card = by_key["insight:with-slides"]
    assert 'class="cover has-art is-slides fit-cover"' in card
    assert card.count('class="slide-caption"') == 3
    assert "Retries &amp; &lt;timeouts&gt; multiply" in card  # escaped
    srcs = [html.unescape(s) for s in re.findall(r'<img src="(/slides/[^"]+)"', card)]
    assert [s.split("?")[0] for s in srcs] == [
        f"/slides/{n}/insight%3Awith-slides" for n in range(3)
    ]
    assert "data-slide-count>1/3<" in card
    assert "is-slides" not in by_key["insight:plain"]


def test_slides_are_served_locked_down(feed):
    _db, client = feed
    page = client.get("/?view=latest").text
    src = html.unescape(re.search(r'<img src="(/slides/1/[^"]+)"', page).group(1))
    reply = client.get(src)
    assert reply.status_code == 200 and reply.text == svg()
    assert reply.headers["content-type"].startswith("image/svg+xml")
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert reply.headers["content-security-policy"].startswith("default-src 'none'")
    assert client.get("/slides/3/insight%3Awith-slides").status_code == 404
    assert client.get("/slides/0/insight%3Aplain").status_code == 404


def test_a_design_card_with_slides_shows_them_and_one_without_stays_text(tmp_path):
    db = str(tmp_path / "d.db")
    store = InsightStore(db)
    store.add(insight("prep:design-daily:caching", type="design", minutes=1))
    store.add(insight("prep:design-daily:queues", type="design"))
    SlideStore(db).put("prep:design-daily:caching", story(5))
    page = TestClient(create_app(db)).get("/?view=interview").text
    cards = re.findall(r'<article class="story.*?</article>', page, re.S)
    by_key = {re.search(r'data-key="([^"]+)"', c).group(1): c for c in cards}
    assert by_key["prep:design-daily:caching"].count('class="slide-caption"') == 5
    assert 'class="cover' not in by_key["prep:design-daily:queues"]


def test_a_youtube_fed_source_keeps_a_single_mark_on_the_slide_badge(
    tmp_path, monkeypatch
):
    from daily_agent.feed import web_feed

    db = str(tmp_path / "b.db")
    card = insight("prep:yt-chan:vid-1", type="design")
    card.source_session = "yt-chan"
    InsightStore(db).add(card)
    SlideStore(db).put("prep:yt-chan:vid-1", story(3))
    monkeypatch.setattr(
        web_feed.WebFeed, "_sources", lambda self: {"yt-chan": "▶ TechPrep"}
    )
    page = TestClient(create_app(db)).get("/?view=interview").text
    badge = re.search(r'<span class="cover-badge">([^<]*)</span>', page).group(1)
    assert badge == "▶ TechPrep"


def test_the_reader_shows_the_slides_too(feed):
    _db, client = feed
    reader = client.get("/i/insight:with-slides").text
    assert reader.count('class="slide-caption"') == 3


# --- the sweep ---------------------------------------------------------------- #


def _sweep(monkeypatch, tmp_path, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "cli.db"))
    (tmp_path / "projects").mkdir(exist_ok=True)
    monkeypatch.setenv("DAILY_AGENT_INSIGHTS_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DAILY_AGENT_SLIDE_MODEL", "claude-code:s")
    cli.get_settings.cache_clear()
    try:
        return CliRunner().invoke(
            cli.app,
            ["insights", "sweep", "--max-titles", "0", "--max-covers", "0", *args],
        )
    finally:
        cli.get_settings.cache_clear()


def test_sweep_writes_a_few_slide_stories(monkeypatch, tmp_path):
    keys = picked_keys("insight:w", 3)
    store = InsightStore(tmp_path / "cli.db")
    for minutes, key in enumerate(keys):
        store.add(insight(key, minutes=minutes))
    models: list[str] = []

    async def fake_write(model, record, feedback=None):
        models.append(model)
        return story(3)

    monkeypatch.setattr(cli, "write_slides", fake_write)
    result = _sweep(monkeypatch, tmp_path, "--max-slides", "2")
    assert result.exit_code == 0, result.output
    assert "Slide stories: wrote 2" in result.output
    assert models == ["claude-code:s"] * 2
    assert SlideStore(tmp_path / "cli.db").keys() == set(keys[1:])


def test_sweep_with_max_slides_zero_never_writes(monkeypatch, tmp_path):
    InsightStore(tmp_path / "cli.db").add(insight(picked_keys("insight:z", 1)[0]))

    async def never(*_args, **_kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(cli, "write_slides", never)
    result = _sweep(monkeypatch, tmp_path, "--max-slides", "0")
    assert result.exit_code == 0, result.output
    assert "Slide stories" not in result.output
