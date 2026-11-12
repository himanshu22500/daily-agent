"""Video stories in the web feed and the Sources page (issue #99). Offline."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.videos import VideoSync, video_key
from daily_agent.models import Insight, VideoStory
from daily_agent.sources.youtube import (
    BadSourceLink,
    ChannelFeed,
    FeedUnavailable,
    Transcript,
    Video,
    YouTubeError,
    channel_ref,
)
from daily_agent.web.app import create_app

NOW = datetime.now(timezone.utc)
CHANNEL = "UCsBjURrPoezykLs9EqgamOA"
VIDEO_ID = "1FurWDiK9A4"


class FakeYouTube:
    def __init__(self):
        self.down = False
        self.feed_down = False

    async def resolve(self, link):
        if "example.com" in link:
            raise BadSourceLink("Only YouTube links can be followed.")
        if self.down:
            raise YouTubeError("YouTube answered 503")
        return channel_ref(CHANNEL)

    async def page_title(self, ref):
        return "Fireship"

    async def feed(self, feed_url):
        if self.feed_down:
            raise FeedUnavailable("YouTube's video feed isn't answering (404)")
        return ChannelFeed(
            title="Fireship",
            videos=[
                Video(
                    video_id=f"video{n:06d}",
                    title=f"Video {n}",
                    published_at=NOW - timedelta(days=n),
                    channel_id=CHANNEL,
                    channel="Fireship",
                )
                for n in range(1, 8)
            ],
        )

    async def transcript(self, video_id):
        return Transcript(text="captions")


async def _writer(video: Video, transcript):
    return VideoStory(headline=f"Story: {video.title}", text="What it says, in detail.")


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "web.db")


@pytest.fixture
def youtube() -> FakeYouTube:
    return FakeYouTube()


@pytest.fixture
def client(db, youtube) -> TestClient:
    return TestClient(create_app(db, videos=VideoSync(db, youtube, _writer)))


def _story(db: str) -> None:
    InsightStore(db).add(
        Insight(
            key=video_key(VIDEO_ID),
            text="Anthropic shipped Fable 5.2.\n\nMore detail.",
            title="Anthropic ships Fable 5.2",
            type="video",
            tags=["anthropic"],
            source_session=f"yt:{CHANNEL}",
            captured_at=NOW - timedelta(days=1),
            status="web-only",
        )
    )


def test_video_card_shows_the_thumbnail_and_reader_embeds_the_video(client, db):
    _story(db)
    page = client.get("/?view=latest").text
    assert f'src="https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg"' in page
    assert 'class="cover has-art is-video"' in page and 'class="play"' in page
    assert ">Video</a>" in page  # the type chip
    assert 'href="/sources"' in page
    reader = client.get(f"/i/video:{VIDEO_ID}").text
    assert (
        f'<iframe src="https://www.youtube-nocookie.com/embed/{VIDEO_ID}?rel=0"'
        in reader
    )
    assert f'href="https://www.youtube.com/watch?v={VIDEO_ID}"' in reader
    assert "Learned in" not in reader


def test_sources_page_follows_and_unfollows(client):
    assert "Not following anything yet." in client.get("/sources").text
    followed = client.post(
        "/sources", data={"link": "youtube.com/@Fireship"}, follow_redirects=False
    )
    assert followed.status_code == 303 and followed.headers["location"] == "/sources"
    page = client.get("/sources").text
    assert "Fireship" in page and "5 videos queued" in page
    source_id = re.search(r'name="id" value="([^"]+)"', page).group(1)
    assert source_id == f"channel:{CHANNEL}"
    gone = client.post(
        "/sources/unfollow", data={"id": source_id}, follow_redirects=False
    )
    assert gone.status_code == 303
    assert "Not following anything yet." in client.get("/sources").text


def test_bad_links_and_outages_are_explained(client, youtube):
    bad = client.post("/sources", data={"link": 'https://example.com/"><b>x'})
    assert bad.status_code == 400
    assert "Only YouTube links can be followed." in bad.text
    assert "&#34;&gt;&lt;b&gt;x" in bad.text  # the link is kept, escaped
    youtube.down = True
    down = client.post("/sources", data={"link": "@Fireship"})
    assert down.status_code == 502 and "Try again" in down.text


def test_following_while_youtubes_feed_is_down(db, youtube):
    """Issue #103: the follow is kept and explained, not blamed on the link."""
    youtube.feed_down = True
    app = create_app(db, videos=VideoSync(db, youtube, _writer), video_loop=False)
    client = TestClient(app)
    followed = client.post(
        "/sources", data={"link": "youtube.com/@Fireship"}, follow_redirects=False
    )
    assert followed.status_code == 303
    page = client.get("/sources").text
    assert "Fireship" in page
    assert "Its latest videos will appear once YouTube's feed answers" in page
    assert (
        "Last check failed: YouTube&#39;s video feed isn&#39;t answering (404)." in page
    )
    assert "no public video feed" not in page


def test_sources_posts_must_be_same_origin(client):
    evil = {"Origin": "https://evil.example"}
    assert client.post("/sources", data={"link": "@x"}, headers=evil).status_code == 403
    assert (
        client.post("/sources/unfollow", data={"id": "x"}, headers=evil).status_code
        == 403
    )


def test_without_video_sync_the_page_is_read_only(db):
    client = TestClient(create_app(db))
    assert "disabled" in client.get("/sources").text
    assert client.post("/sources", data={"link": "@Fireship"}).status_code == 503


def test_background_loop_writes_stories_after_following(db, youtube):
    app = create_app(
        db,
        videos=VideoSync(db, youtube, _writer),
        video_loop=True,
        video_idle_seconds=30,
    )
    with TestClient(app) as client:
        client.post("/sources", data={"link": "@Fireship"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "Up to date" in client.get("/sources").text:
                break
            time.sleep(0.05)
        page = client.get("/sources").text
        assert "Up to date" in page and "5 stories" in page
        assert "Story: Video 1" in client.get("/?view=latest").text
