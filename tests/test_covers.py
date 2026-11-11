"""Illustrated covers: strict SVG validation, import, and locked-down serving."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.feed.covers import cover_slug, import_covers, validate_cover
from daily_agent.feed.insights_store import InsightStore
from daily_agent.models import Insight
from daily_agent.web.app import create_app

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
PAD = '<rect x="1" y="1" width="2" height="2" fill="#123456"/>' * 12


def _svg(body: str = "", *, root: str = 'viewBox="0 0 800 400"') -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" {root}>'
        f'<rect width="800" height="400" fill="#274690"/>{PAD}{body}</svg>'
    ).encode()


def test_a_plain_static_illustration_is_accepted():
    art = _svg(
        '<defs><linearGradient id="a-g"><stop offset="0" stop-color="#fff"/>'
        '</linearGradient></defs><circle cx="400" cy="200" r="90" fill="url(#a-g)"/>'
        '<use href="#a-g"/><text x="10" y="50" font-family="monospace">_id</text>'
    )
    assert validate_cover(art) == []


@pytest.mark.parametrize(
    ("svg", "reason"),
    [
        (_svg("<script>alert(1)</script>"), "<script>"),
        (
            _svg(
                '<foreignObject><b xmlns="http://www.w3.org/1999/xhtml"/></foreignObject>'
            ),
            "<foreignObject>",
        ),
        (_svg('<image href="https://evil.example/x.png"/>'), "<image>"),
        (_svg('<circle r="5" onclick="x()"/>'), "event attribute"),
        (_svg('<use href="https://evil.example/a.svg#x"/>'), "href must point inside"),
        (_svg('<rect fill="url(https://evil.example/p)"/>'), "external url()"),
        (_svg('<rect style="background:url(x); @import url(y)"/>'), "unsafe style"),
        (_svg("<style>@import url(x)</style>"), "<style>"),
        (_svg(root='viewBox="0 0 100 100"'), "viewBox"),
        (_svg("<rect"), "not well-formed"),
        (b'<!DOCTYPE svg [<!ENTITY a "b">]>' + _svg(), "<!doctype"),
        (b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 400'/>", "size"),
        (_svg("<rect/>" * 3000), "size"),
        (
            _svg().replace(b"http://www.w3.org/2000/svg", b"http://example.com/ns"),
            "root element",
        ),
    ],
)
def test_unsafe_or_malformed_covers_are_rejected(svg, reason):
    problems = validate_cover(svg)
    assert problems and any(reason in p for p in problems), problems


def test_slugs_are_path_safe():
    assert cover_slug("insight:uv-sync-prunes") == "uv-sync-prunes"
    assert cover_slug("insight:../../etc/passwd") is None
    assert cover_slug("insight:Has Space") is None


def test_import_copies_only_valid_covers_for_known_insights(tmp_path):
    source, dest = tmp_path / "in", tmp_path / "covers"
    source.mkdir()
    (source / "good.svg").write_bytes(_svg())
    (source / "bad.svg").write_bytes(_svg("<script/>"))
    (source / "stranger.svg").write_bytes(_svg())
    result = import_covers(source, dest, {"insight:good", "insight:bad"})
    assert result.imported == 1 and result.unknown == ["stranger.svg"]
    assert list(result.rejected) == ["bad.svg"]
    assert [p.name for p in dest.iterdir()] == ["good.svg"]


def _app(tmp_path, *, with_cover: bool = True):
    db = str(tmp_path / "c.db")
    InsightStore(db).add(
        Insight(key="insight:good", text="A lesson.", tags=["x"], captured_at=T0)
    )
    InsightStore(db).add(
        Insight(key="insight:plain", text="No art yet.", tags=["y"], captured_at=T0)
    )
    covers = tmp_path / "covers"
    covers.mkdir()
    if with_cover:
        (covers / "good.svg").write_bytes(_svg())
    (tmp_path / "secret.svg").write_bytes(_svg())
    return TestClient(create_app(db, covers_dir=covers))


def test_cards_use_the_illustration_when_one_exists(tmp_path):
    client = _app(tmp_path)
    body = client.get("/?view=latest").text
    assert body.count('<div class="cover has-art"') == 1
    assert 'src="/covers/good.svg?v=' in body
    assert '<span class="cover-topic">#y</span>' in body  # fallback gradient
    assert "has-art" in client.get("/i/insight%3Agood").text
    # Read next thumbnails use it too.
    assert 'class="cover thumb has-art"' in client.get("/i/insight%3Aplain").text


def test_covers_are_served_locked_down(tmp_path):
    client = _app(tmp_path)
    res = client.get("/covers/good.svg")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert "default-src 'none'" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"
    assert client.get("/covers/missing.svg").status_code == 404
    assert client.get("/covers/..%2Fsecret.svg").status_code == 404
    assert client.get("/covers/Good.svg").status_code == 404


def test_feed_works_without_a_covers_directory(tmp_path):
    db = str(tmp_path / "n.db")
    InsightStore(db).add(Insight(key="insight:a", text="A.", captured_at=T0))
    client = TestClient(create_app(db))
    assert "has-art" not in client.get("/?view=latest").text
    assert client.get("/covers/a.svg").status_code == 404


def test_covers_import_command(monkeypatch, tmp_path):
    db = str(tmp_path / "cli.db")
    InsightStore(db).add(Insight(key="insight:good", text="A.", captured_at=T0))
    source = tmp_path / "in"
    source.mkdir()
    (source / "good.svg").write_bytes(_svg())
    (source / "nope.svg").write_bytes(_svg())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", db)
    monkeypatch.setenv("DAILY_AGENT_COVERS_DIR", str(tmp_path / "out"))
    cli.get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli.app, ["insights", "covers-import", str(source)])
    finally:
        cli.get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    assert "Imported 1 cover(s)" in result.output and "1 for unknown" in result.output
    assert (tmp_path / "out" / "good.svg").exists()
