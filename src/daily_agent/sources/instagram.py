"""Instagram creators as a story source (issue #105).

Instagram only shows a creator's posts to a real browser: plain HTTP requests
get an empty page shell. So this drives headless Chrome through Playwright,
logged out. No Instagram account is involved:

- ``Browser.profile``: the creator's profile page -> their name and the links to
  their latest ~12 posts (pinned posts come first).
- ``Browser.post``: one post's page -> caption, date, kind, Instagram's own
  image description, and an image link, read from the JSON Instagram embeds in
  the page it serves to a browser.
- ``Browser.image``: a post's image. Instagram's image links expire, so the
  caller stores the bytes.

Pages load one at a time, with a pause between them. Pasted links are parsed
down to a username and rebuilt on ``https://www.instagram.com``, and images are
only fetched from Instagram's own image hosts.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

BASE = "https://www.instagram.com"
_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "instagr.am",
    "www.instagr.am",
}
USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
SHORTCODE = re.compile(r"[A-Za-z0-9_-]{5,40}")
# First path segments that are Instagram's own pages, not usernames.
_RESERVED = {
    "_n",
    "_u",
    "about",
    "accounts",
    "ajax",
    "api",
    "challenge",
    "developer",
    "direct",
    "emails",
    "explore",
    "graphql",
    "legal",
    "p",
    "privacy",
    "reel",
    "reels",
    "session",
    "static",
    "stories",
    "terms",
    "tv",
    "web",
}
_POST_PATH = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]{5,40})(?:/|$)")
_POST_LINKS = 'a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]'
_JSON_SCRIPT = re.compile(
    r'<script type="application/json"[^>]*>(.*?)</script>', re.DOTALL
)
_NAME_IN_TITLE = re.compile(r"^(.*?)\s*\(@[A-Za-z0-9._]+\)")
_IMAGE_HOSTS = (".cdninstagram.com", ".fbcdn.net")
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 3_000_000
IMAGE_WIDTH = 640
PAGE_TIMEOUT_MS = 30_000
POSTS_TIMEOUT_MS = 15_000
PAUSE_SECONDS = (2.0, 4.5)


class BadInstagramLink(ValueError):
    """Not an Instagram profile link we can follow."""


class InstagramError(RuntimeError):
    """Instagram couldn't be read just now (may pass)."""


class InstagramBlocked(InstagramError):
    """Instagram showed a login wall or rate-limited this machine: back off."""


class AccountUnavailable(InstagramError):
    """The account doesn't exist (any more) or is private."""


@dataclass(frozen=True)
class Profile:
    username: str
    name: str
    codes: list[str]  # post shortcodes, in grid order


@dataclass(frozen=True)
class Post:
    code: str
    username: str
    author: str
    kind: str  # "reel", "video", "photo", or "carousel"
    caption: str
    taken_at: datetime
    image_description: str = ""
    image_url: str = ""


def profile_url(username: str) -> str:
    return f"{BASE}/{username}/"


def post_url(code: str) -> str:
    return f"{BASE}/p/{code}/"


def embed_url(code: str) -> str:
    return f"{BASE}/p/{code}/embed/"


def is_instagram_link(raw: str) -> bool:
    text = (raw or "").strip()
    if "://" not in text:
        text = "https://" + text
    return (urlsplit(text).hostname or "").lower() in _HOSTS


def parse_profile_link(raw: str) -> str:
    """The username in a pasted profile link (lowercase).

    Accepts ``instagram.com/<name>``, with or without ``https://`` or ``www``,
    and links that start with a username (``instagram.com/<name>/reel/<code>``).
    Raises ``BadInstagramLink`` for anything else.
    """
    text = (raw or "").strip()
    if not text:
        raise BadInstagramLink(
            "Paste an Instagram profile link, like instagram.com/name."
        )
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if (parts.hostname or "").lower() not in _HOSTS:
        raise BadInstagramLink("That isn't an Instagram link.")
    segments = [s for s in parts.path.split("/") if s]
    head = segments[0] if segments else ""
    if head == "stories" and len(segments) > 1:
        head = segments[1]
    elif head.lower() in ("p", "reel", "reels", "tv"):
        raise BadInstagramLink(
            "That's a link to one post. Paste the creator's profile link, "
            "like instagram.com/name."
        )
    if not head or head.lower() in _RESERVED or not USERNAME.fullmatch(head):
        raise BadInstagramLink(
            "That isn't an Instagram profile link. Try instagram.com/name."
        )
    return head.lower()


def codes_from_links(hrefs: Iterable[str | None]) -> list[str]:
    """Post shortcodes from a profile's post links, in order, without repeats."""
    codes: list[str] = []
    for href in hrefs:
        match = _POST_PATH.search(href or "")
        if match and match.group(1) not in codes:
            codes.append(match.group(1))
    return codes


def name_from_title(title: str, username: str) -> str:
    """``"Marques Brownlee (@mkbhd) • Instagram ..."`` -> ``"Marques Brownlee"``."""
    match = _NAME_IN_TITLE.match((title or "").strip())
    name = " ".join(match.group(1).split()) if match else ""
    return name or username


def page_state(text: str) -> str:
    """Why a profile page shows no posts: missing, private, empty, or "" (unknown)."""
    if "Profile isn't available" in text or "page isn't available" in text:
        return "missing"
    if "This account is private" in text or "This Account is Private" in text:
        return "private"
    if re.search(r"(?<![\d,.])0\s+posts\b", text):
        return "empty"
    return ""


def _media_objects(data: object, code: str) -> Iterable[dict]:
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("code") == code and isinstance(item.get("taken_at"), int):
                yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def pick_image(candidates: object) -> str:
    """The smallest image at least ``IMAGE_WIDTH`` wide, else the largest."""
    if not isinstance(candidates, list):
        return ""
    usable = [
        c
        for c in candidates
        if isinstance(c, dict)
        and isinstance(c.get("url"), str)
        and isinstance(c.get("width"), int)
    ]
    if not usable:
        return ""
    wide = [c for c in usable if c["width"] >= IMAGE_WIDTH]
    if wide:
        return min(wide, key=lambda c: c["width"])["url"]
    return max(usable, key=lambda c: c["width"])["url"]


def _kind(media: dict) -> str:
    if media.get("product_type") == "clips":
        return "reel"
    if (
        media.get("media_type") == 8
        or media.get("product_type") == "carousel_container"
    ):
        return "carousel"
    if media.get("media_type") == 2:
        return "video"
    return "photo"


def post_from_page(page: str, code: str) -> Post:
    """A post's details from the JSON in its page. Raises ``InstagramError``."""
    found: list[dict] = []
    for block in _JSON_SCRIPT.findall(page):
        if code not in block or "taken_at" not in block:
            continue
        try:
            found.extend(_media_objects(json.loads(block), code))
        except ValueError:
            continue
    if not found:
        raise InstagramError("couldn't find the post's details on its page")
    media = max(found, key=len)
    user = media.get("user") or media.get("owner") or {}
    username = str(user.get("username") or "")
    caption = media.get("caption") or {}
    children = [c for c in media.get("carousel_media") or [] if isinstance(c, dict)]
    descriptions: list[str] = []
    for item in [media, *children]:
        text = " ".join(str(item.get("accessibility_caption") or "").split())
        if text and text not in descriptions:
            descriptions.append(text)
    image = pick_image((media.get("image_versions2") or {}).get("candidates"))
    if not image and children:
        image = pick_image((children[0].get("image_versions2") or {}).get("candidates"))
    return Post(
        code=code,
        username=username,
        author=" ".join(str(user.get("full_name") or "").split()) or username,
        kind=_kind(media),
        caption=str(caption.get("text") or "") if isinstance(caption, dict) else "",
        taken_at=datetime.fromtimestamp(media["taken_at"], tz=timezone.utc),
        image_description="\n".join(descriptions[:10]),
        image_url=image or str(media.get("display_uri") or ""),
    )


def _image_host_ok(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host.endswith(_IMAGE_HOSTS)


def _first_line(exc: BaseException) -> str:
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return (lines[0] if lines else type(exc).__name__)[:200]


# --- the browser ------------------------------------------------------------- #


class Browser:
    """One headless Chrome tab, used for one check pass."""

    def __init__(self, page, *, pause: tuple[float, float] = PAUSE_SECONDS) -> None:
        self._page = page
        self._pause = pause
        self._loaded = False

    async def _goto(self, url: str):
        if self._loaded:
            await asyncio.sleep(random.uniform(*self._pause))
        self._loaded = True
        try:
            response = await self._page.goto(
                url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001 - Playwright's errors
            raise InstagramError(f"couldn't load {url}: {_first_line(exc)}") from exc
        current = self._page.url
        if "/accounts/login" in current or "/challenge" in current:
            raise InstagramBlocked("Instagram asked this Mac to log in")
        if response is not None and response.status == 429:
            raise InstagramBlocked("Instagram is rate-limiting this Mac (429)")
        return response

    async def profile(self, username: str) -> Profile:
        if not USERNAME.fullmatch(username):
            raise ValueError(f"not an Instagram username: {username!r}")
        await self._goto(profile_url(username))
        try:
            await self._page.wait_for_selector(_POST_LINKS, timeout=POSTS_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - no posts showed up; checked below
            pass
        try:
            hrefs = await self._page.eval_on_selector_all(
                _POST_LINKS, "els => els.map(e => e.getAttribute('href'))"
            )
            title = await self._page.title()
            codes = codes_from_links(hrefs)
            body = "" if codes else await self._page.inner_text("body")
        except Exception as exc:  # noqa: BLE001 - Playwright's errors
            raise InstagramError(
                f"couldn't read @{username}: {_first_line(exc)}"
            ) from exc
        if not codes:
            state = page_state(f"{title}\n{body}")
            if state == "missing":
                raise AccountUnavailable(f"Instagram has no account @{username}")
            if state == "private":
                raise AccountUnavailable(f"@{username} is a private account")
            if state != "empty":
                raise InstagramBlocked(
                    f"Instagram showed a login wall instead of @{username}'s posts"
                )
        return Profile(
            username=username, name=name_from_title(title, username), codes=codes
        )

    async def post(self, code: str) -> Post:
        if not SHORTCODE.fullmatch(code):
            raise ValueError(f"not an Instagram post code: {code!r}")
        response = await self._goto(post_url(code))
        if response is None:
            raise InstagramError("Instagram sent no page")
        if response.status == 404:
            raise InstagramError("the post is gone")
        try:
            page = await response.text()
        except Exception as exc:  # noqa: BLE001 - Playwright's errors
            raise InstagramError(f"couldn't read the post: {_first_line(exc)}") from exc
        return post_from_page(page, code)

    async def image(self, url: str) -> tuple[bytes, str]:
        """An image's bytes and type, from Instagram's image hosts only."""
        if not _image_host_ok(url):
            raise InstagramError("not an Instagram image link")
        try:
            response = await self._page.request.get(
                url, timeout=PAGE_TIMEOUT_MS, max_redirects=0
            )
            data = await response.body() if response.ok else b""
        except Exception as exc:  # noqa: BLE001 - Playwright's errors
            raise InstagramError(
                f"couldn't fetch the image: {_first_line(exc)}"
            ) from exc
        kind = (response.headers.get("content-type") or "").split(";")[0].strip()
        if not response.ok or kind not in _IMAGE_TYPES:
            raise InstagramError(f"image answered {response.status} ({kind or '?'})")
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise InstagramError("image is empty or too large")
        return data, kind


class Instagram:
    """Opens browsing sessions: ``async with Instagram().session() as browser``.

    Uses the installed Google Chrome, else Playwright's own Chromium (``uv run
    playwright install chromium``).
    """

    def __init__(self, *, pause: tuple[float, float] = PAUSE_SECONDS) -> None:
        self._pause = pause

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Browser]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - it's a dependency
            raise InstagramError("Playwright isn't installed") from exc
        async with async_playwright() as playwright:
            browser = await self._launch(playwright)
            try:
                context = await browser.new_context(
                    locale="en-US", viewport={"width": 1280, "height": 900}
                )
                yield Browser(await context.new_page(), pause=self._pause)
            finally:
                await browser.close()

    @staticmethod
    async def _launch(playwright):
        errors = []
        for options in ({"channel": "chrome"}, {}):
            try:
                return await playwright.chromium.launch(headless=True, **options)
            except Exception as exc:  # noqa: BLE001 - try the next browser
                errors.append(_first_line(exc))
        raise InstagramError(
            "couldn't start Chrome: install Google Chrome, or run "
            f"`uv run playwright install chromium` ({errors[-1]})"
        )
