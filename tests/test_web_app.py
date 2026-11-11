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
    return re.findall(r"Lesson (\w)\.", body)


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
    assert for_you.count("↻ Revisit") == 1 and "Lesson b." in for_you
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
    assert "★ Saved" in res.text and "<html" not in res.text
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
    body = TestClient(create_app(path)).get("/").text
    assert "<img" not in body
    assert "<code>&lt;b&gt;</code>" in body


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
