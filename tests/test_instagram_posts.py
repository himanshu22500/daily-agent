"""Following Instagram accounts and writing their stories (issue #107).
Offline: a fake Instagram and a fake writer stand in for the API and Claude."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daily_agent.feed.insights_feed import enqueue_new_insights
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.instagram_posts import (
    CHECK_EVERY,
    InstagramSync,
    post_key,
    shortcode_of,
)
from daily_agent.feed.outbox import Outbox
from daily_agent.feed.story_queue import MAX_ATTEMPTS, SKIPPED
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import VideoStory
from daily_agent.sources.instagram import (
    Account,
    AccountFeed,
    BadSourceLink,
    InstagramAuthError,
    MediaExpired,
    Post,
    PostImage,
)

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
CDN = "https://scontent.cdninstagram.com/"


def code(n: int) -> str:
    return f"Cpost{n:06d}"


def post(
    n: int, *, age: timedelta, caption: str | None = None, urls=None, kind="IMAGE"
) -> Post:
    return Post(
        shortcode=code(n),
        username="bytebytego",
        caption=f"Post {n}: about caching" if caption is None else caption,
        media_type=kind,
        product_type="REELS" if kind == "VIDEO" else "FEED",
        permalink=f"https://www.instagram.com/p/{code(n)}/",
        published_at=NOW - age,
        image_urls=tuple(urls if urls is not None else [f"{CDN}{n}.jpg?v=1"]),
    )


class FakeInstagram:
    def __init__(self, posts: list[Post]):
        self.posts = list(posts)
        self.account_error: Exception | None = None
        self.expired: set[str] = set()  # URLs that answer 403
        self.reads = 0

    async def account(self, username: str) -> AccountFeed:
        self.reads += 1
        if self.account_error:
            raise self.account_error
        if username == "nobody":
            raise BadSourceLink("no such account")
        return AccountFeed(
            account=Account(id="1784", username="bytebytego", name="ByteByteGo"),
            posts=list(self.posts),
        )

    async def images(self, p: Post) -> list[PostImage]:
        if p.image_urls and p.image_urls[0] in self.expired:
            raise MediaExpired("image URL answered 403")
        return [PostImage("image/jpeg", url.encode()) for url in p.image_urls]


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "ig.db"
    ig = FakeInstagram([post(n, age=timedelta(days=n)) for n in range(1, 8)])
    written: list[tuple[str, str, int]] = []
    state = {"now": NOW, "fail": set()}

    async def writer(p: Post, account_name: str, images: list[PostImage]) -> VideoStory:
        if p.shortcode in state["fail"]:
            raise RuntimeError("claude error 529: overloaded\ntrace")
        written.append((p.shortcode, account_name, len(images)))
        return VideoStory(
            headline=f"Story for {p.shortcode}",
            text=f"What {p.shortcode} shows. More detail here.",
            tags=["caching"],
        )

    sync = InstagramSync(db, ig, writer, clock=lambda: state["now"])
    return sync, ig, written, state, db


async def test_follow_queues_the_newest_five(setup):
    sync, ig, written, _, _ = setup
    source = await sync.follow("https://www.instagram.com/ByteByteGo/")
    assert source.id == "instagram:bytebytego" and source.title == "ByteByteGo"
    (status,) = sync.store.statuses()
    assert (status.queued, status.stories, status.platform) == (5, 0, "instagram")
    assert written == []  # nothing is written while following


async def test_run_once_writes_newest_first_as_web_only_stories(setup, tmp_path):
    sync, _, written, _, db = setup
    await sync.follow("instagram.com/bytebytego")
    report = await sync.run_once(limit=2)
    assert (report.written, report.remaining) == (2, 3)
    assert [w[0] for w in written] == [code(1), code(2)]
    assert written[0][1:] == ("ByteByteGo", 1)  # account name and its image
    story = InsightStore(db).get(post_key(code(1)))
    assert story.type == "instagram" and story.status == "web-only"
    assert story.title == f"Story for {code(1)}" and story.tags == ["caching"]
    assert story.source_session == "ig:bytebytego"
    assert story.captured_at == NOW - timedelta(days=1)
    # The image is kept, because Instagram's URLs expire.
    image = sync.store.image(code(1), 0)
    assert image is not None and image.data == f"{CDN}1.jpg?v=1".encode()
    media = sync.store.media(code(1))
    assert (media.images, media.kind, media.username) == (1, "photo", "bytebytego")
    assert media.image_url(0) == f"/ig/{code(1)}/0.jpg" and media.image_url(1) == ""
    # Never queued for Telegram.
    outbox = Outbox(tmp_path / "outbox.db")
    assert enqueue_new_insights(InsightStore(db), outbox) == 0


async def test_stories_read_like_feed_entries_from_their_account(setup):
    sync, _, _, _, db = setup
    await sync.follow("instagram.com/bytebytego")
    await sync.run_once(limit=1)
    (entry,) = WebFeed(db).page(view="latest").entries
    assert entry.post_id == code(1) and entry.video_id == ""
    assert entry.source == "ByteByteGo"
    assert entry.post_url == f"https://www.instagram.com/p/{code(1)}/"


def test_shortcodes_only_come_from_post_keys():
    assert shortcode_of("ig:Cpost000001") == "Cpost000001"
    assert shortcode_of("insight:ig:x") == ""
    assert shortcode_of("ig:../etc") == ""


async def test_accounts_are_rechecked_only_when_due(setup):
    sync, ig, _, state, _ = setup
    await sync.follow("instagram.com/bytebytego")
    reads = ig.reads
    await sync.run_once(limit=1)
    assert ig.reads == reads  # just checked by follow
    ig.posts.insert(0, post(0, age=timedelta(minutes=5)))
    state["now"] = NOW + CHECK_EVERY + timedelta(minutes=1)
    report = await sync.run_once(limit=1)
    assert (report.checked, report.queued) == (1, 1)
    assert sync.store.media(code(0)) is not None


async def test_expired_image_urls_are_renewed_from_the_account(setup):
    sync, ig, written, _, _ = setup
    await sync.follow("instagram.com/bytebytego")
    stale = f"{CDN}1.jpg?v=1"
    ig.expired.add(stale)
    ig.posts[0] = post(1, age=timedelta(days=1), urls=[f"{CDN}1.jpg?v=2"])
    await sync.run_once(limit=1)
    assert written == [(code(1), "ByteByteGo", 1)]
    assert sync.store.image(code(1), 0).data == f"{CDN}1.jpg?v=2".encode()


async def test_a_post_that_left_the_account_is_written_from_its_caption(setup):
    sync, ig, written, _, _ = setup
    await sync.follow("instagram.com/bytebytego")
    ig.expired.add(f"{CDN}1.jpg?v=1")
    ig.posts = ig.posts[1:]  # no longer among the latest posts
    await sync.run_once(limit=1)
    assert written == [(code(1), "ByteByteGo", 0)]
    assert sync.store.media(code(1)).images == 0


async def test_a_post_with_nothing_to_read_is_skipped(setup):
    sync, ig, written, _, _ = setup
    ig.posts = [post(1, age=timedelta(days=1), caption="", urls=[])]
    await sync.follow("instagram.com/bytebytego")
    report = await sync.run_once()
    assert report.written == 0 and written == []
    with sync.store._engine.connect() as conn:  # noqa: SLF001 - test peek
        state = conn.exec_driver_sql(
            "SELECT state FROM instagram_posts WHERE shortcode = ?", (code(1),)
        ).scalar()
    assert state == SKIPPED


async def test_failures_retry_with_backoff_then_give_up(setup):
    sync, ig, _, state, _ = setup
    ig.posts = [post(1, age=timedelta(days=1))]
    await sync.follow("instagram.com/bytebytego")
    state["fail"].add(code(1))
    for attempt in range(MAX_ATTEMPTS):
        report = await sync.run_once()
        assert report.errors == [
            "Post 1: about caching: writing: claude error 529: overloaded"
        ]
        # Not retried before its backoff (1h, 2h, 4h) is over.
        assert (await sync.run_once()).errors == []
        state["now"] += timedelta(hours=2**attempt, minutes=1)
    assert report.failed == 1
    (status,) = sync.store.statuses()
    assert status.failed == [
        ("Post 1: about caching", "writing: claude error 529: overloaded")
    ]
    assert (await sync.run_once()).errors == []  # given up


async def test_a_rejected_token_is_reported_on_the_source(setup):
    sync, ig, _, state, _ = setup
    await sync.follow("instagram.com/bytebytego")
    ig.account_error = InstagramAuthError(
        "Instagram rejected the saved token (expired)."
    )
    state["now"] = NOW + CHECK_EVERY + timedelta(minutes=1)
    report = await sync.run_once(limit=0)
    assert "rejected the saved token" in report.errors[0]
    (status,) = sync.store.statuses()
    assert "rejected the saved token" in status.check_error


async def test_unfollow_drops_the_queue_and_keeps_stories(setup):
    sync, _, _, _, db = setup
    source = await sync.follow("instagram.com/bytebytego")
    await sync.run_once(limit=1)
    assert sync.unfollow(source.id)
    assert sync.store.statuses() == []
    assert (await sync.run_once()).written == 0
    assert InsightStore(db).get(post_key(code(1))) is not None
    # Following again brings the dropped posts back.
    await sync.follow("instagram.com/bytebytego")
    (status,) = sync.store.statuses()
    assert status.queued == 4 and status.stories == 1


async def test_follow_errors_store_nothing(setup):
    sync, _, _, _, _ = setup
    with pytest.raises(BadSourceLink):
        await sync.follow("instagram.com/p/Cpost000001/")
    with pytest.raises(BadSourceLink):
        await sync.follow("instagram.com/nobody")
    assert sync.store.statuses() == []
