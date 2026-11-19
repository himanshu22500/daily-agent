"""YouTube adapter (issue #99): link parsing, feeds, captions. All offline."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import requests
import youtube_transcript_api as yta

from daily_agent.sources.youtube import (
    BROWSE_URL,
    BadSourceLink,
    FeedUnavailable,
    NoCaptions,
    SourceRef,
    Transcript,
    VideoUnavailable,
    YouTube,
    YouTubeError,
    channel_id_from_page,
    channel_ref,
    parse_feed,
    parse_link,
    parse_list,
    parse_watch_page,
    playlist_ref,
    read_transcript,
    title_from_page,
    uploads_list,
)

CHANNEL = "UCsBjURrPoezykLs9EqgamOA"
OTHER = "UC2Xd-TjJByJyK2w1zNwY0zQ"

FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <title>Fireship</title>
 <entry>
  <yt:videoId>1FurWDiK9A4</yt:videoId>
  <yt:channelId>{CHANNEL}</yt:channelId>
  <title>Did   Anthropic just save its IPO?</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=1FurWDiK9A4"/>
  <author><name>Fireship</name></author>
  <published>2026-11-10T18:19:55+00:00</published>
  <media:group><media:description>Sponsor &amp; links</media:description></media:group>
 </entry>
 <entry>
  <yt:videoId>shortAAAAAA</yt:videoId>
  <yt:channelId>{CHANNEL}</yt:channelId>
  <title>A short</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/shortAAAAAA"/>
  <published>2026-11-09T10:00:00+00:00</published>
 </entry>
 <entry>
  <yt:videoId>not-an-id</yt:videoId>
  <title>Broken</title>
 </entry>
</feed>""".encode()

CHANNEL_PAGE = f"""<html><head>
<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL}">
</head><body><script>var x = {{"channelId":"{OTHER}","externalId":"{CHANNEL}"}};</script>
</body></html>"""


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://www.youtube.com/@Fireship", "https://www.youtube.com/%40Fireship"),
        ("youtube.com/@Fireship/videos", "https://www.youtube.com/%40Fireship"),
        ("@Fireship", "https://www.youtube.com/%40Fireship"),
        ("https://m.youtube.com/c/Fireship", "https://www.youtube.com/c/Fireship"),
        (
            "https://www.youtube.com/user/someone",
            "https://www.youtube.com/user/someone",
        ),
        ("https://www.youtube.com/watch?v=1FurWDiK9A4", "video:1FurWDiK9A4"),
        ("https://youtu.be/1FurWDiK9A4?t=30", "video:1FurWDiK9A4"),
        ("https://www.youtube.com/shorts/1FurWDiK9A4", "video:1FurWDiK9A4"),
    ],
)
def test_links_that_need_a_page_lookup(link, expected):
    assert parse_link(link) == expected


def test_links_that_name_the_feed_directly():
    ref = parse_link(f"https://www.youtube.com/channel/{CHANNEL}/videos")
    assert ref == channel_ref(CHANNEL)
    assert ref.feed_url.endswith(f"?channel_id={CHANNEL}")
    playlist = parse_link(
        "https://www.youtube.com/watch?v=1FurWDiK9A4&list=PLabcdefghijkl"
    )
    assert isinstance(playlist, SourceRef)
    assert playlist.id == "playlist:PLabcdefghijkl"
    assert playlist.feed_url.endswith("?playlist_id=PLabcdefghijkl")


@pytest.mark.parametrize(
    "link",
    [
        "",
        "https://example.com/@Fireship",
        "https://www.youtube.com.evil.test/@x",
        "http://127.0.0.1:8765/",
        "https://www.youtube.com/",
        "https://www.youtube.com/results?search_query=x",
        "https://www.youtube.com/watch?v=1FurWDiK9A4&list=RDabcdefghijkl",
        "https://www.youtube.com/@a b",
    ],
)
def test_other_links_are_refused(link):
    with pytest.raises(BadSourceLink):
        parse_link(link)


def test_only_the_first_path_segment_is_used():
    # Whatever follows the handle is dropped; the page is rebuilt on youtube.com.
    assert parse_link("https://www.youtube.com/@bad/../../x?y") == (
        "https://www.youtube.com/%40bad"
    )


def test_channel_id_comes_from_the_page_itself_not_other_channels():
    assert channel_id_from_page(CHANNEL_PAGE) == CHANNEL
    assert channel_id_from_page(f'"externalId":"{CHANNEL}"') == CHANNEL
    assert channel_id_from_page(f'"channelId":"{OTHER}"') is None


def test_parse_feed_reads_videos_and_flags_shorts():
    feed = parse_feed(FEED)
    assert feed.title == "Fireship"
    first, short = feed.videos
    assert first.video_id == "1FurWDiK9A4"
    assert first.title == "Did Anthropic just save its IPO?"
    assert first.published_at == datetime(2026, 11, 10, 18, 19, 55, tzinfo=timezone.utc)
    assert first.description == "Sponsor & links"
    assert (first.channel_id, first.channel, first.short) == (
        CHANNEL,
        "Fireship",
        False,
    )
    assert short.short and short.channel == "Fireship"


def test_parse_feed_refuses_entities_and_garbage():
    with pytest.raises(YouTubeError):
        parse_feed(b'<!DOCTYPE x [<!ENTITY a "b">]><feed/>')
    with pytest.raises(YouTubeError):
        parse_feed(b"<feed")


def _youtube(routes: dict[str, httpx.Response], seen: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        assert request.url.host == "www.youtube.com"
        for prefix, response in routes.items():
            if url.startswith(prefix):
                return response
        return httpx.Response(404)

    return YouTube(transport=httpx.MockTransport(handler))


async def test_resolve_handle_via_its_page():
    yt = _youtube(
        {"https://www.youtube.com/%40Fireship": httpx.Response(200, text=CHANNEL_PAGE)}
    )
    assert await yt.resolve("youtube.com/@Fireship") == channel_ref(CHANNEL)


async def test_resolve_channel_link_needs_no_network():
    seen: list[str] = []
    yt = _youtube({}, seen)
    assert await yt.resolve(
        f"https://www.youtube.com/channel/{CHANNEL}"
    ) == channel_ref(CHANNEL)
    assert seen == []


async def test_resolve_video_link_via_its_channel():
    yt = _youtube(
        {
            "https://www.youtube.com/oembed": httpx.Response(
                200, json={"author_url": "https://www.youtube.com/@Fireship"}
            ),
            "https://www.youtube.com/%40Fireship": httpx.Response(
                200, text=CHANNEL_PAGE
            ),
        }
    )
    assert await yt.resolve("https://youtu.be/1FurWDiK9A4") == channel_ref(CHANNEL)


async def test_resolve_failures():
    with pytest.raises(BadSourceLink):
        await _youtube({}).resolve("youtube.com/@nobody")
    with pytest.raises(YouTubeError):
        await _youtube(
            {"https://www.youtube.com/%40x": httpx.Response(200, text="<html></html>")}
        ).resolve("@x")
    with pytest.raises(YouTubeError):
        await _youtube({"https://www.youtube.com/%40x": httpx.Response(503)}).resolve(
            "@x"
        )
    with pytest.raises(BadSourceLink):
        await _youtube({}).resolve("https://youtu.be/1FurWDiK9A4")  # oEmbed 404

    def down(request):
        raise httpx.ConnectError("offline")

    with pytest.raises(YouTubeError):
        await YouTube(transport=httpx.MockTransport(down)).resolve("@x")


async def test_feed_fetch():
    ref = channel_ref(CHANNEL)
    yt = _youtube({ref.feed_url: httpx.Response(200, content=FEED)})
    assert (await yt.feed(ref.feed_url)).title == "Fireship"
    # During YouTube's feed outages every feed answers 404 (issue #103): that's
    # "try later", never "bad link".
    with pytest.raises(FeedUnavailable, match="isn't answering"):
        await _youtube({}).feed(ref.feed_url)
    assert issubclass(FeedUnavailable, YouTubeError)
    with pytest.raises(ValueError):
        await yt.feed("https://example.com/feeds/videos.xml")


def test_title_from_page():
    page = '<meta property="og:title" content="Tom &amp; Jerry\n Channel">'
    assert title_from_page(page) == "Tom & Jerry Channel"
    assert title_from_page("<html></html>") == ""


async def test_page_title_confirms_the_source_exists():
    channel, playlist = channel_ref(CHANNEL), playlist_ref("PLabcdefghij")
    titled = f'<meta property="og:title" content="Fireship">{CHANNEL_PAGE}'
    yt = _youtube({channel.url: httpx.Response(200, text=titled)})
    assert await yt.page_title(channel) == "Fireship"
    untitled = _youtube({channel.url: httpx.Response(200, text=CHANNEL_PAGE)})
    assert await untitled.page_title(channel) == ""  # its id on the page is enough
    missing = {
        # a made-up channel id: 200, but neither a title nor the id
        channel.url: httpx.Response(200, text="<html></html>"),
        playlist.url: httpx.Response(
            200,
            text='<meta property="og:title" content="YouTube">'
            '{"text":"The playlist does not exist."}',
        ),
    }
    for ref in (channel, playlist):
        with pytest.raises(BadSourceLink):
            await _youtube(missing).page_title(ref)
        with pytest.raises(BadSourceLink):
            await _youtube({}).page_title(ref)  # 404
    with pytest.raises(YouTubeError):
        await _youtube({channel.url: httpx.Response(503)}).page_title(channel)


async def test_transcript_runs_the_injected_reader_off_the_loop():
    calls: list[str] = []

    def reader(video_id: str) -> Transcript:
        calls.append(video_id)
        if video_id == "nocaptions1":
            raise NoCaptions("disabled")
        return Transcript(text="hello world", language="en", generated=True)

    yt = YouTube(transcripts=reader)
    assert (await yt.transcript("1FurWDiK9A4")).text == "hello world"
    with pytest.raises(NoCaptions):
        await yt.transcript("nocaptions1")
    with pytest.raises(NoCaptions):
        await yt.transcript("../etc/passwd")
    assert calls == ["1FurWDiK9A4", "nocaptions1"]


class _Track:
    def __init__(self, code: str, generated: bool, lines: list[str]):
        self.language_code = code
        self.is_generated = generated
        self._lines = lines

    def fetch(self):
        return [type("S", (), {"text": line})() for line in self._lines]


class _Listing:
    def __init__(self, tracks):
        self.tracks = tracks

    def __iter__(self):
        return iter(self.tracks)

    def find_transcript(self, codes):
        for code in codes:
            for track in self.tracks:
                if track.language_code == code:
                    return track
        raise yta.NoTranscriptFound("vid", list(codes), None)


class _Api:
    def __init__(self, result):
        self.result = result

    def list(self, video_id):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_read_transcript_prefers_english_then_manual():
    english = _Listing(
        [
            _Track("de", False, ["Hallo"]),
            _Track("en", True, ["hello ", " [Music]", "  "]),
        ]
    )
    got = read_transcript("vid", api=_Api(english))
    assert got == Transcript(text="hello [Music]", language="en", generated=True)
    other = _Listing(
        [_Track("fr", True, ["auto"]), _Track("de", False, ["Hallo  Welt"])]
    )
    assert read_transcript("vid", api=_Api(other)).text == "Hallo Welt"


def test_read_transcript_sorts_failures():
    for missing in (
        _Listing([]),
        yta.TranscriptsDisabled("vid"),
        yta.VideoUnavailable("vid"),
    ):
        with pytest.raises(NoCaptions):
            read_transcript("vid", api=_Api(missing))
    with pytest.raises(NoCaptions):
        read_transcript("vid", api=_Api(_Listing([_Track("en", True, ["  "])])))
    for passing in (yta.RequestBlocked("vid"), requests.ConnectionError("down")):
        with pytest.raises(YouTubeError):
            read_transcript("vid", api=_Api(passing))


# --- older videos (issue #124) ------------------------------------------------ #


def lockup(video_id: str, *, url: str = "", kind: str = "VIDEO", reel=False) -> dict:
    command: dict = {
        "commandMetadata": {
            "webCommandMetadata": {"url": url or f"/watch?v={video_id}&index=1"}
        }
    }
    command["reelWatchEndpoint" if reel else "watchEndpoint"] = {"videoId": video_id}
    return {
        "lockupViewModel": {
            "contentId": video_id,
            "contentType": f"LOCKUP_CONTENT_TYPE_{kind}",
            "metadata": {"lockupMetadataViewModel": {"title": {"content": "x"}}},
            "rendererContext": {
                "commandContext": {"onTap": {"innertubeCommand": command}}
            },
        }
    }


def more(token: str) -> dict:
    """The list's last item, as YouTube nests it."""
    return {
        "continuationItemViewModel": {
            "continuationCommand": {
                "innertubeCommand": {"continuationCommand": {"token": token}}
            }
        }
    }


def list_data(items: list[dict]) -> dict:
    return {
        "contents": {
            "twoColumnBrowseResultsRenderer": {
                "tabs": [
                    {
                        "tabRenderer": {
                            "content": {
                                "sectionListRenderer": {
                                    "contents": [
                                        {"itemSectionRenderer": {"contents": items}}
                                    ]
                                }
                            }
                        }
                    }
                ]
            }
        }
    }


def list_page(items: list[dict]) -> str:
    return (
        '<html><script>ytcfg.set({"INNERTUBE_CLIENT_VERSION":"2.20261112.10.00"});'
        f"</script><script>var ytInitialData = {json.dumps(list_data(items))};"
        "</script></html>"
    )


def continuation(items: list[dict]) -> dict:
    return {
        "onResponseReceivedActions": [
            {"appendContinuationItemsAction": {"continuationItems": items}}
        ]
    }


def test_the_list_a_source_is_walked_back_through():
    assert uploads_list(f"channel:{CHANNEL}") == "UULF" + CHANNEL[2:]
    assert uploads_list("playlist:PLabcdefghij") == "PLabcdefghij"
    for bad in ("channel:nope", "playlist:a b", "instagram:123", ""):
        with pytest.raises(ValueError):
            uploads_list(bad)


def test_parse_list_keeps_videos_in_order_and_finds_the_next_page():
    data = list_data(
        [
            lockup("1FurWDiK9A4"),
            lockup("shortAAAAAA", url="/shorts/shortAAAAAA"),
            lockup("reelAAAAAAA", reel=True),
            lockup("PLabcdefghi", kind="PLAYLIST"),
            lockup("bad id"),
            {"playlistVideoRenderer": {"videoId": "legacyAAAAA"}},
            lockup("1FurWDiK9A4"),  # listed twice
            lockup("video000002"),
            more("TOKEN-2"),
        ]
    )
    assert parse_list(data) == (
        ["1FurWDiK9A4", "legacyAAAAA", "video000002"],
        "TOKEN-2",
    )
    assert parse_list(continuation([lockup("video000003")])) == (["video000003"], "")
    assert parse_list({}) == ([], "")


def watch_page(player: dict) -> str:
    return (
        "<html><script>var ytInitialPlayerResponse = "
        f"{json.dumps(player)};var meta = 1;</script></html>"
    )


def player(video_id="f0zD9d7kBrU", *, date="2024-11-06T08:00:39-08:00") -> dict:
    return {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {
            "videoId": video_id,
            "title": "Sequence[str]   is a tiny landmine",
            "channelId": CHANNEL,
            "author": "Fireship",
            "shortDescription": "  today we look at some code  ",
        },
        "microformat": {"playerMicroformatRenderer": {"publishDate": date}},
    }


def test_parse_watch_page():
    video = parse_watch_page(watch_page(player()), "f0zD9d7kBrU")
    assert video.title == "Sequence[str] is a tiny landmine"
    assert video.published_at == datetime(
        2024, 11, 6, 8, 0, 39, tzinfo=timezone(timedelta(hours=-8))
    )
    assert (video.channel_id, video.channel) == (CHANNEL, "Fireship")
    assert video.description == "today we look at some code" and not video.short
    dated = parse_watch_page(watch_page(player(date="2019-03-01")), "f0zD9d7kBrU")
    assert dated.published_at == datetime(2019, 3, 1, tzinfo=timezone.utc)


def test_parse_watch_page_failures():
    gone = {"playabilityStatus": {"status": "ERROR", "reason": "Video unavailable"}}
    with pytest.raises(VideoUnavailable, match="Video unavailable"):
        parse_watch_page(watch_page(gone), "f0zD9d7kBrU")
    # A bot check or a changed page may pass: never "unavailable".
    check = {"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in"}}
    for page in (
        watch_page(check),
        watch_page(player("other000001")),
        watch_page(player(date="")),
        "<html>nothing here</html>",
        "var ytInitialPlayerResponse = {broken",
    ):
        with pytest.raises(YouTubeError) as caught:
            parse_watch_page(page, "f0zD9d7kBrU")
        assert not isinstance(caught.value, VideoUnavailable)
    with pytest.raises(YouTubeError, match="upload date"):
        parse_watch_page(watch_page(player(date="last tuesday")), "f0zD9d7kBrU")


def _list_youtube(pages: list, seen: list | None = None) -> YouTube:
    """The first page answers the GET; each POST answers the next one."""
    replies = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        reply = replies.pop(0)
        return reply if isinstance(reply, httpx.Response) else httpx.Response(**reply)

    return YouTube(transport=httpx.MockTransport(handler))


async def _read(yt: YouTube, list_id: str = "UULFabcdefghij", **kw) -> list[list[str]]:
    return [page async for page in yt.list_pages(list_id, **kw)]


async def test_list_pages_follows_continuations_to_the_end():
    seen: list[httpx.Request] = []
    yt = _list_youtube(
        [
            {
                "status_code": 200,
                "text": list_page([lockup("video000001"), more("T2")]),
            },
            {
                "status_code": 200,
                "json": continuation([lockup("video000002"), more("T3")]),
            },
            {"status_code": 200, "json": continuation([lockup("video000003")])},
        ],
        seen,
    )
    assert await _read(yt) == [["video000001"], ["video000002"], ["video000003"]]
    first, second, third = seen
    assert str(first.url) == "https://www.youtube.com/playlist?list=UULFabcdefghij"
    assert (second.method, str(second.url)) == ("POST", BROWSE_URL)
    body = json.loads(second.content)
    assert body["continuation"] == "T2"
    assert body["context"]["client"] == {
        "clientName": "WEB",
        "clientVersion": "2.20261112.10.00",
        "hl": "en",
        "gl": "US",
    }
    assert json.loads(third.content)["continuation"] == "T3"


async def test_list_pages_stops_at_the_page_limit_or_when_closed():
    first = {"status_code": 200, "text": list_page([lockup("video000001"), more("T")])}
    again = {
        "status_code": 200,
        "json": continuation([lockup("video000002"), more("T")]),
    }
    seen: list[httpx.Request] = []
    assert await _read(_list_youtube([first, again, again], seen), pages=2) == [
        ["video000001"],
        ["video000002"],
    ]
    assert len(seen) == 2
    seen.clear()
    pages = _list_youtube([first, again], seen).list_pages("UULFabcdefghij")
    assert await anext(pages) == ["video000001"]
    await pages.aclose()
    assert len(seen) == 1  # nothing fetched past what was read


async def test_list_pages_failures():
    with pytest.raises(ValueError):
        await _read(_list_youtube([]), "a b")
    with pytest.raises(YouTubeError, match="404"):
        await _read(_list_youtube([httpx.Response(404)]))
    with pytest.raises(YouTubeError, match="couldn't read"):
        await _read(_list_youtube([httpx.Response(200, text="<html></html>")]))
    missing = list_page([]) + '{"text":"The playlist does not exist."}'
    with pytest.raises(BadSourceLink):
        await _read(_list_youtube([httpx.Response(200, text=missing)]))
    first = httpx.Response(200, text=list_page([lockup("video000001"), more("T")]))
    for bad in (httpx.Response(500), httpx.Response(200, text="not json")):
        with pytest.raises(YouTubeError):
            await _read(_list_youtube([first, bad]))
        first = httpx.Response(200, text=list_page([lockup("video000001"), more("T")]))


async def test_video_reads_its_watch_page():
    seen: list[str] = []
    yt = _youtube(
        {
            "https://www.youtube.com/watch?v=f0zD9d7kBrU": httpx.Response(
                200, text=watch_page(player())
            )
        },
        seen,
    )
    assert (await yt.video("f0zD9d7kBrU")).channel == "Fireship"
    assert seen == ["https://www.youtube.com/watch?v=f0zD9d7kBrU"]
    with pytest.raises(YouTubeError):
        await _youtube({}).video("f0zD9d7kBrU")  # 404
    with pytest.raises(VideoUnavailable):
        await yt.video("not an id")
