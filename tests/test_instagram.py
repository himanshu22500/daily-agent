"""Reading Instagram with a logged-out browser (issue #105). Offline: pages are
fixtures and a fake Playwright page stands in for Chrome."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from daily_agent.sources.instagram import (
    AccountUnavailable,
    BadInstagramLink,
    Browser,
    InstagramBlocked,
    InstagramError,
    codes_from_links,
    embed_url,
    is_instagram_link,
    name_from_title,
    page_state,
    parse_profile_link,
    pick_image,
    post_from_page,
    post_url,
)

TAKEN = 1793223127  # 2026-10-28T21:32:07Z


def media(code: str = "DfDZ1iAvvDy", **extra) -> dict:
    return {
        "__typename": "XDTMediaDict",
        "code": code,
        "taken_at": TAKEN,
        "media_type": 2,
        "product_type": "clips",
        "caption": {"text": "MacBook Pro with PROLED XDR\n\nA long caption."},
        "user": {"username": "mkbhd", "full_name": "Marques  Brownlee"},
        "accessibility_caption": "Video by Marques Brownlee. May be an image of a laptop.",
        "image_versions2": {
            "candidates": [
                {"url": "https://x.cdninstagram.com/1080.jpg", "width": 1080},
                {"url": "https://x.cdninstagram.com/640.jpg", "width": 640},
                {"url": "https://x.cdninstagram.com/320.jpg", "width": 320},
            ]
        },
        **extra,
    }


def page_with(*blocks: object) -> str:
    scripts = "".join(
        f'<script type="application/json" data-sjs>{json.dumps(b)}</script>'
        for b in blocks
    )
    return f"<html><head>{scripts}</head><body></body></html>"


@pytest.mark.parametrize(
    ("link", "username"),
    [
        ("instagram.com/mkbhd", "mkbhd"),
        ("https://www.instagram.com/MKBHD/?hl=en", "mkbhd"),
        ("http://m.instagram.com/nat.geo_1/", "nat.geo_1"),
        ("instagr.am/natgeo", "natgeo"),
        ("https://www.instagram.com/mkbhd/reel/DfDZ1iAvvDy/", "mkbhd"),
        ("https://www.instagram.com/stories/mkbhd/3512/", "mkbhd"),
    ],
)
def test_parse_profile_link(link, username):
    assert parse_profile_link(link) == username
    assert is_instagram_link(link)


@pytest.mark.parametrize(
    ("link", "message"),
    [
        ("", "Paste an Instagram profile link"),
        ("https://youtube.com/@mkbhd", "isn't an Instagram link"),
        ("https://evil.example/instagram.com/mkbhd", "isn't an Instagram link"),
        ("instagram.com/p/DfDZ1iAvvDy/", "link to one post"),
        ("instagram.com/reel/DfDZ1iAvvDy", "link to one post"),
        ("instagram.com/explore/", "isn't an Instagram profile link"),
        ("instagram.com/", "isn't an Instagram profile link"),
        ("instagram.com/" + "a" * 31, "isn't an Instagram profile link"),
        ("instagram.com/bad%20name", "isn't an Instagram profile link"),
    ],
)
def test_parse_profile_link_rejects(link, message):
    with pytest.raises(BadInstagramLink, match=message):
        parse_profile_link(link)


def test_youtube_links_are_not_instagram_links():
    assert not is_instagram_link("youtube.com/@Fireship")
    assert not is_instagram_link("@mkbhd")


def test_codes_from_links_keeps_grid_order_once():
    hrefs = [
        "/mkbhd/reel/AAAAAA1/",
        "/p/BBBBBB2/",
        None,
        "/mkbhd/",
        "/hollywoodrecords/reel/CCCCCC3/",
        "/p/BBBBBB2/",
        "/tv/DDDDDD4",
    ]
    assert codes_from_links(hrefs) == ["AAAAAA1", "BBBBBB2", "CCCCCC3", "DDDDDD4"]


def test_name_from_title_and_page_state():
    title = "Marques Brownlee (@mkbhd) • Instagram photos and videos"
    assert name_from_title(title, "mkbhd") == "Marques Brownlee"
    assert name_from_title("Instagram", "mkbhd") == "mkbhd"
    assert page_state("Profile isn't available • Instagram") == "missing"
    assert page_state("Sorry, this page isn't available.") == "missing"
    assert page_state("This account is private") == "private"
    assert page_state("natgeo 0 posts 2 followers") == "empty"
    assert page_state("10 posts") == ""
    assert page_state("Log in Sign up") == ""


def test_post_from_page_reads_the_embedded_json():
    other = media("ZZZZZZZZZZZ", caption={"text": "someone else's post"})
    page = page_with(
        {"unrelated": True},
        {"require": [["x", {"data": {"items": [other, media()]}}]]},
    ).replace(
        "</head>",
        '<script type="application/json">{not json DfDZ1iAvvDy taken_at</script></head>',
    )
    post = post_from_page(page, "DfDZ1iAvvDy")
    assert post.code == "DfDZ1iAvvDy" and post.kind == "reel"
    assert post.username == "mkbhd" and post.author == "Marques Brownlee"
    assert post.caption.startswith("MacBook Pro")
    assert post.taken_at == datetime.fromtimestamp(TAKEN, tz=timezone.utc)
    assert post.image_description.endswith("an image of a laptop.")
    assert post.image_url == "https://x.cdninstagram.com/640.jpg"


def test_post_kinds_and_carousel_descriptions():
    photo = post_from_page(
        page_with(media(media_type=1, product_type="feed")), "DfDZ1iAvvDy"
    )
    assert photo.kind == "photo"
    child = {
        "accessibility_caption": "Photo 2. May be an image of a wolf.",
        "image_versions2": {
            "candidates": [{"url": "https://y.fbcdn.net/c.jpg", "width": 480}]
        },
    }
    carousel = post_from_page(
        page_with(
            media(
                media_type=8,
                product_type="carousel_container",
                image_versions2={},
                carousel_media=[child],
                caption=None,
            )
        ),
        "DfDZ1iAvvDy",
    )
    assert carousel.kind == "carousel" and carousel.caption == ""
    assert carousel.image_description.splitlines() == [
        "Video by Marques Brownlee. May be an image of a laptop.",
        "Photo 2. May be an image of a wolf.",
    ]
    assert carousel.image_url == "https://y.fbcdn.net/c.jpg"
    video = post_from_page(page_with(media(product_type="feed")), "DfDZ1iAvvDy")
    assert video.kind == "video"


def test_post_from_page_without_the_post():
    with pytest.raises(InstagramError, match="couldn't find"):
        post_from_page(page_with(media("ZZZZZZZZZZZ")), "DfDZ1iAvvDy")
    with pytest.raises(InstagramError):
        post_from_page("<html>login</html>", "DfDZ1iAvvDy")


def test_pick_image():
    assert pick_image(None) == ""
    assert pick_image([{"url": "u", "width": "wide"}]) == ""
    small = [{"url": "a", "width": 240}, {"url": "b", "width": 320}]
    assert pick_image(small) == "b"  # nothing wide enough: the largest


def test_urls():
    assert post_url("DfDZ1iAvvDy") == "https://www.instagram.com/p/DfDZ1iAvvDy/"
    assert embed_url("DfDZ1iAvvDy") == "https://www.instagram.com/p/DfDZ1iAvvDy/embed/"


# --- the browser, against a fake Playwright page ----------------------------- #


class FakeResponse:
    def __init__(self, status=200, body="", headers=None, data=b""):
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body
        self.headers = headers or {}
        self._data = data

    async def text(self):
        return self._body

    async def body(self):
        return self._data


class FakeRequest:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.urls: list[str] = []

    async def get(self, url, **options):
        self.urls.append(url)
        assert options.get("max_redirects") == 0
        return self.response


class FakePage:
    def __init__(self):
        self.url = ""
        self.links: list[str] = []
        self.page_title = "Marques Brownlee (@mkbhd) • Instagram photos and videos"
        self.body = "Log in Sign up"
        self.responses: dict[str, FakeResponse] = {}
        self.redirect: str | None = None
        self.visited: list[str] = []
        self.request = FakeRequest(
            FakeResponse(headers={"content-type": "image/jpeg"}, data=b"\xff\xd8jpeg")
        )

    async def goto(self, url, **_):
        self.visited.append(url)
        self.url = self.redirect or url
        return self.responses.get(url, FakeResponse(body="<html></html>"))

    async def wait_for_selector(self, selector, **_):
        if not self.links:
            raise TimeoutError("no posts")

    async def eval_on_selector_all(self, selector, script):
        return list(self.links)

    async def title(self):
        return self.page_title

    async def inner_text(self, selector):
        return self.body


@pytest.fixture
def page() -> FakePage:
    return FakePage()


@pytest.fixture
def browser(page) -> Browser:
    return Browser(page, pause=(0, 0))


async def test_profile_lists_the_grid(page, browser):
    page.links = ["/mkbhd/reel/AAAAAA1/", "/p/BBBBBB2/"]
    profile = await browser.profile("mkbhd")
    assert profile.name == "Marques Brownlee"
    assert profile.codes == ["AAAAAA1", "BBBBBB2"]
    assert page.visited == ["https://www.instagram.com/mkbhd/"]


@pytest.mark.parametrize(
    ("title", "body", "error", "message"),
    [
        ("Profile isn't available • Instagram", "", AccountUnavailable, "no account"),
        (
            "x (@x) • Instagram",
            "This account is private",
            AccountUnavailable,
            "private",
        ),
        ("x (@x) • Instagram", "Log in to see more", InstagramBlocked, "login wall"),
    ],
)
async def test_profile_without_posts_says_why(
    page, browser, title, body, error, message
):
    page.page_title, page.body = title, body
    with pytest.raises(error, match=message):
        await browser.profile("x")


async def test_profile_with_no_posts_yet(page, browser):
    page.page_title, page.body = "New (@new) • Instagram", "new 0 posts 3 followers"
    profile = await browser.profile("new")
    assert profile.codes == [] and profile.name == "New"


async def test_login_redirect_and_rate_limit_are_blocks(page, browser):
    page.redirect = "https://www.instagram.com/accounts/login/?next=/mkbhd/"
    with pytest.raises(InstagramBlocked, match="log in"):
        await browser.profile("mkbhd")
    page.redirect = None
    page.responses[post_url("DfDZ1iAvvDy")] = FakeResponse(status=429)
    with pytest.raises(InstagramBlocked, match="429"):
        await browser.post("DfDZ1iAvvDy")


async def test_post_reads_the_served_page(page, browser):
    page.responses[post_url("DfDZ1iAvvDy")] = FakeResponse(body=page_with(media()))
    post = await browser.post("DfDZ1iAvvDy")
    assert post.author == "Marques Brownlee"
    page.responses[post_url("GoneGoneGone")] = FakeResponse(status=404)
    with pytest.raises(InstagramError, match="gone"):
        await browser.post("GoneGoneGone")


async def test_bad_names_and_codes_are_never_loaded(page, browser):
    with pytest.raises(ValueError):
        await browser.profile("../accounts")
    with pytest.raises(ValueError):
        await browser.post("a/b")
    assert page.visited == []


async def test_images_only_come_from_instagram_hosts(page, browser):
    data, kind = await browser.image("https://x.cdninstagram.com/640.jpg")
    assert data.startswith(b"\xff\xd8") and kind == "image/jpeg"
    for url in (
        "http://x.cdninstagram.com/640.jpg",
        "https://evil.example/640.jpg",
        "https://cdninstagram.com.evil.example/a.jpg",
    ):
        with pytest.raises(InstagramError, match="not an Instagram image"):
            await browser.image(url)
    assert page.request.urls == ["https://x.cdninstagram.com/640.jpg"]
    page.request.response = FakeResponse(headers={"content-type": "text/html"})
    with pytest.raises(InstagramError):
        await browser.image("https://x.cdninstagram.com/640.jpg")
