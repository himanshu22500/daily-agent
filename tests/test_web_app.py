"""Web feed HTTP layer, exercised offline through Starlette's TestClient."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import Insight
from daily_agent.web.app import create_app
from daily_agent.web.render import ago, inline_markdown

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
FETCH = {"X-Requested-With": "fetch"}


def _add(db: str, key: str, *, stored: int, type: str = "gotcha", text: str = ""):
    InsightStore(db).add(
        Insight(
            key=f"insight:{key}",
            text=text or f"Lesson {key}.",
            type=type,
            tags=[f"tag-{key}"],
            git_branch="main",
            captured_at=T0,
            stored_at=T0 + timedelta(minutes=stored),
        )
    )


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "web.db")
    _add(path, "a", stored=1, type="repo")
    _add(path, "b", stored=2)
    _add(path, "c", stored=3)
    return path


@pytest.fixture
def client(db) -> TestClient:
    return TestClient(create_app(db, page_size=2))


def _order(body: str) -> list[str]:
    """Keys of the feed cards in ``body``, in order (``insight:x`` -> ``x``)."""
    return re.findall(r'data-card data-swap data-key="insight:(\w)"', body)


def _more(body: str) -> str:
    return html.unescape(re.search(r'href="([^"]+)" data-more', body).group(1))


def _session(body: str) -> str:
    return re.search(r'data-session="([^"]+)"', body).group(1)


def _whole_body(client, first: str) -> str:
    """Follow infinite scroll to the end; return all the HTML served."""
    bodies = [first]
    while "data-more" in bodies[-1]:
        bodies.append(client.get(_more(bodies[-1]) + "&partial=1").text)
    return "".join(bodies)


def _whole_edition(client, first: str) -> list[str]:
    return _order(_whole_body(client, first))


def test_latest_is_newest_first_and_rendering_marks_nothing_seen(client):
    first = client.get("/?view=latest")
    assert first.status_code == 200
    assert _order(first.text) == ["c", "b"]
    assert "3 unseen" in first.text and "is-new" in first.text
    again = client.get("/?view=latest")
    assert "3 unseen" in again.text and "is-new" in again.text  # only views count
    assert _order(client.get("/?view=all").text) == ["c", "b"]  # old bookmark


def test_latest_infinite_scroll_fragment(client):
    page = client.get("/?view=latest")
    more = _more(page.text)
    assert "pv=" + _session(page.text) in more  # one page-load id across pages
    fragment = client.get(more + "&partial=1")
    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert _order(fragment.text) == ["a"]
    assert "everything" in fragment.text


def test_type_filter_and_search(client):
    assert _order(client.get("/?view=latest&type=repo").text) == ["a"]
    assert _whole_edition(client, client.get("/?type=repo").text) == ["a"]
    assert _order(client.get("/?view=latest&q=tag-b").text) == ["b"]
    assert "No insights match" in client.get("/?view=latest&q=nothing-here").text


def test_type_chips_are_bare_labels_most_common_first(client):
    chips = re.findall(r'<a class="chip[^"]*"[^>]*>(.*?)</a>', client.get("/").text)
    assert chips == ["All types", "Gotcha", "Repo"]  # no per-type counts


def test_for_you_is_a_new_edition_each_open(client):
    first, second = client.get("/").text, client.get("/").text
    assert _session(first) != _session(second)
    assert sorted(_whole_edition(client, first)) == ["a", "b", "c"]
    assert "For you" in first and 'class="tab on" href="/"' in first


def test_viewed_insights_leave_for_you_until_due(client, db):
    page = client.get("/").text
    seen = client.post(
        "/seen",
        json={"session": _session(page), "keys": ["insight:a", "insight:b"]},
        headers=FETCH,
    )
    assert seen.json() == {"counted": 2}
    assert _whole_edition(client, client.get("/").text) == ["c"]

    client.post("/seen", json={"session": "s2", "keys": ["insight:c"]})
    done = client.get("/").text
    assert _order(done) == [] and "all caught up" in done
    assert "comes back in" in done  # next revisit after the 1-day gap
    assert "0 unseen" not in done and "unseen" not in done.split("</header>")[0]


def test_due_revisits_are_labelled(client, db):
    WebFeed(db).record_views(
        ["insight:b"],
        session_id="old",
        now=datetime.now(timezone.utc) - timedelta(days=2),
    )
    for_you = _whole_body(client, client.get("/").text)
    latest = _whole_body(client, client.get("/?view=latest").text)
    assert for_you.count("↻ Revisit") == 1 and "insight:b" in for_you
    assert "↻ Revisit" not in latest  # labelled in For you only
    saved = client.post("/items/insight%3Ab/save?v=for-you", headers=FETCH)
    assert "↻ Revisit" in saved.text


def test_seen_accepts_beacons_and_rejects_bad_input(client):
    beacon = client.post(
        "/seen",
        content='{"session": "s", "keys": ["insight:a"]}',
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert beacon.json() == {"counted": 1}
    assert client.post("/seen", content="nope").status_code == 400
    assert client.post("/seen", json={"keys": ["insight:a"]}).status_code == 400
    assert client.post("/seen", json={"session": "s", "keys": "x"}).status_code == 400
    many = {"session": "s", "keys": ["k"] * 501}
    assert client.post("/seen", json=many).status_code == 400
    evil = {"Origin": "https://evil.example"}
    assert (
        client.post(
            "/seen", json={"session": "s", "keys": []}, headers=evil
        ).status_code
        == 403
    )


def test_expired_edition_asks_for_refresh(client):
    res = client.get("/?session=gone&offset=2&partial=1")
    assert res.status_code == 410 and "expired" in res.text
    # A full page load with a stale id just starts a fresh edition.
    assert sorted(_whole_edition(client, client.get("/?session=gone").text)) == [
        "a",
        "b",
        "c",
    ]


def test_save_in_place_returns_card_fragment(client):
    res = client.post("/items/insight%3Ab/save", headers=FETCH)
    assert res.status_code == 200
    assert 'aria-pressed="true"' in res.text and "<html" not in res.text
    assert _order(client.get("/?view=saved").text) == ["b"]

    client.post("/items/insight%3Ab/unsave", headers=FETCH)
    assert "Nothing saved yet" in client.get("/?view=saved").text


def test_hide_and_undo_without_js_redirect(client):
    res = client.post(
        "/items/insight%3Ac/hide?next=%2F%3Ftype%3Dgotcha", follow_redirects=False
    )
    assert res.status_code == 303 and res.headers["location"] == "/?type=gotcha"
    assert _order(client.get("/?view=latest").text) == ["b", "a"]
    assert sorted(_whole_edition(client, client.get("/").text)) == ["a", "b"]
    assert _order(client.get("/?view=hidden").text) == ["c"]

    client.post("/items/insight%3Ac/unhide", follow_redirects=False)
    assert _order(client.get("/?view=latest").text) == ["c", "b"]


def test_hide_via_fetch_returns_undo_note(client):
    res = client.post("/items/insight%3Ac/hide", headers=FETCH)
    assert "Hidden from your feed" in res.text and "/unhide" in res.text


@pytest.mark.parametrize("target", ["//evil.example/x", "/\\evil.example", "https://x"])
def test_redirect_never_leaves_the_site(client, target):
    res = client.post(
        "/items/insight%3Aa/save", params={"next": target}, follow_redirects=False
    )
    assert res.headers["location"] == "/"


def test_unknown_host_names_are_refused(db):
    # DNS-rebinding guard: only the configured names may address the server.
    app = create_app(db, allowed_hosts=["127.0.0.1", "localhost"])
    assert TestClient(app, base_url="http://evil.example").get("/").status_code == 400
    assert TestClient(app, base_url="http://127.0.0.1:8765").get("/").status_code == 200


def test_cross_site_posts_are_refused(client):
    evil = {"Origin": "https://evil.example"}
    assert client.post("/items/insight%3Aa/hide", headers=evil).status_code == 403
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert client.post("/items/insight%3Aa/hide", headers=cross).status_code == 403
    same = {"Origin": "http://testserver", **FETCH}
    assert client.post("/items/insight%3Aa/save", headers=same).status_code == 200


def test_errors(client):
    assert client.post("/items/insight%3Anope/save").status_code == 404
    assert client.post("/items/insight%3Aa/explode").status_code == 404
    assert client.get("/?view=everything").status_code == 400
    assert client.get("/?view=latest&cursor=garbage").status_code == 400
    assert client.get("/?offset=x").status_code == 400
    assert client.get("/?partial=1").status_code == 400  # For you needs a session
    assert client.get("/new-count?since=garbage").status_code == 400


def test_new_count_reports_insights_published_after_open(client, db):
    opened = re.search(r'data-opened="([^"]+)"', client.get("/").text).group(1)
    since = html.unescape(opened)
    assert client.get("/new-count", params={"since": since}).json() == {"count": 0}
    InsightStore(db).add(
        Insight(key="insight:d", text="d", captured_at=T0)  # stored now
    )
    assert client.get("/new-count", params={"since": since}).json() == {"count": 1}


def test_insight_text_cannot_inject_markup(tmp_path):
    path = str(tmp_path / "xss.db")
    _add(path, "x", stored=1, text="Use `<b>` not <img src=x onerror=alert(1)>")
    client = TestClient(create_app(path))
    for body in (client.get("/").text, client.get("/i/insight%3Ax").text):
        assert "<img" not in body
    assert "<code>&lt;b&gt;</code>" in client.get("/i/insight%3Ax").text


def test_static_assets_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    manifest = client.get("/static/manifest.webmanifest")
    assert manifest.status_code == 200 and '"start_url": "/"' in manifest.text


def test_inline_markdown_subset():
    out = str(inline_markdown("A **bold** `**not bold**` step.\n\nSecond <p>."))
    assert out == (
        "<p>A <strong>bold</strong> <code>**not bold**</code> step.</p>"
        "<p>Second &lt;p&gt;.</p>"
    )


def test_bold_may_contain_code():
    out = str(inline_markdown("• **Why `LOWER()` and not `citext`?** Because.\x00"))
    assert out == (
        "<p>• <strong>Why <code>LOWER()</code> and not <code>citext</code>?</strong>"
        " Because.</p>"
    )
    # An unpaired marker stays literal, and code never turns bold.
    assert str(inline_markdown("a ** b `**c**`")) == "<p>a ** b <code>**c**</code></p>"


def test_ago_buckets():
    now = T0
    assert ago(now - timedelta(seconds=5), now) == "just now"
    assert ago(now - timedelta(minutes=7), now) == "7m"
    assert ago(now - timedelta(hours=5), now) == "5h"
    assert ago(now - timedelta(hours=30), now) == "yesterday"
    assert ago(now - timedelta(days=9), now).startswith("Oct")


def test_web_command_serves_the_configured_db(monkeypatch, tmp_path):
    calls = {}

    def fake_run(app, **kwargs):
        calls.update(kwargs, app=app)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    # Isolate from a developer's real .env (read from cwd and os.environ).
    monkeypatch.chdir(tmp_path)
    for var in ("DAILY_AGENT_WEB_HOST", "DAILY_AGENT_WEB_PORT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", str(tmp_path / "cli.db"))
    cli.get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli.app, ["web", "--port", "9999"])
    finally:
        cli.get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    assert calls["host"] == "127.0.0.1" and calls["port"] == 9999
    served = calls["app"]
    assert (
        TestClient(served, base_url="http://127.0.0.1:9999").get("/").status_code == 200
    )
    # The CLI always enables the Host allowlist (DNS-rebinding guard).
    assert (
        TestClient(served, base_url="http://evil.example").get("/").status_code == 400
    )


def _titled(db: str) -> None:
    """Give b an AI headline and a session that maps to a project."""
    store = InsightStore(db)
    store.set_title("insight:b", "Gotchas hide in plain sight")
    store.add(
        Insight(
            key="insight:p",
            text="First sentence here. Then the details follow.",
            type="repo",
            tags=["pinia"],
            source_session="sess-1",
            captured_at=T0,
            stored_at=T0 + timedelta(minutes=9),
        )
    )
    store.set_cursor("/x/projects/-nonexistent-root-zz-repo-x/sess-1.jsonl", 1)


def test_cards_show_headline_snippet_cover_and_source(client, db):
    _titled(db)
    body = _whole_body(client, client.get("/?view=latest").text)
    assert '<h2 class="headline">Gotchas hide in plain sight</h2>' in body
    assert '<p class="snippet">Lesson b.</p>' in body  # AI title: full text below
    # Without a title: the first sentence leads, the rest is the snippet.
    assert '<h2 class="headline">First sentence here</h2>' in body
    assert '<p class="snippet">Then the details follow.</p>' in body
    assert "⌂ repo-x" in body and '<span class="src">repo-x</span>' in body
    assert '<span class="cover-topic">#pinia</span>' in body
    assert 'href="/i/insight%3Ap" data-open' in body


def test_covers_are_deterministic_per_insight():
    from daily_agent.web.render import cover_style

    assert cover_style("insight:a", "repo") == cover_style("insight:a", "repo")
    assert cover_style("insight:a", "repo") != cover_style("insight:b", "repo")


def test_reader_page_fragment_and_errors(client, db):
    _titled(db)
    page = client.get("/i/insight%3Ap")
    assert page.status_code == 200 and page.text.startswith("<!doctype html>")
    assert '<h1 class="reader-h1">First sentence here</h1>' in page.text
    assert "Learned in <strong>repo-x</strong>" in page.text
    assert 'data-session="r-' in page.text  # standalone visits get a view id
    fragment = client.get("/i/insight%3Ap?partial=1&from=%2F%3Fview%3Dlatest")
    assert "<!doctype" not in fragment.text and "data-reader" in fragment.text
    assert 'href="/?view=latest" data-close' in fragment.text
    evil = client.get("/i/insight%3Ap?partial=1&from=//evil.example")
    assert 'href="/" data-close' in evil.text
    assert client.get("/i/insight%3Anope").status_code == 404
    assert client.get("/card/insight%3Anope").status_code == 404


def test_reader_actions_answer_with_the_reader_and_cards_refresh(client, db):
    res = client.post("/items/insight%3Ab/save?as=reader", headers=FETCH)
    assert "data-reader" in res.text and 'aria-pressed="true"' in res.text
    card = client.get("/card/insight%3Ab?v=for-you")
    assert card.text.count("data-card data-swap") == 1
    assert 'aria-pressed="true"' in card.text


def test_saving_a_hidden_insight_returns_its_card_not_the_undo_note(client):
    client.post("/items/insight%3Ac/hide", headers=FETCH)
    res = client.post("/items/insight%3Ac/save?as=card", headers=FETCH)
    assert (
        'data-key="insight:c"' in res.text and "Hidden from your feed" not in res.text
    )


def test_static_assets_are_versioned_for_cache_busting(client):
    body = client.get("/").text
    version = re.search(r"/static/app\.js\?v=(\w+)", body).group(1)
    assert f"/static/app.css?v={version}" in body
    assert client.get(f"/static/app.js?v={version}").status_code == 200


def test_reader_ends_with_suggested_topics_and_read_next(client, db):
    _titled(db)  # adds insight:p tagged #pinia (plus a, b, c tagged tag-<key>)
    store = InsightStore(db)
    store.add(
        Insight(
            key="insight:q",
            text="Another Pinia lesson.",
            type="repo",
            tags=["pinia"],
            captured_at=T0,
            title="Pinia stores need explicit resets",
        )
    )
    page = client.get("/i/insight%3Ap").text
    assert "Suggested topics:" in page
    assert '<a class="topic" href="/?view=latest&amp;q=pinia">pinia</a>' in page
    assert "<h2>Read next</h2>" in page
    first = re.search(r'<a class="next-item" href="([^"]+)" data-open>', page)
    assert first.group(1) == "/i/insight%3Aq"  # shares the #pinia tag
    assert "Pinia stores need explicit resets" in page
    assert page.count('class="next-item"') == 4
    # Acting inside the reader re-renders it with Read next intact.
    saved = client.post("/items/insight%3Ap/save?as=reader", headers=FETCH)
    assert "<h2>Read next</h2>" in saved.text


def test_ago_long_spells_out_recent_times():
    from daily_agent.web.render import ago_long

    now = T0
    assert ago_long(now - timedelta(minutes=1), now) == "1 min ago"
    assert ago_long(now - timedelta(minutes=38), now) == "38 mins ago"
    assert ago_long(now - timedelta(hours=3), now) == "3 hours ago"
    assert ago_long(now - timedelta(hours=30), now) == "yesterday"


def test_version_reports_the_running_commit(tmp_path):
    """Deploy-on-merge waits for /version after a restart (issue #128)."""
    import re

    from starlette.testclient import TestClient

    from daily_agent.web.app import create_app

    reply = TestClient(create_app(str(tmp_path / "v.db"))).get("/version")
    assert reply.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{40}|", reply.json()["commit"])
