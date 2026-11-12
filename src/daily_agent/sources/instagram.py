"""Instagram Business/Creator accounts as a story source (issue #107).

Only the official Instagram Graph API is used ("Instagram API with Facebook
Login"), never scraping:

- ``Instagram.account``: *Business Discovery* reads another professional
  account's public profile and latest posts (caption, type, permalink, time,
  image URLs, carousel slides) through the maintainer's own connected account.
  Personal and private accounts can't be read this way.
- ``Instagram.images``: a post's images, downloaded once because Instagram's
  CDN URLs expire. Only Instagram/Facebook CDN hosts are fetched, over https,
  without the token, and with a size cap.
- ``exchange_token``/``linked_accounts``: the one-time ``daily-agent instagram
  connect`` flow. A short-lived user token becomes a long-lived one, and that
  becomes the linked Facebook Page's token, which doesn't expire.

The token is sent in an ``Authorization`` header, never in a URL, so it can't
end up in logs or error messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .youtube import BadSourceLink

__all__ = ["BadSourceLink"]  # re-exported: callers catch it for both platforms

GRAPH = "https://graph.facebook.com/v25.0"
WEB = "https://www.instagram.com"
SCOPES = (
    "instagram_basic",
    "instagram_manage_insights",
    "pages_read_engagement",
    "pages_show_list",
)
_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "instagr.am",
    "www.instagr.am",
}
USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
SHORTCODE = re.compile(r"[A-Za-z0-9_-]{5,64}")
# First path segments that are Instagram pages, not accounts.
_NOT_ACCOUNTS = {
    "about",
    "accounts",
    "developer",
    "direct",
    "explore",
    "legal",
    "s",
    "stories",
    "web",
}
_POST_PATHS = {"p", "reel", "reels", "tv"}
_CDN_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")
_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")

POSTS_PER_CHECK = 15
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 20.0
_MEDIA_FIELDS = (
    "id,shortcode,caption,media_type,media_product_type,media_url,"
    "thumbnail_url,permalink,timestamp,alt_text,"
    "children{media_type,media_url,thumbnail_url,alt_text}"
)
# Graph API error codes.
_AUTH_CODES = {102, 190, 10, 200}
_THROTTLE_CODES = {4, 17, 32, 613, 80002}
_NO_ACCOUNT_CODES = {110}


class InstagramError(RuntimeError):
    """Instagram couldn't be reached or its reply wasn't understood (may pass)."""


class InstagramAuthError(InstagramError):
    """The saved token was rejected; run ``daily-agent instagram connect``."""


class MediaExpired(InstagramError):
    """A post's image URLs have expired; re-read the account for fresh ones."""


@dataclass(frozen=True)
class Account:
    id: str
    username: str
    name: str


@dataclass(frozen=True)
class Post:
    shortcode: str
    username: str
    caption: str
    media_type: str  # IMAGE, VIDEO, CAROUSEL_ALBUM
    product_type: str  # FEED, REELS, ...
    permalink: str
    published_at: datetime
    image_urls: tuple[str, ...] = ()
    alt_texts: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        """``carousel``, ``reel``, ``video`` or ``photo``."""
        if self.media_type == "CAROUSEL_ALBUM":
            return "carousel"
        if self.product_type == "REELS":
            return "reel"
        if self.media_type == "VIDEO":
            return "video"
        return "photo"


@dataclass(frozen=True)
class AccountFeed:
    account: Account
    posts: list[Post]


@dataclass(frozen=True)
class PostImage:
    content_type: str
    data: bytes


@dataclass(frozen=True)
class LinkedAccount:
    """An Instagram professional account linked to one of the user's Pages."""

    page_name: str
    page_token: str
    ig_user_id: str
    ig_username: str


def profile_url(username: str) -> str:
    return f"{WEB}/{username}/"


def post_url(shortcode: str) -> str:
    return f"{WEB}/p/{shortcode}/"


def _split(raw: str):
    text = (raw or "").strip()
    if "://" not in text:
        text = "https://" + text
    return urlsplit(text)


def is_instagram_link(raw: str) -> bool:
    try:
        return (_split(raw).hostname or "").lower() in _HOSTS
    except ValueError:
        return False


def parse_username(raw: str) -> str:
    """The account a pasted profile link names. Raises ``BadSourceLink``."""
    if not (raw or "").strip():
        raise BadSourceLink("Paste an Instagram profile link, like instagram.com/name.")
    try:
        parts = _split(raw)
    except ValueError as exc:
        raise BadSourceLink("That link doesn't look right.") from exc
    if (parts.hostname or "").lower() not in _HOSTS:
        raise BadSourceLink("That isn't an Instagram link.")
    segments = [s for s in parts.path.split("/") if s]
    head = segments[0] if segments else ""
    if head.lower() in _POST_PATHS:
        raise BadSourceLink(
            "That's a link to a post. Paste the account's profile link, "
            "like instagram.com/name."
        )
    if not USERNAME.fullmatch(head) or head.lower() in _NOT_ACCOUNTS:
        raise BadSourceLink(
            "That isn't an Instagram profile link. Try instagram.com/name."
        )
    return head.lower()


def shortcode_from_permalink(url: str) -> str:
    """``https://www.instagram.com/p/<code>/`` (or /reel/, /tv/) -> ``<code>``."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[0] in _POST_PATHS:
        code = segments[1]
        return code if SHORTCODE.fullmatch(code) else ""
    return ""


def is_cdn_url(url: str) -> bool:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host.endswith(_CDN_SUFFIXES)


def _when(value: object) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _media_images(media: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Image URLs (a video's cover frame, a carousel's slides) and alt texts."""
    if media.get("media_type") == "CAROUSEL_ALBUM":
        children = (media.get("children") or {}).get("data") or []
    else:
        children = [media]
    urls: list[str] = []
    alts: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        if child.get("media_type") == "VIDEO":
            url = child.get("thumbnail_url")
        else:
            url = child.get("media_url")
        if isinstance(url, str) and is_cdn_url(url):
            urls.append(url)
        alt = child.get("alt_text")
        if isinstance(alt, str) and alt.strip():
            alts.append(" ".join(alt.split()))
        if len(urls) == MAX_IMAGES:
            break
    return tuple(urls), tuple(alts)


def parse_discovery(data: dict, username: str) -> AccountFeed:
    """A Business Discovery reply -> the account and its latest posts."""
    found = data.get("business_discovery") if isinstance(data, dict) else None
    if not isinstance(found, dict) or not found.get("id"):
        raise InstagramError("unexpected reply from Instagram")
    handle = str(found.get("username") or "")
    if not USERNAME.fullmatch(handle):
        handle = username
    account = Account(
        id=str(found["id"]),
        username=handle.lower(),
        name=" ".join(str(found.get("name") or "").split()) or handle,
    )
    posts: list[Post] = []
    for media in (found.get("media") or {}).get("data") or []:
        if not isinstance(media, dict):
            continue
        product = str(media.get("media_product_type") or "FEED")
        if product in ("AD", "STORY"):
            continue
        permalink = str(media.get("permalink") or "")
        code = str(media.get("shortcode") or "") or shortcode_from_permalink(permalink)
        if not SHORTCODE.fullmatch(code):
            continue
        urls, alts = _media_images(media)
        posts.append(
            Post(
                shortcode=code,
                username=account.username,
                caption=str(media.get("caption") or "").strip(),
                media_type=str(media.get("media_type") or "IMAGE"),
                product_type=product,
                permalink=permalink
                if permalink.startswith(WEB + "/")
                else post_url(code),
                published_at=_when(media.get("timestamp")),
                image_urls=urls,
                alt_texts=alts,
            )
        )
    return AccountFeed(account=account, posts=posts)


def graph_error(
    reply: httpx.Response, username: str = ""
) -> InstagramError | BadSourceLink:
    """The exception for a failed Graph API reply."""
    try:
        error = reply.json().get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    code = error.get("code")
    message = " ".join(str(error.get("message") or "").split())[:150]
    if code in _NO_ACCOUNT_CODES or error.get("error_subcode") == 2207013:
        who = f"@{username}" if username else "that name"
        return BadSourceLink(
            f"Instagram has no public Business or Creator account named {who}. "
            "Personal and private accounts can't be followed."
        )
    if code in _AUTH_CODES or reply.status_code == 401:
        return InstagramAuthError(
            f"Instagram rejected the saved token ({message or reply.status_code}). "
            "Run `daily-agent instagram connect` again."
        )
    if code in _THROTTLE_CODES:
        return InstagramError("Instagram's rate limit was reached; try again later")
    return InstagramError(
        f"Instagram answered {reply.status_code}: {message or 'error'}"
    )


def _first_line(exc: BaseException) -> str:
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return (lines[0] if lines else type(exc).__name__)[:200]


async def _graph_get(
    path: str,
    params: dict[str, str],
    *,
    token: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
    username: str = "",
) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        headers=headers, timeout=TIMEOUT_SECONDS, transport=transport
    ) as client:
        try:
            reply = await client.get(f"{GRAPH}/{path}", params=params)
        except httpx.HTTPError as exc:
            raise InstagramError(
                f"couldn't reach Instagram: {_first_line(exc)}"
            ) from exc
    if reply.status_code != 200:
        raise graph_error(reply, username)
    try:
        data = reply.json()
    except ValueError as exc:
        raise InstagramError("unreadable reply from Instagram") from exc
    if not isinstance(data, dict):
        raise InstagramError("unexpected reply from Instagram")
    return data


class Instagram:
    """Graph API access for one connected account (``token`` + its IG user id).

    ``transport`` (an ``httpx`` transport) is injectable so tests never touch
    the network.
    """

    def __init__(
        self,
        token: str,
        ig_user_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token or not ig_user_id:
            raise ValueError("Instagram needs a token and the connected IG user id")
        self._token = token
        self._user_id = ig_user_id
        self._transport = transport

    async def account(self, username: str) -> AccountFeed:
        """Profile and latest posts of a professional account.

        Raises ``BadSourceLink`` (no such public professional account),
        ``InstagramAuthError`` (reconnect), or ``InstagramError`` (try later).
        """
        if not USERNAME.fullmatch(username):
            raise BadSourceLink("That isn't an Instagram username.")
        fields = (
            f"business_discovery.username({username})"
            f"{{id,username,name,media.limit({POSTS_PER_CHECK}){{{_MEDIA_FIELDS}}}}}"
        )
        data = await _graph_get(
            self._user_id,
            {"fields": fields},
            token=self._token,
            transport=self._transport,
            username=username,
        )
        return parse_discovery(data, username)

    async def images(self, post: Post) -> list[PostImage]:
        """Download the post's images (at most ``MAX_IMAGES``).

        Raises ``MediaExpired`` when Instagram refuses the first URL (signed
        URLs expire); other images that fail are just left out.
        """
        out: list[PostImage] = []
        # A separate client without the token: CDN hosts never see it.
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            for n, url in enumerate(post.image_urls[:MAX_IMAGES]):
                if not is_cdn_url(url):
                    continue
                try:
                    image = await self._download(client, url)
                except MediaExpired:
                    if n == 0:
                        raise
                    continue
                except InstagramError:
                    continue
                if image is not None:
                    out.append(image)
        return out

    async def _download(self, client: httpx.AsyncClient, url: str) -> PostImage | None:
        try:
            async with client.stream("GET", url) as reply:
                if reply.status_code in (403, 404, 410):
                    raise MediaExpired(f"image URL answered {reply.status_code}")
                if reply.status_code != 200:
                    raise InstagramError(f"image answered {reply.status_code}")
                kind = reply.headers.get("content-type", "").split(";")[0].strip()
                if kind not in _IMAGE_TYPES:
                    return None
                declared = reply.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                    return None
                data = bytearray()
                async for chunk in reply.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        return None
        except httpx.HTTPError as exc:
            raise InstagramError(f"image download failed: {_first_line(exc)}") from exc
        return PostImage(content_type=kind, data=bytes(data)) if data else None


# --- connecting (one-time) ---------------------------------------------------- #


async def exchange_token(
    app_id: str,
    app_secret: str,
    short_token: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """A short-lived user token -> a long-lived (60-day) one."""
    data = await _graph_get(
        "oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
        transport=transport,
    )
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise InstagramError("Facebook returned no long-lived token")
    return token


async def linked_accounts(
    user_token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[LinkedAccount]:
    """The user's Pages that have an Instagram professional account linked.

    Each Page token, made from a long-lived user token, doesn't expire.
    """
    data = await _graph_get(
        "me/accounts",
        {
            "fields": "name,access_token,instagram_business_account{id,username}",
            "limit": "100",
        },
        token=user_token,
        transport=transport,
    )
    out: list[LinkedAccount] = []
    for page in data.get("data") or []:
        if not isinstance(page, dict):
            continue
        ig = page.get("instagram_business_account") or {}
        if not (ig.get("id") and page.get("access_token")):
            continue
        out.append(
            LinkedAccount(
                page_name=str(page.get("name") or ""),
                page_token=str(page["access_token"]),
                ig_user_id=str(ig["id"]),
                ig_username=str(ig.get("username") or ""),
            )
        )
    return out
