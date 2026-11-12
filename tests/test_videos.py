"""Following YouTube channels and writing their stories (issue #99). Offline:
a fake YouTube and a fake writer stand in for the network and Claude."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from daily_agent.feed import videos as videos_mod
from daily_agent.feed.covers import cover_slug
from daily_agent.feed.insights_feed import enqueue_new_insights
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.outbox import Outbox
from daily_agent.feed.videos import (
    CAPTION_WAIT,
    CHECK_EVERY,
    CLAIM_TTL,
    MAX_ATTEMPTS,
    VideoStore,
    VideoSync,
    sync_forever,
    video_id_of,
    video_key,
)
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import VideoStory
from daily_agent.sources.youtube import (
    BadSourceLink,
    ChannelFeed,
    FeedUnavailable,
    NoCaptions,
    Transcript,
    Video,
    YouTubeError,
    channel_ref,
)

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
CHANNEL = "UCsBjURrPoezykLs9EqgamOA"


def vid(n: int) -> str:
    return f"video{n:06d}"  # 11 chars, a valid YouTube id


def upload(n: int, *, age: timedelta, short: bool = False) -> Video:
    return Video(
        video_id=vid(n),
        title=f"Video {n}",
        published_at=NOW - age,
        description=f"About video {n}",
        channel_id=CHANNEL,
        channel="Fireship",
        short=short,
    )


class FakeYouTube:
    def __init__(self, videos: list[Video]):
        self.videos = list(videos)
        self.feed_error: Exception | None = None
        self.page_error: Exception | None = None
        self.no_captions: set[str] = set()
        self.caption_errors: dict[str, Exception] = {}
        self.feeds = 0

    async def resolve(self, link: str):
        if "nope" in link:
            raise BadSourceLink("not a channel")
        return channel_ref(CHANNEL)

    async def feed(self, feed_url: str) -> ChannelFeed:
        self.feeds += 1
        if self.feed_error:
            raise self.feed_error
        return ChannelFeed(title="Fireship", videos=list(self.videos))

    async def page_title(self, ref) -> str:
        if self.page_error:
            raise self.page_error
        return "Fireship (page)"

    async def transcript(self, video_id: str) -> Transcript:
        if video_id in self.caption_errors:
            raise self.caption_errors[video_id]
        if video_id in self.no_captions:
            raise NoCaptions("disabled")
        return Transcript(text=f"captions of {video_id}", language="en")


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def setup(tmp_path):
    db = str(tmp_path / "videos.db")
    youtube = FakeYouTube(
        [upload(n, age=timedelta(days=n)) for n in range(1, 9)]
        + [upload(0, age=timedelta(hours=12), short=True)]
    )
    written: list[tuple[str, Transcript | None]] = []
    failures: dict[str, Exception] = {}

    async def writer(video: Video, transcript: Transcript | None) -> VideoStory:
        if video.video_id in failures:
            raise failures[video.video_id]
        written.append((video.video_id, transcript))
        return VideoStory(
            headline=f"Story of {video.title}",
            text=f"What {video.title} says.\n\nMore detail.",
            tags=["topic"],
        )

    clock = Clock()
    sync = VideoSync(db, youtube, writer, clock=clock)
    return sync, youtube, written, failures, clock, db


async def test_follow_queues_the_latest_five_and_skips_shorts(setup):
    sync, youtube, written, _, _, _ = setup
    source = await sync.follow("youtube.com/@Fireship")
    assert source.id == f"channel:{CHANNEL}" and source.title == "Fireship"
    [status] = sync.store.statuses()
    assert (status.queued, status.stories, status.title) == (5, 0, "Fireship")
    assert written == []  # following never writes; run_once does


async def test_run_once_writes_newest_first_as_web_only_video_stories(setup):
    sync, youtube, written, _, _, db = setup
    await sync.follow("@Fireship")
    report = await sync.run_once(limit=2)
    assert [v for v, _ in written] == [vid(1), vid(2)]
    assert (report.written, report.remaining, report.checked) == (2, 3, 0)
    story = InsightStore(db).get(video_key(vid(1)))
    assert story.type == "video" and story.status == "web-only"
    assert story.title == "Story of Video 1" and story.tags == ["topic"]
    assert story.captured_at == NOW - timedelta(days=1)
    assert story.source_session == f"yt:{CHANNEL}"
    # Never queued for Telegram.
    assert enqueue_new_insights(InsightStore(db), Outbox(db)) == 0

    await sync.run_once()
    assert [v for v, _ in written][2:] == [vid(3), vid(4), vid(5)]
    assert (await sync.run_once()).written == 0  # older ones were skipped


async def test_stories_read_like_feed_entries_from_their_channel(setup):
    sync, _, _, _, _, db = setup
    await sync.follow("@Fireship")
    await sync.run_once(limit=1)
    entry = WebFeed(db).entry(video_key(vid(1)))
    assert entry.source == "Fireship"
    assert entry.video_id == vid(1)
    assert entry.thumbnail == f"https://i.ytimg.com/vi/{vid(1)}/hqdefault.jpg"
    assert entry.embed_url.startswith(
        f"https://www.youtube-nocookie.com/embed/{vid(1)}"
    )
    assert entry.video_url == f"https://www.youtube.com/watch?v={vid(1)}"
    assert ("video", 1) in WebFeed(db).types()


def test_video_ids_only_come_from_video_keys():
    assert video_id_of(video_key("1FurWDiK9A4")) == "1FurWDiK9A4"
    # Video stories use their YouTube thumbnail; the illustrator skips them.
    assert cover_slug(video_key("1FurWDiK9A4")) is None
    assert video_id_of("insight:abc") == ""
    assert video_id_of('video:"><script>') == ""


async def test_sources_are_rechecked_only_when_due(setup):
    sync, youtube, written, _, clock, _ = setup
    await sync.follow("@Fireship")
    await sync.run_once()
    assert youtube.feeds == 1
    youtube.videos.insert(0, upload(99, age=timedelta(minutes=5)))
    clock.now += CHECK_EVERY - timedelta(minutes=1)
    await sync.run_once()
    assert youtube.feeds == 1
    clock.now += timedelta(minutes=2)
    report = await sync.run_once()
    assert (report.checked, report.queued) == (1, 1)
    assert written[-1][0] == vid(99)


async def test_new_video_without_captions_waits_then_uses_the_description(setup):
    sync, youtube, written, _, clock, db = setup
    await sync.follow("@Fireship")
    await sync.run_once()
    youtube.videos.insert(0, upload(50, age=timedelta(hours=1)))
    youtube.no_captions.add(vid(50))
    clock.now += CHECK_EVERY
    report = await sync.run_once()
    assert (report.queued, report.waiting, report.written) == (1, 1, 0)
    assert (await sync.run_once()).written == 0  # deferred, not retried at once
    clock.now += CAPTION_WAIT
    assert (await sync.run_once()).written == 1
    assert written[-1] == (vid(50), None)
    assert InsightStore(db).get(video_key(vid(50))) is not None


async def test_old_video_without_captions_is_written_from_its_description(setup):
    sync, youtube, written, _, _, _ = setup
    youtube.no_captions.add(vid(1))
    await sync.follow("@Fireship")
    await sync.run_once(limit=1)
    assert written == [(vid(1), None)]


async def test_failures_retry_with_backoff_then_give_up(setup):
    sync, youtube, written, failures, clock, _ = setup
    youtube.caption_errors[vid(1)] = YouTubeError("blocked")
    failures[vid(2)] = RuntimeError("claude error 429: Usage limit reached\nmore")
    await sync.follow("@Fireship")
    report = await sync.run_once(limit=2)
    assert report.written == 0 and len(report.errors) == 2
    assert (await sync.run_once(limit=2)).written == 2  # others go ahead meanwhile
    for _ in range(MAX_ATTEMPTS):
        clock.now += timedelta(days=1)
        await sync.run_once()
    [status] = sync.store.statuses()
    assert status.queued == 0 and status.stories == 3
    assert sorted(status.failed) == [
        ("Video 1", "captions: blocked"),
        ("Video 2", "writing: claude error 429: Usage limit reached"),
    ]
    # Following again retries them.
    youtube.caption_errors.clear()
    failures.clear()
    await sync.follow("@Fireship")
    assert sync.store.statuses()[0].queued == 2
    assert (await sync.run_once()).written == 2


async def test_a_claimed_video_is_never_written_twice(setup):
    sync, _, written, _, clock, _ = setup
    await sync.follow("@Fireship")
    store = sync.store
    assert store.claim(vid(1), now=NOW)
    assert not store.claim(vid(1), now=NOW)  # another runner has it
    await sync.run_once(limit=1)
    assert [v for v, _ in written] == [vid(2)]
    assert store.claim(vid(1), now=NOW + CLAIM_TTL + timedelta(seconds=1))  # crashed


async def test_unfollow_drops_the_queue_and_keeps_stories(setup):
    sync, youtube, written, _, _, db = setup
    await sync.follow("@Fireship")
    await sync.run_once(limit=1)
    assert sync.unfollow(f"channel:{CHANNEL}")
    assert not sync.unfollow(f"channel:{CHANNEL}")
    assert sync.store.statuses() == []
    assert (await sync.run_once()).written == 0
    assert InsightStore(db).get(video_key(vid(1))) is not None
    # Following again brings back the latest five (minus what's written).
    await sync.follow("@Fireship")
    assert sync.store.statuses()[0].queued == 4


async def test_failed_check_is_reported_and_retried(setup):
    sync, youtube, _, _, clock, _ = setup
    await sync.follow("@Fireship")
    youtube.feed_error = YouTubeError("YouTube feed answered 500")
    clock.now += CHECK_EVERY
    report = await sync.run_once()
    assert report.checked == 0 and "500" in report.errors[0]
    assert "500" in sync.store.statuses()[0].check_error
    youtube.feed_error = None
    assert (await sync.run_once()).checked == 1
    assert sync.store.statuses()[0].check_error == ""


async def test_follow_errors_store_nothing(setup):
    sync, youtube, _, _, _, db = setup
    with pytest.raises(BadSourceLink):
        await sync.follow("nope")
    youtube.feed_error = FeedUnavailable("YouTube's video feed isn't answering (404)")
    youtube.page_error = BadSourceLink("no such channel")
    with pytest.raises(BadSourceLink):
        await sync.follow("@Fireship")
    youtube.page_error = YouTubeError("couldn't reach YouTube")  # all of it down
    with pytest.raises(YouTubeError):
        await sync.follow("@Fireship")
    assert VideoStore(db).statuses() == []


async def test_follow_while_the_feed_is_down_queues_once_it_answers(setup):
    """YouTube's RSS server has hours-long outages (issue #103)."""
    sync, youtube, written, _, clock, _ = setup
    youtube.feed_error = FeedUnavailable("YouTube's video feed isn't answering (404)")
    source = await sync.follow("youtube.com/@Fireship")
    assert source.title == "Fireship (page)" and source.checked_at is None
    [status] = sync.store.statuses()
    assert "isn't answering" in status.check_error and status.queued == 0

    clock.now += timedelta(minutes=10)  # still down: retried every pass
    feeds = youtube.feeds
    report = await sync.run_once()
    assert youtube.feeds == feeds + 1 and report.checked == 0 and not written

    youtube.feed_error = None
    clock.now += timedelta(minutes=10)
    report = await sync.run_once()
    assert report.checked == 1 and report.queued == 5 and report.written == 5
    [status] = sync.store.statuses()
    assert status.check_error == "" and status.checked_at == clock.now
    assert status.title == "Fireship (page)"  # a name it already had stays


async def test_a_source_without_a_name_takes_the_feed_title(setup):
    sync, youtube, _, _, _, _ = setup
    youtube.feed_error = FeedUnavailable("down")
    youtube.page_title = _no_title
    await sync.follow("@Fireship")
    assert sync.store.statuses()[0].title == f"channel:{CHANNEL}"
    youtube.feed_error = None
    await sync.run_once()
    assert sync.store.statuses()[0].title == "Fireship"


async def _no_title(ref) -> str:
    return ""


async def test_sync_forever_keeps_going_while_work_is_ready_and_wakes_on_demand():
    class Sync:
        def __init__(self):
            self.passes = 0

        async def run_once(self):
            self.passes += 1
            if self.passes == 2:
                raise RuntimeError("one bad pass")
            more = self.passes == 1
            return videos_mod.SyncReport(written=1, remaining=int(more))

    sync, wake = Sync(), asyncio.Event()
    task = asyncio.create_task(sync_forever(sync, wake, idle=3600))
    for _ in range(50):
        await asyncio.sleep(0)
    assert sync.passes == 2  # pass 1 left work, so pass 2 ran at once
    wake.set()
    for _ in range(50):
        await asyncio.sleep(0)
    assert sync.passes == 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
