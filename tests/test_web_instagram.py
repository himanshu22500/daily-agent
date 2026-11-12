"""Instagram stories in the web feed and on the Sources page (issue #107). Offline."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from daily_agent import cli
from daily_agent.feed.instagram_posts import InstagramSync
from daily_agent.sources.instagram import (
    Account,
    AccountFeed,
    BadSourceLink,
    InstagramAuthError,
    LinkedAccount,
    Post,
    PostImage,
)
from daily_agent.models import VideoStory
from daily_agent.web.app import create_app

NOW = datetime.now(timezone.utc)
JPEG = b"\xff\xd8\xff\xe0 fake jpeg"


def _post(n: int, kind: str = "CAROUSEL_ALBUM") -> Post:
    return Post(
        shortcode=f"Cpost{n:06d}",
        username="bytebytego",
        caption=f"Post {n}",
        media_type=kind,
        product_type="REELS" if kind == "VIDEO" else "FEED",
        permalink=f"https://www.instagram.com/p/Cpost{n:06d}/",
        published_at=NOW - timedelta(days=n),
        image_urls=tuple(
            f"https://scontent.cdninstagram.com/{n}-{s}.jpg" for s in range(3)
        ),
    )


class FakeInstagram:
    def __init__(self):
        self.error: Exception | None = None
        self.posts = [_post(1), _post(2, "VIDEO")] + [_post(n) for n in range(3, 8)]

    async def account(self, username):
        if self.error:
            raise self.error
        if username == "nobody":
            raise BadSourceLink(
                "Instagram has no public Business or Creator account named @nobody."
            )
        return AccountFeed(
            Account("1784", "bytebytego", "ByteByteGo"), list(self.posts)
        )

    async def images(self, post):
        count = 1 if post.media_type == "VIDEO" else 3
        return [PostImage("image/jpeg", JPEG + bytes([i])) for i in range(count)]


async def _writer(post, account_name, images):
    return VideoStory(
        headline=f"Story: {post.caption}",
        text="What the slides say. In detail.",
        tags=["caching"],
    )


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "web.db")


@pytest.fixture
def instagram() -> FakeInstagram:
    return FakeInstagram()


@pytest.fixture
def sync(db, instagram) -> InstagramSync:
    return InstagramSync(db, instagram, _writer)


@pytest.fixture
def client(db, sync) -> TestClient:
    return TestClient(create_app(db, instagram=sync))


async def _written(sync: InstagramSync, n: int = 2) -> None:
    await sync.follow("instagram.com/bytebytego")
    await sync.run_once(limit=n)


async def test_cards_show_the_stored_image_and_the_reader_the_slides(client, sync):
    await _written(sync)
    page = client.get("/?view=latest").text
    assert 'src="/ig/Cpost000001/0.jpg"' in page
    assert 'class="cover has-art is-post"' in page and "1/3" in page
    assert "@bytebytego" in page and ">Instagram</a>" in page  # badge and type chip
    # The reel's card has a play mark.
    assert 'class="cover has-art is-post is-video"' in page
    reader = client.get("/i/ig:Cpost000001").text
    assert reader.count('class="post-slide"') == 3
    assert 'src="/ig/Cpost000001/2.jpg"' in reader
    assert 'href="https://www.instagram.com/p/Cpost000001/"' in reader
    assert "View on Instagram" in reader and "3 slides" in reader
    assert "From <strong>ByteByteGo</strong> (@bytebytego) on Instagram" in reader
    assert "Learned in" not in reader
    assert "Watch on Instagram" in client.get("/i/ig:Cpost000002").text


async def test_stored_images_are_served_locked_down(client, sync):
    await _written(sync, 1)
    image = client.get("/ig/Cpost000001/1.jpg")
    assert image.status_code == 200 and image.content == JPEG + b"\x01"
    assert image.headers["content-type"] == "image/jpeg"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert image.headers["content-security-policy"] == "default-src 'none'"
    assert client.get("/ig/Cpost000001/9.jpg").status_code == 404
    assert client.get("/ig/nope!/0.jpg").status_code == 404


def test_sources_page_follows_and_unfollows_accounts(client):
    followed = client.post(
        "/sources",
        data={"link": "https://www.instagram.com/ByteByteGo/"},
        follow_redirects=False,
    )
    assert followed.status_code == 303
    page = client.get("/sources").text
    assert "ByteByteGo" in page and "5 posts queued" in page and ">Instagram<" in page
    assert "isn't connected" not in page
    source_id = re.search(r'name="id" value="([^"]+)"', page).group(1)
    assert source_id == "instagram:bytebytego"
    client.post("/sources/unfollow", data={"id": source_id})
    assert "Not following anything yet." in client.get("/sources").text


def test_instagram_problems_are_explained(client, instagram):
    bad = client.post("/sources", data={"link": "instagram.com/nobody"})
    assert (
        bad.status_code == 400 and "no public Business or Creator account" in bad.text
    )
    post_link = client.post("/sources", data={"link": "instagram.com/p/Cpost000001/"})
    assert post_link.status_code == 400 and "link to a post" in post_link.text
    instagram.error = InstagramAuthError(
        "Instagram rejected the saved token (expired)."
    )
    expired = client.post("/sources", data={"link": "instagram.com/bytebytego"})
    assert expired.status_code == 502 and "rejected the saved token" in expired.text


def test_without_a_connection_instagram_links_are_explained(db):
    client = TestClient(create_app(db))
    assert "Instagram isn't connected yet" in client.get("/sources").text
    refused = client.post("/sources", data={"link": "instagram.com/bytebytego"})
    assert refused.status_code == 503
    assert "daily-agent instagram connect" in refused.text


def test_background_loop_writes_posts_after_following(db, instagram):
    app = create_app(
        db,
        instagram=InstagramSync(db, instagram, _writer),
        video_loop=True,
        video_idle_seconds=30,
    )
    with TestClient(app) as client:
        client.post("/sources", data={"link": "instagram.com/bytebytego"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "Up to date" in client.get("/sources").text:
                break
            time.sleep(0.05)
        page = client.get("/sources").text
        assert "Up to date" in page and "5 stories" in page
        assert "Story: Post 1" in client.get("/?view=latest").text


def test_connect_saves_the_page_token_without_printing_it(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    env = tmp_path / ".env"
    env.write_text("DAILY_AGENT_MODEL=claude-code:sonnet\n")
    env.chmod(0o600)
    calls = {}

    async def fake_linked(app_id, app_secret, short):
        calls.update(app_id=app_id, secret=app_secret, short=short)
        return [
            LinkedAccount("Other Page", "OTHER-TOKEN", "1000", "other"),
            LinkedAccount("My Page", "PAGE-TOKEN-SECRET", "1784", "me.dev"),
        ]

    class FakeClient:
        def __init__(self, token, user_id):
            calls["verified_with"] = (token, user_id)

        async def account(self, username):
            calls["verified"] = username

    monkeypatch.setattr(cli, "_linked", fake_linked)
    monkeypatch.setattr(cli, "Instagram", FakeClient)
    result = CliRunner().invoke(
        cli.app,
        ["instagram", "connect", "--env-file", str(env)],
        input="APP-ID\nAPP-SECRET\nSHORT-TOKEN\n2\n",
    )
    assert result.exit_code == 0, result.output
    assert calls["secret"] == "APP-SECRET" and calls["short"] == "SHORT-TOKEN"
    assert (
        calls["verified_with"] == ("PAGE-TOKEN-SECRET", "1784")
        and calls["verified"] == "me.dev"
    )
    for secret in ("APP-SECRET", "SHORT-TOKEN", "PAGE-TOKEN-SECRET"):
        assert secret not in result.output
    assert "Connected @me.dev (Page: My Page)" in result.output
    saved = env.read_text()
    assert "DAILY_AGENT_MODEL=claude-code:sonnet" in saved  # the rest is kept
    assert "DAILY_AGENT_INSTAGRAM_TOKEN='PAGE-TOKEN-SECRET'" in saved
    assert "DAILY_AGENT_INSTAGRAM_USER_ID='1784'" in saved
    assert env.stat().st_mode & 0o777 == 0o600


def test_connect_stops_when_no_account_is_linked(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    async def none_linked(*args):
        return []

    monkeypatch.setattr(cli, "_linked", none_linked)
    env = tmp_path / ".env"
    result = CliRunner().invoke(
        cli.app, ["instagram", "connect", "--env-file", str(env)], input="A\nB\nC\n"
    )
    assert result.exit_code == 1 and "No Facebook Page" in result.output
    assert not env.exists()
