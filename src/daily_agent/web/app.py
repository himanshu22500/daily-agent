"""The web feed: a scrollable page over every stored insight (issues #77, #80).

Server-rendered HTML plus a little vanilla JS (infinite scroll, in-place
save/hide, on-screen view tracking, a "new insights" pill). No frontend build.
Every action also works without JS through plain form posts and redirects.

It only reads the insight store and writes reader state (``web_feed_state``,
``web_feed_sessions``); the outbox and Telegram delivery are untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import subprocess
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, quote, urlencode

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..feed.covers import CoverStore, cover_slug
from ..feed.instagram_posts import InstagramStore, InstagramSync, shortcode_of
from ..feed.mock_interviews import DEFAULT_QUESTIONS, InterviewError, InterviewStore
from ..feed.preferences import Preferences
from ..feed.prep import PrepStore, PrepSync
from ..feed.prep_grades import GOT, GRADES, MISSED, GradeStore
from ..feed.prep_tracks import DESIGN, DSA, RESUME, TRACKS, slug
from ..feed.prep_view import reveal_label, steps_of, tag_label
from ..feed.questions import QuestionError, QuestionStore, QuestionSync
from ..feed.ratings import DOWN, UP, Ratings
from ..feed.story_queue import sync_forever
from ..feed.videos import VideoStore, VideoSync
from ..feed.web_feed import FOR_YOU, INTERVIEW, PREP_TYPES, VIEWS, WebFeed
from ..sources.instagram import InstagramAuthError, InstagramError, is_instagram_link
from ..sources.youtube import BadSourceLink, YouTubeError
from .render import (
    ago,
    ago_long,
    back_in,
    cover_style,
    inline_markdown,
    inline_spans,
    kinds,
    label,
    longdate,
    source_hue,
    type_class,
    until,
)

HERE = Path(__file__).parent

# action -> (WebFeed method name, value)
_ACTIONS = {
    "save": ("set_saved", True),
    "unsave": ("set_saved", False),
    "hide": ("set_hidden", True),
    "unhide": ("set_hidden", False),
}
# 👍 / 👎 / Undo (issue #126): rate an item; 👎 also hides it.
_RATE_ACTIONS = (UP, DOWN, "unrate")
# Old bookmarks: the chronological view used to be called "all".
_VIEW_ALIASES = {"all": "latest"}
MAX_SEEN_BATCH = 500
MAX_SEEN_BODY = 64 * 1024
MAX_FORM_BODY = 8 * 1024


def _feed_url(view: str = FOR_YOU, type: str = "", q: str = "", **extra: str) -> str:
    params = {"view": "" if view == FOR_YOU else view, "type": type, "q": q, **extra}
    query = urlencode({k: v for k, v in params.items() if v})
    return "/?" + query if query else "/"


def _reader_url(key: str, back: str = "", **params: str) -> str:
    """``/i/<key>``, opened from ``back`` (its close link), with ``params``."""
    query = urlencode({k: v for k, v in {"from": back, **params}.items() if v})
    return f"/i/{quote(key, safe='')}" + ("?" + query if query else "")


# The Interview tab's decks (#137): what "every ... that's due" counts.
_DECK_NAMES = {
    "": "card",
    RESUME: "question",
    DSA: "DSA card",
    DESIGN: "system design card",
}


def _safe_next(value: str | None) -> str:
    """Only allow local redirects (no open redirect via ``next``).

    Browsers treat ``/\\`` like ``//`` (scheme-relative), so both are refused.
    """
    if value and value.startswith("/") and value[1:2] not in ("/", "\\"):
        return value
    return "/"


def _same_origin(request: Request) -> bool:
    """Reject cross-site POSTs (a page elsewhere can't flip reader state)."""
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        return False
    site = request.headers.get("sec-fetch-site")
    return site in (None, "same-origin", "none")


def _asset_version() -> str:
    """Short hash of the static files, appended to their URLs (cache-busting)."""
    digest = hashlib.sha1()
    for name in ("app.css", "app.js"):
        digest.update((HERE / "static" / name).read_bytes())
    return digest.hexdigest()[:10]


def _running_commit() -> str:
    """The git commit this server runs ("" outside a checkout), for /version:
    deploy-on-merge waits for it after a restart (issue #128)."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


async def _form(request: Request) -> dict[str, str]:
    """A small urlencoded form body (first value per field)."""
    body = await request.body()
    if len(body) > MAX_FORM_BODY:
        return {}
    fields = parse_qs(body.decode(errors="replace"), keep_blank_values=True)
    return {name: values[0] for name, values in fields.items()}


def create_app(
    db_path: str | Path,
    *,
    page_size: int = 20,
    prep_mix: float = 0.5,
    allowed_hosts: Sequence[str] | None = None,
    videos: VideoSync | None = None,
    instagram: InstagramSync | None = None,
    video_loop: bool = False,
    video_idle_seconds: float = 600.0,
    prep: PrepSync | None = None,
    prep_loop: bool = False,
    questions: QuestionSync | None = None,
    question_loop: bool = False,
    question_poll_seconds: float = 15.0,
) -> Starlette:
    """Build the feed app.

    ``allowed_hosts`` restricts the ``Host`` header (requests for any other name
    get a 400). The CLI always sets it: without it, a hostile website could use
    DNS rebinding to point its own name at this loopback server and read the
    feed. ``None`` disables the check (tests).

    ``videos`` lets the Sources page follow YouTube channels (issue #99), and
    ``instagram`` Instagram accounts (issue #107; ``None`` until connected).
    With ``video_loop`` the server also writes their stories in the background.
    With ``prep`` and ``prep_loop`` it writes queued interview-prep concept
    explainers (issue #108). Questions asked under an item are always stored;
    with ``questions`` and ``question_loop`` the server also answers them,
    checking for new ones every ``question_poll_seconds`` (issue #119). The
    same loop runs mock-interview turns (``questions.interviews``, #133).

    ``prep_mix`` is the share of a For you edition that is interview prep
    while the reader's Prep mode switch is on (issue #132).
    """
    feed = WebFeed(db_path)
    covers = CoverStore(db_path)
    video_store = videos.store if videos is not None else VideoStore(db_path)
    ig_store = instagram.store if instagram is not None else InstagramStore(db_path)
    prep_store = prep.store if prep is not None else PrepStore(db_path)
    question_store = (
        questions.store if questions is not None else QuestionStore(db_path)
    )
    ratings = Ratings(db_path)
    grades = GradeStore(db_path)
    prefs = Preferences(db_path)
    commit = _running_commit()
    wake = asyncio.Event()
    ig_wake = asyncio.Event()
    ask_wake = asyncio.Event()

    def post_media(key: str):
        """What to show for an Instagram story (None for anything else)."""
        shortcode = shortcode_of(key)
        return ig_store.media(shortcode) if shortcode else None

    def cover_url(key: str) -> str:
        """URL of the insight's illustrated cover, or "" if it has none."""
        slug = cover_slug(key)
        version = covers.version(key) if slug else None
        if not version:
            return ""
        return f"/covers/{slug}.svg?v={quote(version, safe='')}"

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(
        md=inline_markdown,
        ago=ago,
        until=until,
        label=label,
        type_class=type_class,
        longdate=longdate,
        ago_long=ago_long,
        kinds=kinds,
        mdi=inline_spans,
        back_in=back_in,
        tag_label=tag_label,
        reveal=reveal_label,
    )

    def last_grade(key: str):
        """A prep card's latest grade (None if never graded)."""
        return grades.latest([key]).get(key)

    templates.env.globals.update(
        steps_of=steps_of,
        reader_url=_reader_url,
        last_grade=last_grade,
        due_of=feed.due_at,
        grade_names=GRADES,
        track_names=TRACKS,
        deck_names=_DECK_NAMES,
    )
    templates.env.globals.update(
        quote=lambda s: quote(s, safe=""),
        feed_url=_feed_url,
        cover_style=cover_style,
        cover_url=cover_url,
        post_media=post_media,
        questions_for=question_store.thread,
        rating_of=ratings.get,
        source_hue=source_hue,
        asset_version=_asset_version(),
    )

    def _for_you(request: Request, type_: str, partial: bool) -> Response | dict:
        params = request.query_params
        edition = params.get("session") or ""
        try:
            offset = int(params.get("offset") or 0)
        except ValueError:
            return PlainTextResponse("Invalid offset.", status_code=400)
        # Prep mode (#132) mixes prep into the all-types deck; a type chip
        # still shows just that type.
        prep_on = prep_mix > 0 and prefs.prep_mode()
        mix = prep_mix if prep_on and not type_ else 0.0

        def build() -> str:
            return feed.new_edition(type=type_ or None, prep_mix=mix)

        if not edition:
            if partial:
                return PlainTextResponse("Missing session.", status_code=400)
            edition, offset = build(), 0
        try:
            page = feed.edition_page(edition, offset=offset, limit=page_size)
        except KeyError:
            if partial:
                return templates.TemplateResponse(
                    request, "_expired.html", {}, status_code=410
                )
            edition = build()
            page = feed.edition_page(edition, offset=0, limit=page_size)
            offset = 0
        return {
            "prep_mode": prep_on,
            "prep_mixed": mix > 0,
            "entries": page.entries,
            "session_id": edition,
            "first_page": offset == 0,
            "more_url": (
                # The chip's type rides along, so a later page knows the
                # edition wasn't a prep mix.
                _feed_url(type=type_, session=edition, offset=str(page.next_offset))
                if page.next_offset is not None
                else None
            ),
            "next_revisit": None
            if page.next_offset is not None
            else feed.next_revisit_at(prep=mix > 0),
        }

    def _chronological(
        request: Request, view: str, type_: str, q: str
    ) -> Response | dict:
        cursor = request.query_params.get("cursor") or None
        try:
            page = feed.page(
                view=view, type=type_ or None, query=q, cursor=cursor, limit=page_size
            )
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        filters = {"view": view, "type": type_, "q": q}
        return {
            "entries": page.entries,
            # Views in a chronological list count once per page load.
            "session_id": request.query_params.get("pv")
            or "p-" + secrets.token_urlsafe(6),
            "first_page": cursor is None,
            "more_url": (
                _feed_url(**filters, cursor=page.next_cursor)
                if page.next_cursor
                else None
            ),
        }

    def _interview(
        request: Request, deck: dict[str, str], partial: bool
    ) -> Response | dict:
        """The Interview tab: a practice deck rebuilt on every visit (#117),
        of one track, resume point and tag (``deck``; #137)."""
        params = request.query_params
        edition = params.get("session") or ""
        narrow = {name: value or None for name, value in deck.items()}
        try:
            offset = int(params.get("offset") or 0)
        except ValueError:
            return PlainTextResponse("Invalid offset.", status_code=400)

        def build() -> str:
            return feed.new_edition(interview=True, **narrow)

        if not edition:
            if partial:
                return PlainTextResponse("Missing session.", status_code=400)
            edition, offset = build(), 0
        try:
            page = feed.edition_page(edition, offset=offset, limit=page_size)
        except KeyError:
            if partial:
                return templates.TemplateResponse(
                    request, "_expired.html", {}, status_code=410
                )
            edition, offset = build(), 0
            page = feed.edition_page(edition, offset=0, limit=page_size)
        return {
            "entries": page.entries,
            "session_id": edition,
            "first_page": offset == 0,
            "more_url": (
                _feed_url(
                    view=INTERVIEW,
                    **deck,
                    session=edition,
                    offset=str(page.next_offset),
                )
                if page.next_offset is not None
                else None
            ),
            "next_revisit": None
            if page.next_offset is not None
            else feed.next_revisit_at(interview=True, **narrow),
        }

    def _deck(params) -> dict[str, str]:
        """The Interview deck a URL asks for: ``track``, ``tag`` and ``point``
        (a resume point implies the Resume track; old links carry only it)."""
        track = params.get("track") or ""
        track = track if track in TRACKS else ""
        point = (params.get("point") or "").strip().lower()[:20]
        if point and track in ("", RESUME):
            track = RESUME
        else:
            point = ""
        return {
            "track": track,
            "tag": slug(params.get("tag") or "", limit=40),
            "point": point,
        }

    def index(request: Request) -> Response:
        params = request.query_params
        view = params.get("view") or FOR_YOU
        view = _VIEW_ALIASES.get(view, view)
        if view not in VIEWS:
            return PlainTextResponse(f"Unknown view: {view}", status_code=400)
        type_ = params.get("type") or ""
        q = params.get("q") or ""
        deck = _deck(params)
        partial = params.get("partial") == "1"
        if view in (FOR_YOU, "latest") and type_ in PREP_TYPES and not partial:
            # Old links to the Interview/Concept chips: prep has its own tab now.
            return RedirectResponse(_feed_url(view=INTERVIEW), status_code=303)

        if view == FOR_YOU:
            result = _for_you(request, type_, partial)
        elif view == INTERVIEW:
            result = _interview(request, deck, partial)
        else:
            result = _chronological(request, view, type_, q)
        if isinstance(result, Response):
            return result
        filters = {"view": view, "type": type_, "q": q}
        if view == INTERVIEW:
            filters = {"view": view, **deck}
        context = {
            **result,
            "filters": filters,
            "show_revisit": view in (FOR_YOU, INTERVIEW),
            "next_url": _feed_url(**filters),
        }
        if view not in (FOR_YOU, INTERVIEW) and context["more_url"]:
            # Keep one page-load id across this list's infinite scroll.
            context["more_url"] += "&pv=" + quote(context["session_id"], safe="")
        if partial:
            return templates.TemplateResponse(request, "_cards.html", context)
        context.update(
            prep_toggle=prep_mix > 0,
            views=VIEWS,
            types=feed.types(),
            # Track chips hide empty tracks (unless picked); points sit
            # under Resume.
            tracks=[
                (track, name, count)
                for track, name, count in feed.interview_tracks()
                if count or track == deck["track"]
            ]
            if view == INTERVIEW
            else [],
            points=feed.interview_points()
            if view == INTERVIEW and deck["track"] == RESUME
            else [],
            unseen=feed.unseen_count(),
            opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return templates.TemplateResponse(request, "index.html", context)

    async def prep_switch(request: Request) -> Response:
        """The Prep mode switch on For you (issue #132). Body: ``on=1|0``."""
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        on = (await _form(request)).get("on")
        if on not in ("0", "1"):
            return PlainTextResponse("Expected on=1 or on=0.", status_code=400)
        await run_in_threadpool(prefs.set_prep_mode, on == "1")
        # A fresh For you edition, built with the new setting.
        return RedirectResponse(_feed_url(), status_code=303)

    def version(request: Request) -> Response:
        """The commit this server runs (deploy-on-merge's health check)."""
        return JSONResponse({"commit": commit})

    def new_count(request: Request) -> Response:
        try:
            since = datetime.fromisoformat(request.query_params.get("since") or "")
        except ValueError:
            return PlainTextResponse("Invalid 'since' timestamp.", status_code=400)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return JSONResponse({"count": feed.count_published_after(since)})

    async def seen(request: Request) -> Response:
        """Record cards that were on screen (posted by the page, often as a beacon)."""
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        body = await request.body()
        if len(body) > MAX_SEEN_BODY:
            return PlainTextResponse("Too large.", status_code=413)
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return PlainTextResponse("Invalid JSON.", status_code=400)
        keys = payload.get("keys") if isinstance(payload, dict) else None
        session_id = payload.get("session") if isinstance(payload, dict) else None
        if (
            not isinstance(keys, list)
            or len(keys) > MAX_SEEN_BATCH
            or not all(isinstance(k, str) for k in keys)
            or not isinstance(session_id, str)
            or not 0 < len(session_id) <= 64
        ):
            return PlainTextResponse(
                "Expected {keys: [...], session}.", status_code=400
            )
        counted = await run_in_threadpool(
            feed.record_views, keys, session_id=session_id
        )
        return JSONResponse({"counted": counted})

    def act(request: Request) -> Response:
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        key = request.path_params["key"]
        if request.path_params["action"] in _RATE_ACTIONS:
            return rate(request, key, request.path_params["action"])
        action = _ACTIONS.get(request.path_params["action"])
        if action is None:
            return PlainTextResponse("Unknown action.", status_code=404)
        method, value = action
        if not getattr(feed, method)(key, value):
            return PlainTextResponse("No such insight.", status_code=404)
        next_url = _safe_next(request.query_params.get("next"))
        if request.headers.get("x-requested-with") != "fetch":
            return RedirectResponse(next_url, status_code=303)
        entry = feed.entry(key)
        # Answer with the fragment the page swaps in: the reader, or the card
        # (a hidden card collapses to an undo note).
        reading = request.query_params.get("as") == "reader"
        if reading:
            fragment = "_reader.html"
        elif request.path_params["action"] == "hide":
            fragment = "_hidden.html"
        else:
            fragment = "_card.html"
        return templates.TemplateResponse(
            request,
            fragment,
            {
                "entry": entry,
                "next_url": next_url,
                "close_url": next_url,
                "show_revisit": request.query_params.get("v") in (FOR_YOU, INTERVIEW),
                # Which deck the card belongs to, for its own action links.
                "filters": {"view": request.query_params.get("v") or ""},
                "related": feed.related(key) if reading else [],
            },
        )

    def rate(request: Request, key: str, action: str) -> Response:
        """👍, 👎 (also hides the item) or Undo (issue #126).

        Answers a fetch with what the page swaps in: the card (``as=card``; a
        👎 folds it into an undo note), the reader's rate box (``as=rate``),
        or the whole reader (``as=reader``).
        """
        if feed.entry(key) is None:
            return PlainTextResponse("No such insight.", status_code=404)
        if action == "unrate":
            gone = ratings.clear(key)
            if gone is not None and gone.down:
                feed.set_hidden(key, False)
        else:
            ratings.rate(key, action)
            # 👎 removes it; 👍 brings back an item hidden earlier.
            feed.set_hidden(key, action == DOWN)
        next_url = _safe_next(request.query_params.get("next"))
        if request.headers.get("x-requested-with") != "fetch":
            return RedirectResponse(next_url, status_code=303)
        entry = feed.entry(key)
        shown_as = request.query_params.get("as")
        if shown_as == "rate":
            fragment = "_rate.html"
        elif shown_as == "reader":
            fragment = "_reader.html"
        elif action == DOWN:
            fragment = "_rated.html"
        else:
            fragment = "_card.html"
        return templates.TemplateResponse(
            request,
            fragment,
            {
                "entry": entry,
                "next_url": next_url,
                "close_url": next_url,
                "show_revisit": request.query_params.get("v") in (FOR_YOU, INTERVIEW),
                "filters": {"view": request.query_params.get("v") or ""},
                "related": feed.related(key) if shown_as == "reader" else [],
            },
        )

    async def rate_reasons(request: Request) -> Response:
        """Why a 👎 (optional; the sheet after it). Body: ``reasons=a,b``."""
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        key = request.path_params["key"]
        codes = (await _form(request)).get("reasons", "").split(",")
        saved = await run_in_threadpool(ratings.set_reasons, key, codes)
        if not saved:
            return PlainTextResponse("Rate it 👎 first.", status_code=409)
        return JSONResponse({"reasons": list(ratings.get(key).reasons)})

    async def grade(request: Request) -> Response:
        """Got it / Shaky / Missed on a prep card (#137), or a drill's picked
        option, which grades itself: right is got it, wrong is missed.

        Body: ``grade=got|shaky|missed`` or ``choice=A..D``. A fetch gets back
        what the page swaps in: the grade box, or with ``as=steps`` the
        drill's steps. A plain form post returns to the reader with every
        step open, the grade shown and the drill's pick marked.
        """
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        entry = feed.entry(request.path_params["key"])
        if entry is None or not entry.gradable:
            return PlainTextResponse("No such prep card.", status_code=404)
        form = await _form(request)
        choice = form.get("choice", "").strip().upper()
        given = form.get("grade", "")
        if choice:
            drill = steps_of(entry.type, entry.text).drill
            if drill is None or drill.option(choice) is None:
                return PlainTextResponse("No such option.", status_code=400)
            given = GOT if drill.right(choice) else MISSED
        if given not in GRADES:
            return PlainTextResponse("Unknown grade.", status_code=400)
        await run_in_threadpool(grades.grade, entry.key, given)
        back = _safe_next(request.query_params.get("from"))
        if request.headers.get("x-requested-with") != "fetch":
            shown = {"choice": choice} if choice else {"steps": "all"}
            target = _reader_url(entry.key, back, **shown, graded=given)
            return RedirectResponse(target + "#grade", status_code=303)
        as_steps = request.query_params.get("as") == "steps"
        return templates.TemplateResponse(
            request,
            "_steps.html" if as_steps else "_grade.html",
            {"entry": entry, "close_url": back, "graded": given, "choice": choice},
        )

    def card(request: Request) -> Response:
        """One feed card, to refresh it after acting on it in the reader."""
        entry = feed.entry(request.path_params["key"])
        if entry is None:
            return PlainTextResponse("No such insight.", status_code=404)
        return templates.TemplateResponse(
            request,
            "_card.html",
            {
                "entry": entry,
                "next_url": _safe_next(request.query_params.get("next")),
                "show_revisit": request.query_params.get("v") in (FOR_YOU, INTERVIEW),
                # Which deck the card belongs to, for its own action links.
                "filters": {"view": request.query_params.get("v") or ""},
            },
        )

    def _reader_context(request: Request, entry) -> dict:
        params = request.query_params
        choice = (params.get("choice") or "").upper()
        graded = params.get("graded") or ""
        return {
            "entry": entry,
            "close_url": _safe_next(params.get("from")),
            "session_id": "r-" + secrets.token_urlsafe(6),
            "related": feed.related(entry.key),
            # A prep card's state after a grade without JS (#137): every step
            # open, the grade just given, and a drill's picked option.
            "steps_open": params.get("steps") == "all",
            "graded": graded if graded in GRADES else "",
            "choice": choice if choice in ("A", "B", "C", "D") else "",
        }

    def reader(request: Request) -> Response:
        """An insight opened like an article: overlay fragment or standalone page."""
        entry = feed.entry(request.path_params["key"])
        if entry is None:
            return PlainTextResponse("No such insight.", status_code=404)
        context = _reader_context(request, entry)
        if request.query_params.get("partial") == "1":
            return templates.TemplateResponse(request, "_reader.html", context)
        return templates.TemplateResponse(request, "reader_page.html", context)

    # Mock interviews in the Ask box of interview-prep cards (issue #133).
    interviews = questions.interviews if questions is not None else None
    interview_store = (
        interviews.store if interviews is not None else InterviewStore(db_path)
    )
    interview_questions = (
        interviews.questions if interviews is not None else DEFAULT_QUESTIONS
    )

    def ask_thread(asked: list, mocks: list) -> list[tuple[str, object]]:
        """Questions and mock interviews on an item, as one chat in time order."""
        items = [(q.asked_at, "q", q) for q in asked]
        items += [(iv.started_at, "mock", iv) for iv in mocks]
        items.sort(key=lambda item: (item[0], item[1]))
        return [(kind, item) for _, kind, item in items]

    templates.env.globals.update(
        interviews_for=interview_store.for_item,
        can_interview=lambda entry: entry.type in PREP_TYPES,
        interview_questions=interview_questions,
        ask_thread=ask_thread,
    )

    def _ask_refused(request: Request, entry, error: Exception, text: str):
        """The Ask box again with the error, and what was typed kept."""
        status = getattr(error, "status", 400)
        context = {"entry": entry, "ask_error": str(error), "ask_text": text}
        if request.headers.get("x-requested-with") == "fetch":
            return templates.TemplateResponse(
                request, "_ask.html", context, status_code=status
            )
        return templates.TemplateResponse(
            request,
            "reader_page.html",
            {**_reader_context(request, entry), **context},
            status_code=status,
        )

    async def ask(request: Request) -> Response:
        """The "Ask about this" box under an item: GET its thread, POST a
        question (Claude answers it on the Mac; issue #119)."""
        entry = feed.entry(request.path_params["key"])
        if entry is None:
            return PlainTextResponse("No such insight.", status_code=404)
        fetched = request.headers.get("x-requested-with") == "fetch"
        if request.method == "GET":
            return templates.TemplateResponse(request, "_ask.html", {"entry": entry})
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        text = (await _form(request)).get("question", "")
        try:
            question = await run_in_threadpool(
                question_store.ask, entry.key, text, now=datetime.now(timezone.utc)
            )
        except QuestionError as exc:
            return _ask_refused(request, entry, exc, text)
        ask_wake.set()  # answered right away when this server is the Mac's
        if fetched:
            return templates.TemplateResponse(request, "_ask.html", {"entry": entry})
        return RedirectResponse(
            f"/i/{quote(entry.key, safe='')}#ask-{question.id}", status_code=303
        )

    async def interview(request: Request) -> Response:
        """A mock interview on a prep card (issue #133). POST ``do=start``, or
        ``do=reply`` / ``do=end`` ("End & grade") with the ``interview`` id
        and the ``answer``. Claude's side runs on the Mac, like Ask."""
        entry = feed.entry(request.path_params["key"])
        if entry is None:
            return PlainTextResponse("No such insight.", status_code=404)
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        form = await _form(request)
        step = form.get("do") or ("reply" if form.get("interview") else "start")
        text = form.get("answer", "")
        now = datetime.now(timezone.utc)
        try:
            if entry.type not in PREP_TYPES:
                raise InterviewError("Mock interviews are for interview-prep cards.")
            if step == "start":
                current = await run_in_threadpool(
                    interview_store.start,
                    entry.key,
                    questions=interview_questions,
                    now=now,
                )
            elif step in ("reply", "end"):
                current = await run_in_threadpool(
                    interview_store.answer,
                    entry.key,
                    form.get("interview", ""),
                    text,
                    end=step == "end",
                    now=now,
                )
            else:
                raise InterviewError("Unknown step.")
        except InterviewError as exc:
            return _ask_refused(request, entry, exc, text)
        ask_wake.set()  # the Mac's own server starts on it right away
        if request.headers.get("x-requested-with") == "fetch":
            return templates.TemplateResponse(request, "_ask.html", {"entry": entry})
        return RedirectResponse(
            f"/i/{quote(entry.key, safe='')}#turn-{current.turns[-1].id}",
            status_code=303,
        )

    def post_image(request: Request) -> Response:
        """A stored Instagram image (downloaded once; Instagram's URLs expire)."""
        shortcode = shortcode_of("ig:" + request.path_params["shortcode"])
        image = (
            ig_store.image(shortcode, request.path_params["n"]) if shortcode else None
        )
        if image is None:
            return PlainTextResponse("No such image.", status_code=404)
        return Response(
            image.data,
            media_type=image.content_type,
            headers={
                "Content-Security-Policy": "default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=604800",
            },
        )

    def cover(request: Request) -> Response:
        """An illustrated cover. Model-drawn SVG, so served fully locked down."""
        name = request.path_params["name"]
        svg = covers.get(f"insight:{name}") if cover_slug(name) == name else None
        if svg is None:
            return PlainTextResponse("No such cover.", status_code=404)
        return Response(
            svg,
            media_type="image/svg+xml",
            headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    def _sources_page(
        request: Request, *, error: str = "", link: str = "", status_code: int = 200
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "sources.html",
            {
                "sources": video_store.statuses() + ig_store.statuses(),
                "prep": prep_store.progress(),
                "error": error,
                "link": link,
                "can_follow": videos is not None or instagram is not None,
                "instagram": instagram is not None,
                "background": video_loop,
            },
            status_code=status_code,
        )

    async def sources(request: Request) -> Response:
        """Followed YouTube channels; POST a link to follow one."""
        if request.method == "GET":
            return _sources_page(request)
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        link = (await _form(request)).get("link", "").strip()[:500]
        if is_instagram_link(link):
            return await _follow_instagram(request, link)
        if videos is None:
            return _sources_page(
                request,
                error="Following channels isn't available on this server.",
                link=link,
                status_code=503,
            )
        try:
            await videos.follow(link)
        except BadSourceLink as exc:
            return _sources_page(request, error=str(exc), link=link, status_code=400)
        except YouTubeError as exc:
            return _sources_page(
                request,
                error=f"Couldn't reach YouTube ({exc}). Try again in a minute.",
                link=link,
                status_code=502,
            )
        wake.set()
        return RedirectResponse("/sources", status_code=303)

    async def _follow_instagram(request: Request, link: str) -> Response:
        if instagram is None:
            return _sources_page(
                request,
                error=(
                    "Instagram isn't connected yet. On the Mac, run "
                    "`daily-agent instagram connect`, then restart the feed."
                ),
                link=link,
                status_code=503,
            )
        try:
            await instagram.follow(link)
        except BadSourceLink as exc:
            return _sources_page(request, error=str(exc), link=link, status_code=400)
        except InstagramAuthError as exc:
            return _sources_page(request, error=str(exc), link=link, status_code=502)
        except InstagramError as exc:
            return _sources_page(
                request,
                error=f"Couldn't reach Instagram ({exc}). Try again in a minute.",
                link=link,
                status_code=502,
            )
        ig_wake.set()
        return RedirectResponse("/sources", status_code=303)

    async def unfollow(request: Request) -> Response:
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        source_id = (await _form(request)).get("id", "")
        store = ig_store if source_id.startswith("instagram:") else video_store
        store.unfollow(source_id, now=datetime.now(timezone.utc))
        return RedirectResponse("/sources", status_code=303)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        tasks = []
        if video_loop:
            for sync, event in ((videos, wake), (instagram, ig_wake)):
                if sync is not None:
                    tasks.append(
                        asyncio.create_task(
                            sync_forever(sync, event, idle=video_idle_seconds)
                        )
                    )
        if prep is not None and prep_loop:
            tasks.append(
                asyncio.create_task(
                    sync_forever(prep, asyncio.Event(), idle=video_idle_seconds)
                )
            )
        if questions is not None and question_loop:
            tasks.append(
                asyncio.create_task(
                    sync_forever(questions, ask_wake, idle=question_poll_seconds)
                )
            )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    middleware = (
        [Middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))]
        if allowed_hosts
        else []
    )
    return Starlette(
        middleware=middleware,
        lifespan=lifespan,
        routes=[
            Route("/", index),
            Route("/sources", sources, methods=["GET", "POST"]),
            Route("/sources/unfollow", unfollow, methods=["POST"]),
            Route("/new-count", new_count),
            Route("/version", version),
            Route("/prefs/prep-mode", prep_switch, methods=["POST"]),
            Route("/seen", seen, methods=["POST"]),
            Route("/items/{key:path}/grade", grade, methods=["POST"]),
            Route("/items/{key:path}/ask", ask, methods=["GET", "POST"]),
            Route("/items/{key:path}/interview", interview, methods=["POST"]),
            Route("/items/{key:path}/reasons", rate_reasons, methods=["POST"]),
            Route("/items/{key:path}/{action}", act, methods=["POST"]),
            Route("/card/{key:path}", card),
            Route("/covers/{name}.svg", cover),
            Route("/ig/{shortcode}/{n:int}.jpg", post_image),
            Route("/i/{key:path}", reader),
            Mount(
                "/static", StaticFiles(directory=str(HERE / "static")), name="static"
            ),
        ],
    )
