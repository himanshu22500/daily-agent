"""The web feed: a scrollable page over every stored insight (issue #77).

Server-rendered HTML plus a little vanilla JS (infinite scroll, in-place
save/hide, a "new insights" pill). No frontend build. Every action also works
without JS through plain form posts and redirects.

It only reads the insight store and writes reader state (``web_feed_state``);
the outbox and Telegram delivery are untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence
from urllib.parse import quote, urlencode

from starlette.applications import Starlette
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

from ..feed.web_feed import VIEWS, WebFeed
from .render import ago, inline_markdown, label, type_class

HERE = Path(__file__).parent

# action -> (WebFeed method name, value, fragment template to answer with)
_ACTIONS = {
    "save": ("set_saved", True, "_card.html"),
    "unsave": ("set_saved", False, "_card.html"),
    "hide": ("set_hidden", True, "_hidden.html"),
    "unhide": ("set_hidden", False, "_card.html"),
}


def _feed_url(view: str = "all", type: str = "", q: str = "", **extra: str) -> str:
    params = {"view": view if view != "all" else "", "type": type, "q": q, **extra}
    query = urlencode({k: v for k, v in params.items() if v})
    return "/?" + query if query else "/"


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


def create_app(
    db_path: str | Path,
    *,
    page_size: int = 20,
    allowed_hosts: Sequence[str] | None = None,
) -> Starlette:
    """Build the feed app.

    ``allowed_hosts`` restricts the ``Host`` header (requests for any other name
    get a 400). The CLI always sets it: without it, a hostile website could use
    DNS rebinding to point its own name at this loopback server and read the
    feed. ``None`` disables the check (tests).
    """
    feed = WebFeed(db_path)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(
        md=inline_markdown, ago=ago, label=label, type_class=type_class
    )
    templates.env.globals.update(quote=lambda s: quote(s, safe=""), feed_url=_feed_url)

    def index(request: Request) -> Response:
        params = request.query_params
        view = params.get("view") or "all"
        type_ = params.get("type") or ""
        q = params.get("q") or ""
        cursor = params.get("cursor") or None
        if view not in VIEWS:
            return PlainTextResponse(f"Unknown view: {view}", status_code=400)
        try:
            page = feed.page(
                view=view, type=type_ or None, query=q, cursor=cursor, limit=page_size
            )
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)

        partial = params.get("partial") == "1"
        # Count before marking this page seen, so the header includes it.
        unseen = 0 if partial else feed.unseen_count()
        # Rendering an entry is what "seeing" it means: the New dot shows once.
        feed.mark_seen(e.key for e in page.entries if e.new)

        filters = {"view": view, "type": type_, "q": q}
        context = {
            "page": page,
            "first_page": cursor is None,
            "filters": filters,
            "next_url": _feed_url(**filters),
            "more_url": (
                _feed_url(**filters, cursor=page.next_cursor)
                if page.next_cursor
                else None
            ),
        }
        if partial:
            return templates.TemplateResponse(request, "_cards.html", context)
        context.update(
            views=VIEWS,
            types=feed.types(),
            unseen=unseen,
            top_cursor=(
                page.entries[0].cursor if page.entries and not cursor else None
            ),
        )
        return templates.TemplateResponse(request, "index.html", context)

    def new_count(request: Request) -> Response:
        since = request.query_params.get("since") or ""
        try:
            count = feed.count_newer_than(since)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return JSONResponse({"count": count})

    def act(request: Request) -> Response:
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        key = request.path_params["key"]
        action = _ACTIONS.get(request.path_params["action"])
        if action is None:
            return PlainTextResponse("Unknown action.", status_code=404)
        method, value, fragment = action
        if not getattr(feed, method)(key, value):
            return PlainTextResponse("No such insight.", status_code=404)
        next_url = _safe_next(request.query_params.get("next"))
        if request.headers.get("x-requested-with") != "fetch":
            return RedirectResponse(next_url, status_code=303)
        return templates.TemplateResponse(
            request, fragment, {"entry": feed.entry(key), "next_url": next_url}
        )

    middleware = (
        [Middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))]
        if allowed_hosts
        else []
    )
    return Starlette(
        middleware=middleware,
        routes=[
            Route("/", index),
            Route("/new-count", new_count),
            Route("/items/{key:path}/{action}", act, methods=["POST"]),
            Mount(
                "/static", StaticFiles(directory=str(HERE / "static")), name="static"
            ),
        ],
    )
