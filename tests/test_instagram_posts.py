"""Following Instagram creators and writing their stories (issue #105).
Offline: a fake Instagram (no browser) and a fake writer stand in."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from daily_agent.feed.insights_feed import enqueue_new_insights
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.instagram_posts import (
    BLOCKED_PAUSE,
    CHECK_EVERY,
    FIRST_LOOK,
    UNAVAILABLE_RETRY,
    InstagramSync,
    code_of,
    post_key,
)
from daily_agent.feed.outbox import Outbox
from daily_agent.feed.story_queue import MAX_ATTEMPTS
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import VideoStory
from daily_agent.sources.instagram import (
    AccountUnavailable,
    BadInstagramLink,
    InstagramBlocked,
    InstagramError,
    Post,
    Profile,
)

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


def code(n: int) -> str:
    return f"Post{n:07d}"  # 11 chars, a valid shortcode


def post(n: int, *, age: timedelta, username: str = "mkbhd") -> Post:
    return Post(
        code=code(n),
        username=username,
        author="Marques Brownlee" if username == "mkbhd" else username.title(),
        kind="reel",
        caption=f"Caption {n}",
        taken_at=NOW - age,
        image_description=f"May be an image of thing {n}",
        image_url=f"https://x.cdninstagram.com/{n}.jpg",
    )


class FakeBrowser:
    def __init__(self, ig: FakeInstagram):
        self.ig = ig

    async def profile(self, username: str) -> Profile:
        self.ig.profiles.append(username)
        error = self.ig.profile_errors.get(username)
        if error:
            raise error
        grid = self.ig.grids.get(username, [])
        return Profile(username=username, name=self.ig.names[username], codes=grid)

    async def post(self, shortcode: str) -> Post:
        self.ig.opened.append(shortcode)
        error = self.ig.post_errors.get(shortcode)
        if error:
            raise error
        return self.ig.posts[shortcode]

    async def image(self, url: str) -> tuple[bytes, str]:
        if "broken" in url:
            raise InstagramError("image answered 403")
        return b"jpeg:" + url.encode(), "image/jpeg"


class FakeInstagram:
    def __init__(self):
        self.names = {"mkbhd": "Marques Brownlee", "natgeo": "National Geographic"}
        self.grids: dict[str, list[str]] = {}
        self.posts: dict[str, Post] = {}
        self.profile_errors: dict[str, Exception] = {}
        self.post_errors: dict[str, Exception] = {}
        self.start_error: Exception | None = None
        self.profiles: list[str] = []
        self.opened: list[str] = []
        self.sessions = 0

    def add(self, username: str, *posts: Post, front: bool = False) -> None:
        for p in posts:
            self.posts[p.code] = p
        codes = [p.code for p in posts]
        grid = self.grids.setdefault(username, [])
        self.grids[username] = codes + grid if front else grid + codes

    @asynccontextmanager
    async def session(self):
        if self.start_error:
            raise self.start_error
        self.sessions += 1
        yield FakeBrowser(self)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def setup(tmp_path):
    db = str(tmp_path / "ig.db")
    ig = FakeInstagram()
    # A pinned post from last year, then 11 posts, newest first.
    ig.add("mkbhd", post(99, age=timedelta(days=400)))
    ig.add("mkbhd", *[post(n, age=timedelta(days=n)) for n in range(1, 12)])
    written: list[Post] = []
    failures: set[str] = set()

    async def writer(p: Post) -> VideoStory:
        if p.code in failures:
            raise RuntimeError("claude -p exited 1\nusage limit")
        written.append(p)
        return VideoStory(
            headline=f"Story of {p.caption}", text=f"What {p.caption} says.", tags=["t"]
        )

    clock = Clock()
    sync = InstagramSync(db, ig, writer, clock=clock)
    return sync, ig, written, failures, clock, db


async def test_follow_is_instant_and_browses_nothing(setup):
    sync, ig, _, _, _, _ = setup
    source = sync.follow("https://www.instagram.com/MKBHD/")
    assert source.id == "instagram:mkbhd" and source.username == "mkbhd"
    assert ig.sessions == 0
    [status] = sync.store.statuses()
    assert status.platform == "instagram" and status.title == "@mkbhd"
    assert status.url == "https://www.instagram.com/mkbhd/"
    assert status.checked_at is None
    with pytest.raises(BadInstagramLink):
        sync.follow("instagram.com/p/DfDZ1iAvvDy/")


async def test_first_check_queues_the_newest_five_and_skips_pinned_old_posts(setup):
    sync, ig, written, _, _, _ = setup
    sync.follow("instagram.com/mkbhd")
    report = await sync.run_once()
    # The first 8 grid posts are opened: the pinned one and posts 1-7.
    assert ig.opened == ig.grids["mkbhd"][:FIRST_LOOK]
    assert report.checked == 1 and report.queued == 5 and report.written == 5
    assert [p.code for p in written] == [code(n) for n in range(1, 6)]
    [status] = sync.store.statuses()
    assert status.title == "Marques Brownlee" and status.stories == 5
    assert status.checked_at == NOW and status.check_error == ""
    # Every grid post is now known, so none is ever opened again.
    assert sync.store.known_codes(ig.grids["mkbhd"]) == set(ig.grids["mkbhd"])
    assert sync.store.image(code(1)) == (
        b"jpeg:https://x.cdninstagram.com/1.jpg",
        "image/jpeg",
    )


async def test_stories_are_web_only_instagram_entries_from_the_creator(setup):
    sync, _, _, _, _, db = setup
    sync.follow("instagram.com/mkbhd")
    await sync.run_once()
    insight = InsightStore(db).get(post_key(code(1)))
    assert insight.type == "instagram" and insight.status == "web-only"
    assert insight.source_session == "ig:mkbhd"
    assert insight.captured_at == NOW - timedelta(days=1)
    assert enqueue_new_insights(InsightStore(db), Outbox(db)) == 0  # not Telegram
    entry = WebFeed(db).entry(post_key(code(1)))
    assert entry.source == "Marques Brownlee"
    assert entry.instagram_code == code(1) and entry.video_id == ""
    assert entry.thumbnail == f"/media/instagram/{code(1)}"
    assert entry.embed_url == f"https://www.instagram.com/p/{code(1)}/embed/"
    assert entry.instagram_url == f"https://www.instagram.com/p/{code(1)}/"
    assert ("instagram", 5) in WebFeed(db).types()


def test_story_keys():
    assert code_of(post_key("DfDZ1iAvvDy")) == "DfDZ1iAvvDy"
    assert code_of("instagram:../x") == ""
    assert code_of("video:DfDZ1iAvvDy") == ""


async def test_later_checks_open_only_new_posts(setup):
    sync, ig, written, _, clock, _ = setup
    sync.follow("instagram.com/mkbhd")
    await sync.run_once()
    ig.opened.clear()
    clock.now += timedelta(hours=1)
    assert (await sync.run_once()).checked == 0  # not due yet
    ig.add("mkbhd", post(0, age=-timedelta(hours=2)), front=True)  # a new post
    # ...and an old post the creator just pinned.
    ig.add("mkbhd", post(98, age=timedelta(days=300)), front=True)
    clock.now = NOW + CHECK_EVERY
    report = await sync.run_once()
    assert sorted(ig.opened) == sorted([code(0), code(98)])
    assert report.queued == 1 and written[-1].code == code(0)
    clock.now += CHECK_EVERY
    ig.opened.clear()
    await sync.run_once()
    assert ig.opened == []


async def test_a_login_wall_pauses_all_checks(setup):
    sync, ig, _, _, clock, _ = setup
    ig.add("natgeo", post(50, age=timedelta(days=1), username="natgeo"))
    sync.follow("instagram.com/mkbhd")
    sync.follow("instagram.com/natgeo")
    ig.profile_errors["mkbhd"] = InstagramBlocked("Instagram asked this Mac to log in")
    report = await sync.run_once()
    assert ig.profiles == ["mkbhd"]  # natgeo wasn't tried after the wall
    assert "paused for 3 hours" in report.errors[0]
    mkbhd = next(s for s in sync.store.statuses() if s.id == "instagram:mkbhd")
    assert "log in" in mkbhd.check_error
    del ig.profile_errors["mkbhd"]
    clock.now += timedelta(hours=1)
    await sync.run_once()
    assert ig.profiles == ["mkbhd"]  # still paused
    clock.now = NOW + BLOCKED_PAUSE
    report = await sync.run_once()
    assert ig.profiles == ["mkbhd", "mkbhd", "natgeo"] and report.checked == 2


async def test_unavailable_accounts_are_retried_a_day_later(setup):
    sync, ig, _, _, clock, _ = setup
    sync.follow("instagram.com/mkbhd")
    sync.follow("instagram.com/gone")
    ig.profile_errors["gone"] = AccountUnavailable("Instagram has no account @gone")
    report = await sync.run_once()
    assert report.checked == 1  # mkbhd still checked
    gone = next(s for s in sync.store.statuses() if s.id == "instagram:gone")
    assert gone.check_error == "Instagram has no account @gone"
    clock.now += UNAVAILABLE_RETRY - timedelta(minutes=1)
    await sync.run_once()
    assert ig.profiles.count("gone") == 1
    clock.now += timedelta(minutes=2)
    await sync.run_once()
    assert ig.profiles.count("gone") == 2


async def test_chrome_that_wont_start_is_reported_on_every_source(setup):
    sync, ig, _, _, _, _ = setup
    ig.start_error = InstagramError("couldn't start Chrome: install Google Chrome")
    sync.follow("instagram.com/mkbhd")
    report = await sync.run_once()
    assert report.checked == 0 and "couldn't start Chrome" in report.errors[0]
    assert "couldn't start Chrome" in sync.store.statuses()[0].check_error


async def test_a_post_that_fails_to_open_is_tried_on_the_next_check(setup):
    sync, ig, _, _, clock, _ = setup
    ig.post_errors[code(2)] = InstagramError("couldn't load the post")
    sync.follow("instagram.com/mkbhd")
    report = await sync.run_once()
    assert report.queued == 5 and code(2) not in sync.store.known_codes([code(2)])
    del ig.post_errors[code(2)]
    ig.opened.clear()
    clock.now += CHECK_EVERY
    await sync.run_once()
    # Opened again; posted before the follow, so it isn't new.
    assert ig.opened == [code(2)]
    assert sync.store.known_codes([code(2)]) == {code(2)}


async def test_a_missing_image_falls_back_without_failing(setup):
    sync, ig, _, _, _, _ = setup
    ig.posts[code(1)] = Post(
        **{**ig.posts[code(1)].__dict__, "image_url": "https://x.fbcdn.net/broken"}
    )
    sync.follow("instagram.com/mkbhd")
    report = await sync.run_once()
    assert report.written == 5 and sync.store.image(code(1)) is None


async def test_writer_failures_retry_then_give_up(setup):
    sync, _, written, failures, clock, _ = setup
    failures.add(code(1))
    sync.follow("instagram.com/mkbhd")
    report = await sync.run_once()
    assert report.written == 4 and "usage limit" not in report.errors[0]
    assert "claude -p exited 1" in report.errors[0]
    for _ in range(MAX_ATTEMPTS):
        clock.now += timedelta(hours=8)
        await sync.run_once()
    [status] = sync.store.statuses()
    assert status.failed == [("Caption 1", "writing: claude -p exited 1")]


async def test_unfollow_and_follow_again(setup):
    sync, ig, written, _, clock, _ = setup
    sync.follow("instagram.com/mkbhd")
    await sync.run_once(limit=2)
    assert sync.unfollow("instagram:mkbhd") and not sync.unfollow("instagram:mkbhd")
    assert sync.store.statuses() == []
    assert len(sync.store.ready(now=clock.now, limit=10)) == 0  # queue dropped
    clock.now += timedelta(hours=1)
    sync.follow("instagram.com/mkbhd")
    ig.opened.clear()
    report = await sync.run_once()
    assert ig.opened == []  # every grid post is known: dates come from the DB
    assert report.queued == 3 and len(written) == 5


async def test_a_collab_post_belongs_to_the_followed_creator(setup):
    sync, ig, _, _, _, db = setup
    collab = post(0, age=timedelta(hours=1), username="disneyplus")
    ig.add("mkbhd", collab, front=True)
    sync.follow("instagram.com/mkbhd")
    await sync.run_once()
    insight = InsightStore(db).get(post_key(code(0)))
    assert insight.source_session == "ig:mkbhd"
