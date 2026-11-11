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


def test_index_shows_newest_first_and_new_marks_clear(client):
    first = client.get("/")
    assert first.status_code == 200
    assert _order(first.text) == ["c", "b"]
    assert "3 new" in first.text and "is-new" in first.text

    second = client.get("/")
    assert "is-new" not in second.text  # rendered once = seen
    assert "1 new" in second.text  # "a" is still on an unloaded page


def test_infinite_scroll_fragment(client):
    page = client.get("/")
    more = html.unescape(re.search(r'href="([^"]+)" data-more', page.text).group(1))
    fragment = client.get(more + "&partial=1")
    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert _order(fragment.text) == ["a"]
    assert "all caught up" in fragment.text


def test_type_filter_and_search(client):
    assert _order(client.get("/?type=repo").text) == ["a"]
    assert _order(client.get("/?q=tag-b").text) == ["b"]
    assert "No insights match" in client.get("/?q=nothing-here").text


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
    assert _order(client.get("/").text) == ["b", "a"]
    assert _order(client.get("/?view=hidden").text) == ["c"]

    client.post("/items/insight%3Ac/unhide", follow_redirects=False)
    assert _order(client.get("/").text) == ["c", "b"]


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
    assert client.get("/?cursor=garbage").status_code == 400
    assert client.get("/new-count?since=garbage").status_code == 400


def test_new_count_reports_insights_published_after_the_top(client, db):
    page = client.get("/")
    top = html.unescape(re.search(r'data-top="([^"]+)"', page.text).group(1))
    assert client.get("/new-count", params={"since": top}).json() == {"count": 0}
    _add(db, "d", stored=4)
    assert client.get("/new-count", params={"since": top}).json() == {"count": 1}


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
