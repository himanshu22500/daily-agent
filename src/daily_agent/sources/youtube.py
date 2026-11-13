"""YouTube channels and playlists as a story source (issue #99).

Everything here reads public YouTube pages; there is no API key:

- ``YouTube.resolve``: a channel link (``/@handle``, ``/channel/UC…``,
  ``/c/…``, ``/user/…``), a playlist link (``?list=…``), or a video link (via
  its channel) -> the public RSS feed of those uploads.
- ``YouTube.feed``: that RSS feed -> its title and latest ~15 videos.
- ``YouTube.page_title``: a channel's or playlist's own page -> its name; this
  checks a source exists while the RSS server is down (issue #103).
- ``YouTube.transcript``: a video's captions, via ``youtube-transcript-api``.
- ``YouTube.list_pages``: a channel's long-form uploads (or a playlist), from
  its public playlist page, 100 videos a page, newest first (issue #124).
- ``YouTube.video``: one video's title, upload date, and description, from its
  watch page (for videos older than the RSS feed reaches).

Only YouTube is ever fetched. Pasted links are parsed and rebuilt on
``https://www.youtube.com``, so a link can't make the server fetch an arbitrary
URL.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlsplit

import httpx

BASE = "https://www.youtube.com"
FEED_URL = BASE + "/feeds/videos.xml"
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{10,64}")
# Handles and legacy /c/, /user/ names (handles may be non-Latin).
_NAME = re.compile(r"[\w.\-]{1,100}")
# Where a channel page states its own id (the first match wins). The page also
# mentions other channels' ids, so a bare "channelId" is never trusted.
_CHANNEL_ID_ON_PAGE = (
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'<meta itemprop="identifier" content="(UC[\w-]{22})"'),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
)  # fmt: skip
_OG_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
# A missing channel's page answers 200 with neither a title nor its id; a
# missing playlist's page answers 200 with one of these alerts.
_MISSING = re.compile(
    r'"text":"(?:The playlist does not exist|This playlist is private)\.'
)
_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
# Skips the EU cookie-consent interstitial.
_COOKIES = {"SOCS": "CAI"}
TIMEOUT_SECONDS = 20.0
TRANSCRIPT_TIMEOUT_SECONDS = 60.0
# Where a YouTube page embeds its data as JSON.
_INITIAL_DATA = "var ytInitialData = "
_PLAYER_RESPONSE = "var ytInitialPlayerResponse = "
_CLIENT_VERSION = re.compile(r'"INNERTUBE_CLIENT_VERSION":"([0-9.]+)"')
BROWSE_URL = BASE + "/youtubei/v1/browse?prettyPrint=false"
# How far back a list is read: 10 pages of about 100 videos.
LIST_PAGES = 10


class BadSourceLink(ValueError):
    """Not a link we can follow (the message tells the reader why)."""


class YouTubeError(RuntimeError):
    """YouTube couldn't be reached or its reply wasn't understood (may pass)."""


class FeedUnavailable(YouTubeError):
    """YouTube's RSS server didn't answer. It has outages that last hours, during
    which it answers 404 even for channels that exist (issue #103)."""


class NoCaptions(Exception):
    """The video has no captions to read (disabled, none yet, or unplayable)."""


class VideoUnavailable(Exception):
    """The video is private or removed (this won't pass)."""


@dataclass(frozen=True)
class SourceRef:
    id: str  # "channel:UC…" or "playlist:PL…"
    feed_url: str
    url: str  # the page to link to


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    published_at: datetime
    description: str = ""
    channel_id: str = ""
    channel: str = ""
    short: bool = False


@dataclass(frozen=True)
class ChannelFeed:
    title: str
    videos: list[Video]


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str = ""
    generated: bool = False


def watch_url(video_id: str) -> str:
    return f"{BASE}/watch?v={video_id}"


def embed_url(video_id: str) -> str:
    return f"https://www.youtube-nocookie.com/embed/{video_id}?rel=0"


def thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def channel_ref(channel_id: str) -> SourceRef:
    return SourceRef(
        id=f"channel:{channel_id}",
        feed_url=f"{FEED_URL}?channel_id={channel_id}",
        url=f"{BASE}/channel/{channel_id}",
    )


def uploads_list(source_id: str) -> str:
    """The list a followed source is walked back through (issue #124).

    A channel's long-form uploads ("UULF" + its id, newest first, no Shorts),
    or a followed playlist itself. Raises ``ValueError`` for anything else.
    """
    kind, _, ident = source_id.partition(":")
    if kind == "channel" and CHANNEL_ID.fullmatch(ident):
        return "UULF" + ident[2:]
    if kind == "playlist" and _PLAYLIST_ID.fullmatch(ident):
        return ident
    raise ValueError(f"not a followed source id: {source_id!r}")


def playlist_ref(playlist_id: str) -> SourceRef:
    return SourceRef(
        id=f"playlist:{playlist_id}",
        feed_url=f"{FEED_URL}?playlist_id={playlist_id}",
        url=f"{BASE}/playlist?list={playlist_id}",
    )


def parse_link(raw: str) -> SourceRef | str:
    """A followable source, or the YouTube page that names one.

    Returns a ``SourceRef`` when the link already identifies the feed (channel
    id, playlist), otherwise the ``https://www.youtube.com`` URL to look the
    channel up on: a handle/legacy channel page, or ``video:<id>`` for a video
    link (followed via its channel). Raises ``BadSourceLink`` for anything else.
    """
    text = (raw or "").strip()
    if not text:
        raise BadSourceLink("Paste a YouTube channel link, like youtube.com/@name.")
    if text.startswith("@"):
        text = f"{BASE}/{text}"
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host in _SHORT_HOSTS:
        video_id = parts.path.strip("/")
        if VIDEO_ID.fullmatch(video_id):
            return f"video:{video_id}"
        raise BadSourceLink("That youtu.be link has no video id.")
    if host not in _HOSTS:
        raise BadSourceLink("Only YouTube links can be followed.")
    query = parse_qs(parts.query)
    segments = [s for s in parts.path.split("/") if s]
    head = segments[0] if segments else ""
    if query.get("list"):
        playlist_id = query["list"][0]
        if playlist_id.startswith(("RD", "WL", "LL")):
            raise BadSourceLink(
                "That's an automatic mix or a personal list; it has no public feed."
            )
        if not _PLAYLIST_ID.fullmatch(playlist_id):
            raise BadSourceLink("That playlist id doesn't look right.")
        return playlist_ref(playlist_id)
    if head == "channel" and len(segments) > 1 and CHANNEL_ID.fullmatch(segments[1]):
        return channel_ref(segments[1])
    if head.startswith("@") and _NAME.fullmatch(head[1:]):
        return f"{BASE}/{quote(head)}"
    if head in ("c", "user") and len(segments) > 1 and _NAME.fullmatch(segments[1]):
        return f"{BASE}/{head}/{quote(segments[1])}"
    if head == "watch" and query.get("v") and VIDEO_ID.fullmatch(query["v"][0]):
        return f"video:{query['v'][0]}"
    if head in ("shorts", "live", "embed") and len(segments) > 1:
        if VIDEO_ID.fullmatch(segments[1]):
            return f"video:{segments[1]}"
    raise BadSourceLink(
        "That isn't a YouTube channel or playlist link. "
        "Try the channel's page, like youtube.com/@name."
    )


def channel_id_from_page(html: str) -> str | None:
    for pattern in _CHANNEL_ID_ON_PAGE:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def title_from_page(page: str) -> str:
    match = _OG_TITLE.search(page)
    return " ".join(html.unescape(match.group(1)).split()) if match else ""


def _when(value: str | None) -> datetime:
    try:
        moment = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_feed(data: bytes) -> ChannelFeed:
    """A YouTube uploads/playlist RSS (Atom) feed -> title and videos."""
    lowered = data[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise YouTubeError("unexpected feed format")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise YouTubeError(f"unreadable feed: {exc}") from exc
    feed_title = (root.findtext("a:title", default="", namespaces=_NS)).strip()
    videos: list[Video] = []
    for entry in root.findall("a:entry", _NS):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=_NS)).strip()
        if not VIDEO_ID.fullmatch(video_id):
            continue
        link = entry.find("a:link", _NS)
        href = link.get("href", "") if link is not None else ""
        author = entry.findtext("a:author/a:name", default="", namespaces=_NS)
        videos.append(
            Video(
                video_id=video_id,
                title=" ".join(
                    entry.findtext("a:title", default="", namespaces=_NS).split()
                ),
                published_at=_when(
                    entry.findtext("a:published", default="", namespaces=_NS)
                ),
                description=(
                    entry.findtext(
                        "media:group/media:description", default="", namespaces=_NS
                    )
                ).strip(),
                channel_id=(
                    entry.findtext("yt:channelId", default="", namespaces=_NS)
                ).strip(),
                channel=author.strip() or feed_title,
                short="/shorts/" in href,
            )
        )
    return ChannelFeed(title=feed_title, videos=videos)


# --- older videos (issue #124) ------------------------------------------------ #


def embedded_json(page: str, marker: str) -> dict | None:
    """The JSON object a YouTube page assigns after ``marker`` (None if absent)."""
    start = page.find(marker)
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(page, start + len(marker))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _dig(data, *keys) -> dict:
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data if isinstance(data, dict) else {}


def _listed_video(item: dict, legacy: bool) -> str:
    """A list item's video id ("" for Shorts, playlists, and anything else)."""
    if legacy:  # the playlist page's older layout
        video_id = item.get("videoId")
        command = _dig(item, "navigationEndpoint")
    else:
        if item.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
            return ""
        video_id = item.get("contentId")
        command = _dig(item, "rendererContext", "commandContext", "onTap")
        command = _dig(command, "innertubeCommand")
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        return ""
    url = _dig(command, "commandMetadata", "webCommandMetadata").get("url")
    if "reelWatchEndpoint" in command or str(url or "").startswith("/shorts/"):
        return ""
    return video_id


def parse_list(data: dict) -> tuple[list[str], str]:
    """A playlist page's videos (in list order) and the next page's token.

    ``data`` is the page's ``ytInitialData`` or a ``browse`` continuation reply;
    the token is "" at the end of the list.
    """
    videos: list[str] = []
    token = ""
    stack: list = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
            continue
        if not isinstance(node, dict):
            continue
        for name, legacy in (
            ("lockupViewModel", False),
            ("playlistVideoRenderer", True),
        ):
            if isinstance(node.get(name), dict):
                video_id = _listed_video(node[name], legacy)
                if video_id:
                    videos.append(video_id)
                break
        else:
            command = node.get("continuationCommand")
            if not token and isinstance(command, dict):
                if isinstance(command.get("token"), str):
                    token = command["token"]
            stack.extend(reversed(list(node.values())))
    return list(dict.fromkeys(videos)), token


def parse_watch_page(page: str, video_id: str) -> Video:
    """A watch page -> the video's title, upload date, description, channel.

    Raises ``VideoUnavailable`` when YouTube says the video is gone or private,
    and ``YouTubeError`` when the page can't be read (that may pass).
    """
    player = embedded_json(page, _PLAYER_RESPONSE)
    if player is None:
        raise YouTubeError("couldn't read the video's page")
    status = _dig(player, "playabilityStatus")
    if status.get("status") == "ERROR":
        raise VideoUnavailable(str(status.get("reason") or "unavailable")[:200])
    details = _dig(player, "videoDetails")
    micro = _dig(player, "microformat", "playerMicroformatRenderer")
    stamp = str(micro.get("publishDate") or micro.get("uploadDate") or "")
    if details.get("videoId") != video_id or not stamp:
        raise YouTubeError(
            str(status.get("reason") or "the video's page has no upload date")[:200]
        )
    try:
        published = datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise YouTubeError(f"unreadable upload date {stamp[:40]!r}") from exc
    if not published.tzinfo:
        published = published.replace(tzinfo=timezone.utc)
    channel_id = str(details.get("channelId") or "")
    return Video(
        video_id=video_id,
        title=" ".join(str(details.get("title") or "").split()),
        published_at=published,
        description=str(details.get("shortDescription") or "").strip(),
        channel_id=channel_id if CHANNEL_ID.fullmatch(channel_id) else "",
        channel=str(details.get("author") or micro.get("ownerChannelName") or ""),
    )


# --- captions ---------------------------------------------------------------- #

_ENGLISH = ("en", "en-US", "en-GB", "en-CA", "en-AU")


def read_transcript(video_id: str, api=None) -> Transcript:
    """Captions for ``video_id`` (blocking; network). English first, else any.

    Raises ``NoCaptions`` when the video has none (or can't be played), and
    ``YouTubeError`` for anything that may pass (blocked, network, parsing).
    """
    import requests
    import youtube_transcript_api as yta

    no_captions = (
        yta.TranscriptsDisabled,
        yta.NoTranscriptFound,
        yta.VideoUnavailable,
        yta.VideoUnplayable,
        yta.AgeRestricted,
        yta.InvalidVideoId,
    )
    client = api or yta.YouTubeTranscriptApi()
    try:
        listing = client.list(video_id)
        tracks = list(listing)
        if not tracks:
            raise NoCaptions("no captions")
        try:
            chosen = listing.find_transcript(_ENGLISH)
        except yta.NoTranscriptFound:
            chosen = next((t for t in tracks if not t.is_generated), tracks[0])
        fetched = chosen.fetch()
    except no_captions as exc:
        raise NoCaptions(_first_line(exc)) from exc
    except (yta.YouTubeTranscriptApiException, requests.RequestException) as exc:
        raise YouTubeError(_first_line(exc)) from exc
    text = " ".join(" ".join(s.text.split()) for s in fetched if s.text.strip())
    if not text:
        raise NoCaptions("empty captions")
    return Transcript(
        text=text,
        language=getattr(chosen, "language_code", ""),
        generated=bool(getattr(chosen, "is_generated", False)),
    )


def _first_line(exc: BaseException) -> str:
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return (lines[0] if lines else type(exc).__name__)[:200]


# --- the client -------------------------------------------------------------- #


class YouTube:
    """Async access to YouTube's public pages, feeds, and captions.

    ``transport`` (an ``httpx`` transport) and ``transcripts`` (``video_id ->
    Transcript``, blocking) are injectable so tests never touch the network.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        transcripts: Callable[[str], Transcript] | None = None,
    ) -> None:
        self._transport = transport
        self._transcripts = transcripts or read_transcript

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=_HEADERS,
            cookies=_COOKIES,
            follow_redirects=True,
            timeout=TIMEOUT_SECONDS,
            transport=self._transport,
        )

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        try:
            return await client.get(url)
        except httpx.HTTPError as exc:
            raise YouTubeError(f"couldn't reach YouTube: {_first_line(exc)}") from exc

    async def resolve(self, link: str) -> SourceRef:
        """The feed behind a pasted link. Raises ``BadSourceLink``/``YouTubeError``."""
        target = parse_link(link)
        if isinstance(target, SourceRef):
            return target
        async with self._client() as client:
            if target.startswith("video:"):
                target = await self._video_channel(client, target[6:])
                if isinstance(target, SourceRef):
                    return target
            page = await self._get(client, target)
            if page.status_code == 404:
                raise BadSourceLink("YouTube has no channel at that link.")
            if page.status_code != 200:
                raise YouTubeError(f"YouTube answered {page.status_code}")
            channel_id = channel_id_from_page(page.text)
        if not channel_id:
            raise YouTubeError("couldn't find the channel id on its page")
        return channel_ref(channel_id)

    async def _video_channel(
        self, client: httpx.AsyncClient, video_id: str
    ) -> SourceRef | str:
        """A video's channel (or its page), via YouTube's public oEmbed endpoint."""
        oembed = f"{BASE}/oembed?format=json&url={quote(watch_url(video_id), safe='')}"
        reply = await self._get(client, oembed)
        if reply.status_code in (401, 403, 404):
            raise BadSourceLink("That video is private or doesn't exist.")
        if reply.status_code != 200:
            raise YouTubeError(f"YouTube answered {reply.status_code}")
        try:
            author_url = str(reply.json().get("author_url") or "")
        except ValueError as exc:
            raise YouTubeError("unreadable oEmbed reply") from exc
        try:
            target = parse_link(author_url)
        except BadSourceLink as exc:
            raise YouTubeError("couldn't find the video's channel") from exc
        if isinstance(target, str) and target.startswith("video:"):
            raise YouTubeError("couldn't find the video's channel")
        return target

    async def feed(self, feed_url: str) -> ChannelFeed:
        if not feed_url.startswith(FEED_URL + "?"):
            raise ValueError(f"not a YouTube feed URL: {feed_url!r}")
        async with self._client() as client:
            reply = await self._get(client, feed_url)
        if reply.status_code != 200:
            # Not BadSourceLink even for a 404: during YouTube's feed outages
            # every channel's feed answers 404.
            raise FeedUnavailable(
                f"YouTube's video feed isn't answering ({reply.status_code})"
            )
        return parse_feed(reply.content)

    async def page_title(self, ref: SourceRef) -> str:
        """The source's name from its own page ("" if the page doesn't say).

        Raises ``BadSourceLink`` when YouTube has no such public channel or
        playlist.
        """
        async with self._client() as client:
            page = await self._get(client, ref.url)
        if page.status_code != 200 and page.status_code != 404:
            raise YouTubeError(f"YouTube answered {page.status_code}")
        title = title_from_page(page.text) if page.status_code == 200 else ""
        found = title or channel_id_from_page(page.text)
        if page.status_code == 404 or not found or _MISSING.search(page.text):
            raise BadSourceLink(
                "YouTube has no public channel or playlist at that link."
            )
        return title

    async def list_pages(
        self, list_id: str, *, pages: int = LIST_PAGES
    ) -> AsyncIterator[list[str]]:
        """A public list's video ids, a page (about 100) at a time, from the top.

        Stops after ``pages`` pages or at the end of the list. Raises
        ``BadSourceLink`` when the list doesn't exist and ``YouTubeError`` when
        YouTube can't be reached or read. Close it (``contextlib.aclosing``)
        when stopping early.
        """
        if not _PLAYLIST_ID.fullmatch(list_id):
            raise ValueError(f"not a playlist id: {list_id!r}")
        async with self._client() as client:
            reply = await self._get(client, f"{BASE}/playlist?list={list_id}")
            if reply.status_code != 200:
                raise YouTubeError(f"YouTube answered {reply.status_code}")
            data = embedded_json(reply.text, _INITIAL_DATA)
            if data is None:
                raise YouTubeError("couldn't read the video list")
            videos, token = parse_list(data)
            if not videos and _MISSING.search(reply.text):
                raise BadSourceLink("YouTube has no public list of these videos.")
            version = _CLIENT_VERSION.search(reply.text)
            yield videos
            for _ in range(pages - 1):
                if not token or version is None:
                    return
                body = {
                    "context": {
                        "client": {
                            "clientName": "WEB",
                            "clientVersion": version.group(1),
                            "hl": "en",
                            "gl": "US",
                        }
                    },
                    "continuation": token,
                }
                try:
                    reply = await client.post(BROWSE_URL, json=body)
                except httpx.HTTPError as exc:
                    raise YouTubeError(
                        f"couldn't reach YouTube: {_first_line(exc)}"
                    ) from exc
                if reply.status_code != 200:
                    raise YouTubeError(f"YouTube answered {reply.status_code}")
                try:
                    data = reply.json()
                except ValueError as exc:
                    raise YouTubeError("unreadable video list page") from exc
                if not isinstance(data, dict):
                    raise YouTubeError("unreadable video list page")
                videos, token = parse_list(data)
                yield videos

    async def video(self, video_id: str) -> Video:
        """One video's details from its watch page (see ``parse_watch_page``)."""
        if not VIDEO_ID.fullmatch(video_id):
            raise VideoUnavailable("not a video id")
        async with self._client() as client:
            reply = await self._get(client, watch_url(video_id))
        if reply.status_code != 200:
            raise YouTubeError(f"YouTube answered {reply.status_code}")
        return parse_watch_page(reply.text, video_id)

    async def transcript(self, video_id: str) -> Transcript:
        if not VIDEO_ID.fullmatch(video_id):
            raise NoCaptions("not a video id")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._transcripts, video_id),
                TRANSCRIPT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise YouTubeError("captions request timed out") from exc
