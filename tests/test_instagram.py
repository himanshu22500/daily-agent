"""Instagram Graph API client (issue #107). Offline: httpx.MockTransport."""

from __future__ import annotations

import httpx
import pytest

from daily_agent.sources.instagram import (
    MAX_IMAGE_BYTES,
    BadSourceLink,
    Instagram,
    InstagramAuthError,
    InstagramError,
    MediaExpired,
    Post,
    exchange_token,
    is_cdn_url,
    is_instagram_link,
    linked_accounts,
    parse_discovery,
    parse_username,
    shortcode_from_permalink,
)

TOKEN = "PAGE-TOKEN-SECRET"
CDN = "https://scontent-bom1-1.cdninstagram.com/v/t51/"


def media(**overrides) -> dict:
    base = {
        "id": "17890000000000001",
        "shortcode": "Cabc123XYZ_",
        "caption": "Three Postgres index tips",
        "media_type": "IMAGE",
        "media_product_type": "FEED",
        "media_url": CDN + "a.jpg?oe=1",
        "permalink": "https://www.instagram.com/p/Cabc123XYZ_/",
        "timestamp": "2026-11-10T08:00:00+0000",
    }
    base.update(overrides)
    return base


def discovery(*items: dict) -> dict:
    return {
        "business_discovery": {
            "id": "17841400000000000",
            "username": "ByteByteGo",
            "name": "ByteByteGo",
            "media": {"data": list(items)},
        },
        "id": "17841499999999999",
    }


@pytest.mark.parametrize(
    "link, username",
    [
        ("instagram.com/bytebytego", "bytebytego"),
        ("https://www.instagram.com/ByteByteGo/", "bytebytego"),
        ("https://instagram.com/byte.byte_go?igsh=abc123", "byte.byte_go"),
        ("m.instagram.com/someone/reels/", "someone"),
    ],
)
def test_profile_links_name_an_account(link, username):
    assert is_instagram_link(link)
    assert parse_username(link) == username


@pytest.mark.parametrize(
    "link, message",
    [
        ("", "Paste an Instagram profile link"),
        ("youtube.com/@fireship", "isn't an Instagram link"),
        ("https://www.instagram.com/p/Cabc123XYZ_/", "link to a post"),
        ("https://www.instagram.com/reel/Cabc123XYZ_/", "link to a post"),
        ("https://www.instagram.com/explore/", "isn't an Instagram profile"),
        ("https://www.instagram.com/", "isn't an Instagram profile"),
        ("https://www.instagram.com/bad name!/", "isn't an Instagram profile"),
    ],
)
def test_other_links_are_refused(link, message):
    with pytest.raises(BadSourceLink, match=message):
        parse_username(link)


def test_youtube_links_are_not_instagram_links():
    assert not is_instagram_link("youtube.com/@fireship")
    assert not is_instagram_link("@fireship")


def test_shortcodes_and_cdn_hosts():
    assert shortcode_from_permalink("https://www.instagram.com/reel/DAbc_12-x/") == (
        "DAbc_12-x"
    )
    assert shortcode_from_permalink("https://www.instagram.com/bytebytego/") == ""
    assert is_cdn_url(CDN + "x.jpg")
    assert is_cdn_url("https://scontent.xx.fbcdn.net/x.jpg")
    assert not is_cdn_url("http://scontent.cdninstagram.com/x.jpg")  # not https
    assert not is_cdn_url("https://evil.example.com/cdninstagram.com/x.jpg")
    assert not is_cdn_url("https://cdninstagram.com.evil.example/x.jpg")


def test_discovery_reply_becomes_posts_with_their_images():
    feed = parse_discovery(
        discovery(
            media(),
            media(
                shortcode=None,
                permalink="https://www.instagram.com/p/Ccarousel01/",
                media_type="CAROUSEL_ALBUM",
                media_url=None,
                children={
                    "data": [
                        {"media_type": "IMAGE", "media_url": CDN + "s1.jpg"},
                        {
                            "media_type": "VIDEO",
                            "media_url": CDN + "v.mp4",
                            "thumbnail_url": CDN + "v-cover.jpg",
                        },
                        {
                            "media_type": "IMAGE",
                            "media_url": "https://evil.example/x.jpg",
                        },
                        {
                            "media_type": "IMAGE",
                            "media_url": CDN + "s3.jpg",
                            "alt_text": "A  chart",
                        },
                    ]
                },
            ),
            # A reel with licensed audio: no media_url, only its cover frame.
            media(
                shortcode="Creel000001",
                media_type="VIDEO",
                media_product_type="REELS",
                media_url=None,
                thumbnail_url=CDN + "reel-cover.jpg",
            ),
            media(shortcode="bad!", permalink="https://www.instagram.com/p/bad!/"),
            media(shortcode="Cad00000001", media_product_type="AD"),
        ),
        "bytebytego",
    )
    assert feed.account.username == "bytebytego" and feed.account.name == "ByteByteGo"
    photo, carousel, reel = feed.posts
    assert photo.kind == "photo" and photo.image_urls == (CDN + "a.jpg?oe=1",)
    assert photo.published_at.isoformat() == "2026-11-10T08:00:00+00:00"
    assert carousel.shortcode == "Ccarousel01" and carousel.kind == "carousel"
    # Slides in order, a video slide by its cover, foreign hosts dropped.
    assert carousel.image_urls == (CDN + "s1.jpg", CDN + "v-cover.jpg", CDN + "s3.jpg")
    assert carousel.alt_texts == ("A chart",)
    assert reel.kind == "reel" and reel.image_urls == (CDN + "reel-cover.jpg",)


def test_an_odd_username_in_the_reply_falls_back_to_the_requested_one():
    reply = discovery(media())
    reply["business_discovery"]["username"] = "x){id}"
    assert parse_discovery(reply, "bytebytego").account.username == "bytebytego"


def test_unexpected_replies_are_errors():
    with pytest.raises(InstagramError):
        parse_discovery({"id": "1"}, "x")


def _graph_reply(status: int, error: dict) -> httpx.Response:
    return httpx.Response(status, json={"error": error})


@pytest.mark.parametrize(
    "status, error, kind, message",
    [
        (400, {"code": 110, "error_subcode": 2207013}, BadSourceLink, "no public Business or Creator account named @someone"),
        (400, {"code": 190, "message": "Session has expired"}, InstagramAuthError, "instagram connect"),
        (403, {"code": 10, "message": "Permission denied"}, InstagramAuthError, "Permission denied"),
        (400, {"code": 4, "message": "Application request limit reached"}, InstagramError, "rate limit"),
        (500, {"code": 1, "message": "An unknown error occurred"}, InstagramError, "answered 500"),
    ],
)  # fmt: skip
async def test_graph_errors_are_explained(status, error, kind, message):
    transport = httpx.MockTransport(lambda request: _graph_reply(status, error))
    client = Instagram(TOKEN, "17841499999999999", transport=transport)
    with pytest.raises(kind, match=message) as caught:
        await client.account("someone")
    assert TOKEN not in str(caught.value)


async def test_the_token_goes_in_a_header_never_in_the_url():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=discovery(media()))

    client = Instagram(
        TOKEN, "17841499999999999", transport=httpx.MockTransport(handler)
    )
    feed = await client.account("bytebytego")
    assert [p.shortcode for p in feed.posts] == ["Cabc123XYZ_"]
    (request,) = seen
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert request.url.path == "/v25.0/17841499999999999"
    assert "business_discovery.username(bytebytego)" in request.url.params["fields"]


async def test_a_bad_username_never_reaches_the_api():
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    client = Instagram(TOKEN, "1", transport=httpx.MockTransport(handler))
    with pytest.raises(BadSourceLink):
        await client.account("x){id}")


def _post(*urls: str) -> Post:
    from datetime import datetime, timezone

    return Post(
        shortcode="Cabc123XYZ_",
        username="bytebytego",
        caption="",
        media_type="CAROUSEL_ALBUM",
        product_type="FEED",
        permalink="https://www.instagram.com/p/Cabc123XYZ_/",
        published_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
        image_urls=urls,
    )


async def test_images_are_downloaded_without_the_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "big.jpg":
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=b"x" * (MAX_IMAGE_BYTES + 1),
            )
        if name == "page.jpg":
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>"
            )
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg; charset=binary"},
            content=name.encode(),
        )

    client = Instagram(TOKEN, "1", transport=httpx.MockTransport(handler))
    images = await client.images(
        _post(
            CDN + "one.jpg",
            CDN + "big.jpg",
            CDN + "page.jpg",
            "https://evil.example/x.jpg",
        )
    )
    assert [(i.content_type, i.data) for i in images] == [("image/jpeg", b"one.jpg")]
    assert all("authorization" not in r.headers for r in seen)
    assert all(
        r.url.host.endswith("cdninstagram.com") for r in seen
    )  # never evil.example


async def test_an_expired_first_image_asks_for_fresh_urls():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, text="URL signature expired")
    )
    client = Instagram(TOKEN, "1", transport=transport)
    with pytest.raises(MediaExpired):
        await client.images(_post(CDN + "one.jpg"))


async def test_connecting_swaps_tokens_and_finds_the_linked_account():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/oauth/access_token"):
            assert request.url.params["grant_type"] == "fb_exchange_token"
            assert request.url.params["fb_exchange_token"] == "SHORT"
            return httpx.Response(
                200, json={"access_token": "LONG", "expires_in": 5184000}
            )
        assert request.headers["authorization"] == "Bearer LONG"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": "No IG Page", "access_token": "P0", "id": "1"},
                    {
                        "name": "My Page",
                        "access_token": "PAGE",
                        "id": "2",
                        "instagram_business_account": {
                            "id": "1784",
                            "username": "me.dev",
                        },
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    long_lived = await exchange_token("APP", "SECRET", "SHORT", transport=transport)
    assert long_lived == "LONG"
    (account,) = await linked_accounts(long_lived, transport=transport)
    assert (
        account.page_name,
        account.page_token,
        account.ig_user_id,
        account.ig_username,
    ) == (
        "My Page",
        "PAGE",
        "1784",
        "me.dev",
    )


async def test_a_rejected_exchange_is_explained():
    transport = httpx.MockTransport(
        lambda request: _graph_reply(
            400, {"code": 190, "message": "Error validating access token"}
        )
    )
    with pytest.raises(
        InstagramAuthError, match="Error validating access token"
    ) as caught:
        await exchange_token("APP", "SECRET", "SHORT", transport=transport)
    assert "SECRET" not in str(caught.value) and "SHORT" not in str(caught.value)
