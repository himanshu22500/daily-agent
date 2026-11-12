"""Instagram stories in the web feed and on the Sources page (issue #105).
Offline: a fake Instagram (no browser) and a fake writer."""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.instagram_posts import InstagramStore, InstagramSync, post_key
from daily_agent.models import Insight, VideoStory
from daily_agent.sources.instagram import Post, Profile
from daily_agent.web.app import create_app

NOW = datetime.now(timezone.utc)
CODE = "DfDZ1iAvvDy"


class FakeBrowser:
    async def profile(self, username):
        return Profile(
            username=username,
            name="Marques Brownlee",
            codes=[f"Post{n:07d}" for n in range(1, 4)],
        )

    async def post(self, code):
        n = int(code[4:])
        return Post(
            code=code,
            username="mkbhd",
            author="Marques Brownlee",
            kind="photo",
            caption=f"Caption {n}",
            taken_at=NOW - timedelta(days=n),
            image_url=f"https://x.cdninstagram.com/{n}.jpg",
        )

    async def image(self, url):
        return b"\xff\xd8 jpeg " + url.encode(), "image/jpeg"


class FakeInstagram:
    @asynccontextmanager
    async def session(self):
        yield FakeBrowser()


async def _writer(post: Post) -> VideoStory:
    return VideoStory(headline=f"Story: {post.caption}", text="What it says, briefly.")


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "web.db")


@pytest.fixture
def client(db) -> TestClient:
    return TestClient(
        create_app(db, instagram=InstagramSync(db, FakeInstagram(), _writer))
    )


def _story(db: str) -> None:
    InsightStore(db).add(
        Insight(
            key=post_key(CODE),
            text="Marques says every demo fumbles.\n\nMore detail.",
            title="Marques finds the PROLED MacBook Pro hard to demo",
            type="instagram",
            tags=["laptops"],
            source_session="ig:mkbhd",
            captured_at=NOW - timedelta(days=1),
            status="web-only",
        )
    )
    store = InstagramStore(db)
    store.follow("mkbhd", now=NOW)
    store.mark_checked(
        "instagram:mkbhd", now=NOW, next_at=NOW, title="Marques Brownlee"
    )


def test_card_shows_the_stored_image_and_reader_embeds_the_post(client, db):
    _story(db)
    page = client.get("/?view=latest").text
    assert f'src="/media/instagram/{CODE}"' in page
    assert 'class="cover has-art is-instagram"' in page
    assert "◎ Marques Brownlee" in page
    assert ">Instagram</a>" in page  # the type chip
    reader = client.get(f"/i/instagram:{CODE}").text
    assert f'<iframe src="https://www.instagram.com/p/{CODE}/embed/"' in reader
    assert 'referrerpolicy="no-referrer"' in reader
    assert f'href="https://www.instagram.com/p/{CODE}/"' in reader
    assert "From <strong>Marques Brownlee</strong> on Instagram" in reader
    assert "Learned in" not in reader


def test_images_are_served_locked_down_with_a_placeholder(client, db):
    InstagramStore(db).save_image(CODE, b"\xff\xd8jpeg", "image/jpeg", now=NOW)
    image = client.get(f"/media/instagram/{CODE}")
    assert image.status_code == 200 and image.content == b"\xff\xd8jpeg"
    assert image.headers["content-type"] == "image/jpeg"
    assert image.headers["content-security-policy"] == "default-src 'none'"
    assert image.headers["x-content-type-options"] == "nosniff"
    for code in ("Missing0000", "not.a.code", "x" * 60):
        missing = client.get(f"/media/instagram/{code}")
        assert missing.status_code == 200
        assert missing.headers["content-type"].startswith("image/svg+xml")
        assert missing.headers["content-security-policy"] == "default-src 'none'"
        assert "<svg" in missing.text and "script" not in missing.text
    assert client.get("/media/instagram/..%2F..%2Fetc").status_code == 404


def test_sources_page_follows_and_unfollows_instagram_creators(client):
    followed = client.post(
        "/sources", data={"link": "instagram.com/mkbhd"}, follow_redirects=False
    )
    assert followed.status_code == 303
    page = client.get("/sources").text
    assert "@mkbhd" in page and 'href="https://www.instagram.com/mkbhd/"' in page
    assert "Instagram · Its latest posts will appear after the first check" in page
    source_id = re.search(r'name="id" value="([^"]+)"', page).group(1)
    assert source_id == "instagram:mkbhd"
    gone = client.post(
        "/sources/unfollow", data={"id": source_id}, follow_redirects=False
    )
    assert gone.status_code == 303
    assert "Not following anything yet." in client.get("/sources").text


def test_bad_instagram_links_are_explained(client):
    bad = client.post("/sources", data={"link": "instagram.com/p/DfDZ1iAvvDy/"})
    assert bad.status_code == 400 and "link to one post" in bad.text


def test_without_instagram_sync_instagram_links_are_refused(db):
    client = TestClient(create_app(db))
    refused = client.post("/sources", data={"link": "instagram.com/mkbhd"})
    assert refused.status_code == 503 and "Instagram" in refused.text


def test_background_loop_checks_and_writes_after_following(db):
    app = create_app(
        db,
        instagram=InstagramSync(db, FakeInstagram(), _writer),
        instagram_loop=True,
        video_idle_seconds=30,
    )
    with TestClient(app) as client:
        client.post("/sources", data={"link": "instagram.com/mkbhd"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "Up to date" in client.get("/sources").text:
                break
            time.sleep(0.05)
        page = client.get("/sources").text
        assert "Marques Brownlee" in page
        assert "Instagram · Up to date" in page and "3 stories" in page
        feed = client.get("/?view=latest").text
        assert "Story: Caption 1" in feed
        assert client.get("/media/instagram/Post0000001").content.startswith(
            b"\xff\xd8"
        )
