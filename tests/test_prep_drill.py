"""The Telegram morning drill and evening answers (issue #136). Offline:
tmp_path SQLite (or Postgres schemas in CI), a fake clock, and Telegram
through ``httpx.MockTransport``."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.feed.channels import TelegramChannel, TelegramError
from daily_agent.feed.prep_drill import (
    EVENING,
    MAX_ATTEMPTS,
    MORNING,
    DrillCard,
    DrillLog,
    PrepDrill,
    answer_steps,
    card_link,
    drill_forever,
    flatten,
    hour_label,
)
from daily_agent.feed.prep_grades import GOT, MISSED, GradeStore
from daily_agent.feed.prep_progress import local_zone
from daily_agent.feed.prep_tracks import (
    DESIGN,
    DESIGN_TYPE,
    DRILL_TYPE,
    DSA,
    PROBLEM_TYPE,
    PrepItem,
    PrepItems,
    item_key,
)
from daily_agent.feed.web_feed import WebFeed

IST = local_zone("Asia/Kolkata")
FEED = "http://daily-agent.tail1234.ts.net"
DAY = date(2026, 11, 25)  # a Wednesday


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    """That local (IST) time, as the UTC clock reads it."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
    return local.astimezone(timezone.utc)


PROBLEM = """Purchase events stream in as `(sku, t)`. At any moment, return the
**k** SKUs bought most in the last 10 minutes.

```python
def top_k(events, k): ...
```

### Pattern
Sliding window + count map + heap.

### Key idea
Keep a count per SKU inside the window; a size-k min-heap gives the top k.

### Complexity
O(n log k).
"""

DESIGN_CARD = """Design a rate limiter for a public API: 100 requests a minute per
key, across 20 app servers.

### Requirements
Per-key limits, low latency, shared across servers.

### 30-second recap
Token bucket per key in **Redis**, refilled lazily; a Lua script makes
check-and-take atomic.
"""

P1 = item_key("dsa-pack", "p1")
P2 = item_key("dsa-pack", "p2")
D1 = item_key("design-daily", "rate-limiter")


def _problem(name: str, tags=("sliding-window", "heap"), text=PROBLEM, **kw):
    return PrepItem(
        item_key("dsa-pack", name),
        kw.pop("type", PROBLEM_TYPE),
        kw.pop("title", f"Top-K trending SKUs ({name})"),
        text,
        list(tags),
    )


def _seed(db, dsa=("p1",), design=True) -> None:
    items = PrepItems(db)
    t0 = at(7) - timedelta(days=3)
    items.put("dsa-pack", "Blinkit DSA pack", [_problem(n) for n in dsa], now=t0)
    if design:
        card = PrepItem(D1, DESIGN_TYPE, "Design a rate limiter", DESIGN_CARD,
                        ["rate-limiting", "caching"])  # fmt: skip
        items.put("design-daily", "Design walkthroughs", [card], now=t0)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _drill(db, clock, sent, **kw) -> PrepDrill:
    return PrepDrill(db, sent.append, zone=IST, feed_url=FEED, clock=clock, **kw)


def run(drill: PrepDrill):
    return asyncio.run(drill.run_once())


def test_a_drill_in_the_morning_and_its_answers_in_the_evening(tmp_path):
    db = tmp_path / "drill.db"
    _seed(db)
    clock, sent = Clock(at(9)), []
    drill = _drill(db, clock, sent)

    assert run(drill) == MORNING
    (morning,) = sent
    assert morning.startswith("Morning drill · Wed 25 Nov\n")
    assert "\nTwo cards to answer in your head; the answers come at 8 pm." in morning
    assert "DSA · Blinkit DSA pack\nTop-K trending SKUs (p1)\n" in morning
    assert "Purchase events stream in as (sku, t)." in morning  # no markdown
    assert "**" not in morning and "`" not in morning and "def top_k" in morning
    assert "Sliding window + count map" not in morning  # prompt only
    assert f"{FEED}/i/prep%3Adsa-pack%3Ap1" in morning
    assert "System design · Design walkthroughs\nDesign a rate limiter\n" in morning
    assert "Token bucket" not in morning
    assert f"{FEED}/i/prep%3Adesign-daily%3Arate-limiter" in morning

    assert run(drill) is None  # once a day
    clock.now = at(13)
    assert run(drill) is None and len(sent) == 1

    clock.now = at(20)
    assert run(drill) == EVENING
    evening = sent[1]
    assert evening.startswith("Answers · Wed 25 Nov\n\nDSA: Top-K trending SKUs (p1)\n")
    assert "Pattern: Sliding window + count map + heap.\n" in evening
    assert "Key idea: Keep a count per SKU inside the window;" in evening
    assert "Complexity" not in evening and "Purchase events" not in evening
    assert (
        "System design: Design a rate limiter\n30-second recap: Token bucket" in evening
    )
    assert "Redis" in evening and "**" not in evening and "Requirements" not in evening
    assert f"{FEED}/i/prep%3Adsa-pack%3Ap1" in evening
    assert evening.endswith("Grade them in the feed: Got it, Shaky or Missed.")
    clock.now = at(21, 30)
    assert run(drill) is None and len(sent) == 2


def test_the_mac_and_the_server_send_each_slot_once(tmp_path):
    db = tmp_path / "drill.db"
    _seed(db)
    clock, mac, server = Clock(at(9, 2)), [], []
    assert run(_drill(db, clock, mac)) == MORNING
    assert run(_drill(db, clock, server)) is None
    clock.now = at(20, 1)
    assert run(_drill(db, clock, server)) == EVENING
    assert run(_drill(db, clock, mac)) is None
    assert (len(mac), len(server)) == (1, 1)

    log = DrillLog(db)
    assert log.claim(DAY + timedelta(days=1), MORNING, [P1], now=at(9)) is True
    assert log.claim(DAY + timedelta(days=1), MORNING, [P2], now=at(9)) is False
    assert log.get(DAY + timedelta(days=1), MORNING).keys == P1


def test_a_machine_that_slept_catches_up_until_the_evening(tmp_path):
    db = tmp_path / "drill.db"
    _seed(db)
    clock, sent = Clock(at(8, 59)), []
    drill = _drill(db, clock, sent)
    assert run(drill) is None  # before the drill hour
    clock.now = at(19, 40)  # woke late: the drill still goes out
    assert run(drill) == MORNING
    clock.now = at(20)
    assert run(drill) == EVENING

    # Asleep all day until after the evening hour: neither goes out.
    other = tmp_path / "other.db"
    _seed(other)
    clock, sent = Clock(at(20, 30)), []
    drill = _drill(other, clock, sent)
    assert run(drill) is None and sent == []
    clock.now = at(9, 5, DAY + timedelta(days=1))
    assert run(drill) == MORNING and sent[0].startswith("Morning drill · Thu 26 Nov")


def test_quiet_hours_hold_both_slots(tmp_path):
    db = tmp_path / "drill.db"
    _seed(db)
    clock, sent = Clock(at(9)), []
    drill = _drill(db, clock, sent, quiet_start=19, quiet_end=10)
    assert run(drill) is None
    clock.now = at(10)
    assert run(drill) == MORNING
    clock.now = at(20)
    assert run(drill) is None
    assert drill.slot_at(at(23, 30).astimezone(IST)) is None


def test_the_day_and_hours_are_the_readers_not_the_machines(tmp_path):
    db = tmp_path / "drill.db"
    _seed(db)
    # 03:30 UTC is 09:00 in Asia/Kolkata.
    clock, sent = Clock(datetime(2026, 11, 25, 3, 30, tzinfo=timezone.utc)), []
    assert run(_drill(db, clock, sent)) == MORNING
    assert DrillLog(db).get(DAY, MORNING).sent_at is not None


def test_the_pick_prefers_due_or_unseen_cards_on_weak_tags(tmp_path):
    db = tmp_path / "drill.db"
    cards = [
        _problem("a", tags=["heap"]),
        _problem("b", tags=["dp", "must-do"]),
        _problem("c", tags=["dp"]),
        _problem("d", tags=["heap"]),
        _problem("e", tags=["dp"], type=DRILL_TYPE),
    ]
    PrepItems(db).put("dsa-pack", "Pack", cards, now=at(7) - timedelta(days=9))
    grades = GradeStore(db)
    grades.grade(item_key("dsa-pack", "a"), GOT, now=at(9) - timedelta(days=2))
    grades.grade(item_key("dsa-pack", "c"), MISSED, now=at(9) - timedelta(days=2))
    drill = _drill(db, Clock(at(9)), [])

    # a: seen, not due. c: missed, due again, and dp is weak. b: unseen, on
    # dp, and must-do. d: unseen but heap isn't weak. e: a quiz, not a problem.
    (pick,) = drill.pick(DAY, now=at(9))
    assert pick.key == item_key("dsa-pack", "b")
    WebFeed(db).set_hidden(pick.key, True)  # a hidden card is never drilled
    (pick,) = drill.pick(DAY, now=at(9))
    assert pick.key == item_key("dsa-pack", "c")


def test_never_the_same_card_two_days_running(tmp_path):
    db = tmp_path / "drill.db"
    _seed(db, dsa=("p1", "p2"))
    grades = GradeStore(db)
    grades.grade(P2, GOT, now=at(9) - timedelta(days=1))  # p2: seen, not due
    clock, sent = Clock(at(9)), []
    drill = _drill(db, clock, sent)
    assert run(drill) == MORNING and "(p1)" in sent[0]  # unseen first

    clock.now = at(9, 0, DAY + timedelta(days=1))
    assert run(drill) == MORNING
    assert "(p2)" in sent[1] and "(p1)" not in sent[1]
    # The only design card was drilled yesterday, so it sits today out.
    assert "Design a rate limiter" not in sent[1]
    assert "\nOne card to answer in your head;" in sent[1]

    clock.now = at(9, 0, DAY + timedelta(days=2))
    assert run(drill) == MORNING
    assert "(p1)" in sent[2] and "Design a rate limiter" in sent[2]


def test_nothing_to_drill_sends_nothing(tmp_path):
    db = tmp_path / "drill.db"
    PrepItems(db)
    clock, sent = Clock(at(9)), []
    drill = _drill(db, clock, sent)
    assert run(drill) is None and sent == []
    assert DrillLog(db).get(DAY, MORNING) is None
    clock.now = at(20)
    assert run(drill) is None


def test_a_failed_send_is_retried_then_given_up(tmp_path):
    db = tmp_path / "drill.db"
    _seed(db)
    clock, sent = Clock(at(9)), []
    failures = [TelegramError("Bad Gateway")]

    def flaky(text: str) -> None:
        if failures:
            raise failures.pop()
        sent.append(text)

    drill = PrepDrill(db, flaky, zone=IST, feed_url=FEED, clock=clock)
    assert run(drill) is None
    row = DrillLog(db).get(DAY, MORNING)
    assert (row.attempts, row.error, row.sent_at) == (1, "Bad Gateway", None)
    clock.now = at(9, 5)
    assert run(drill) is None  # not yet
    clock.now = at(9, 10)
    assert run(drill) == MORNING and "(p1)" in sent[0]
    assert DrillLog(db).get(DAY, MORNING).error == ""

    # Telegram down all morning: a few tries, then no drill and no answers.
    other = tmp_path / "other.db"
    _seed(other)

    def down(text: str) -> None:
        raise TelegramError("Bad Gateway")

    clock = Clock(at(9))
    drill = PrepDrill(other, down, zone=IST, clock=clock)
    for minutes in range(0, 60, 10):
        clock.now = at(9, minutes)
        assert run(drill) is None
    assert DrillLog(other).get(DAY, MORNING).attempts == MAX_ATTEMPTS
    clock.now = at(20)
    assert run(drill) is None
    assert DrillLog(other).get(DAY, EVENING) is None


def test_the_drill_goes_through_the_telegram_bot(tmp_path):
    db = tmp_path / "drill.db"
    _seed(db)
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((str(request.url), json.loads(request.content)))
        if len(posted) == 1:
            return httpx.Response(400, json={"ok": False, "description": "chat not found"})  # fmt: skip
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    bot = TelegramChannel(
        "123:ABC", "42", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    clock = Clock(at(9))
    drill = PrepDrill(db, bot.send_text, zone=IST, feed_url=FEED, clock=clock)
    assert run(drill) is None
    assert DrillLog(db).get(DAY, MORNING).error == "chat not found"
    clock.now = at(9, 15)
    assert run(drill) == MORNING
    url, body = posted[-1]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert body["chat_id"] == "42" and body["disable_web_page_preview"] is True
    assert body["text"].startswith("Morning drill")
    assert "parse_mode" not in body  # plain text: nothing for Telegram to mangle


def test_answer_steps_fall_back_sensibly():
    def card(type_, text):
        return DrillCard("prep:x:y", type_, "T", text)

    quiz = card(DRILL_TYPE, "Which pattern?\n\n### Answer\nMonotonic stack.")
    assert [s.title for s in answer_steps(quiz)] == ["Answer"]
    bare = card(PROBLEM_TYPE, "Q\n\n### Approach\nA\n\n### Code\nB")
    assert [s.title for s in answer_steps(bare)] == ["Approach"]
    design = card(DESIGN_TYPE, "Q\n\n### Requirements\nR\n\n### Data model\nD")
    assert [s.title for s in answer_steps(design)] == ["Requirements"]
    recap = card(DESIGN_TYPE, "Q\n\n### Scale\nS\n\n### 30-second Recap\nR")
    assert [s.title for s in answer_steps(recap)] == ["30-second Recap"]
    assert answer_steps(card(PROBLEM_TYPE, "No steps at all")) == []
    assert (
        DrillCard("k", PROBLEM_TYPE, "", "First line. More.").headline == "First line"
    )
    assert {DrillCard("k", PROBLEM_TYPE, "", "").track, DrillCard("k", DESIGN_TYPE, "", "").track} == {DSA, DESIGN}  # fmt: skip


def test_formatting_helpers():
    text = "**Bold** and `code`\n\n\n\n```py\nx = 1\n```\nend"
    assert flatten(text, 100) == "Bold and code\n\nx = 1\nend"
    assert flatten("one two three four", 12) == "one two…"
    assert [hour_label(h) for h in (0, 9, 12, 20)] == ["12 am", "9 am", "12 pm", "8 pm"]
    assert card_link("", P1) == ""
    assert card_link(FEED + "/", P1) == f"{FEED}/i/prep%3Adsa-pack%3Ap1"


async def test_the_loop_outlives_a_bad_pass():
    calls = []

    class Flaky:
        async def run_once(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database is locked")

    task = asyncio.create_task(drill_forever(Flaky(), every=0))
    for _ in range(100):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3


def test_the_web_server_runs_the_drill_loop(tmp_path):
    from daily_agent.web.app import create_app

    ran = threading.Event()

    class Fake:
        async def run_once(self):
            ran.set()

    with TestClient(create_app(tmp_path / "w.db", drill=Fake())):
        assert ran.wait(5)


@pytest.fixture
def web_kwargs(monkeypatch, tmp_path):
    """Run `daily-agent web` with create_app and uvicorn faked; return its kwargs."""
    seen: dict = {}

    def fake_create_app(db, **kwargs):
        seen.update(kwargs)
        return object()

    import uvicorn

    import daily_agent.web.app as web_app

    monkeypatch.setattr(web_app, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "w.db"))
    monkeypatch.setenv("DAILY_AGENT_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("DAILY_AGENT_TELEGRAM_CHAT_ID", "")

    def run() -> dict:
        seen.clear()
        cli.get_settings.cache_clear()
        try:
            result = CliRunner().invoke(cli.app, ["web"])
        finally:
            cli.get_settings.cache_clear()
        assert result.exit_code == 0, result.output
        return dict(seen)

    return run


def test_only_a_machine_with_telegram_runs_the_drill(web_kwargs, monkeypatch):
    kwargs = web_kwargs()
    assert kwargs["drill"] is None  # the feed server without the bot's secrets
    assert (kwargs["local_tz"], kwargs["interview_date"]) == ("Asia/Kolkata", "")

    monkeypatch.setenv("DAILY_AGENT_TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("DAILY_AGENT_TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("DAILY_AGENT_PREP_DRILL_HOUR", "7")
    monkeypatch.setenv("DAILY_AGENT_INTERVIEW_DATE", "2026-12-18")
    kwargs = web_kwargs()
    drill = kwargs["drill"]
    assert isinstance(drill, PrepDrill)
    assert (drill.drill_hour, drill.answer_hour, str(drill.zone)) == (
        7,
        20,
        "Asia/Kolkata",
    )
    assert kwargs["interview_date"] == "2026-12-18"

    monkeypatch.setenv("DAILY_AGENT_PREP_DRILL_TELEGRAM", "false")
    assert web_kwargs()["drill"] is None
