"""YouTube adapter (issue #99): link parsing, feeds, captions. All offline."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import requests
import youtube_transcript_api as yta

from daily_agent.sources.youtube import (
    BadSourceLink,
    FeedUnavailable,
    NoCaptions,
    SourceRef,
    Transcript,
    YouTube,
    YouTubeError,
    channel_id_from_page,
    channel_ref,
    parse_feed,
    parse_link,
    playlist_ref,
    read_transcript,
    title_from_page,
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
