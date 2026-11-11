"""Illustrated covers: strict validation, database storage, import, generation, serving."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.feed.covers import (
    CoverStore,
    cover_slug,
    generate_covers,
    import_covers,
    validate_cover,
)
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


def test_store_keeps_only_valid_svgs_and_versions_changes(tmp_path):
    covers = CoverStore(tmp_path / "s.db")
    assert covers.put("insight:a", _svg("<script/>").decode())
    assert covers.get("insight:a") is None and covers.version("insight:a") is None
    assert covers.put("insight:a", _svg().decode(), source="subagent:fable") == []
    first = covers.version("insight:a")
    assert covers.get("insight:a") == _svg().decode() and covers.keys() == {"insight:a"}
    covers.put("insight:a", _svg('<circle r="9"/>').decode())
    assert covers.version("insight:a") != first


def test_import_stores_only_valid_covers_for_known_insights(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    (source / "good.svg").write_bytes(_svg())
    (source / "bad.svg").write_bytes(_svg("<script/>"))
    (source / "stranger.svg").write_bytes(_svg())
    (source / "binary.svg").write_bytes(b"\xff\xfe" + _svg())
    covers = CoverStore(tmp_path / "i.db")
    result = import_covers(
        source,
        covers,
        {"insight:good", "insight:bad", "insight:binary"},
        source="subagent:fable",
    )
    assert result.imported == 1 and result.unknown == ["stranger.svg"]
    assert sorted(result.rejected) == ["bad.svg", "binary.svg"]
    assert covers.keys() == {"insight:good"}


def _app(tmp_path):
    db = str(tmp_path / "c.db")
    InsightStore(db).add(
        Insight(key="insight:good", text="A lesson.", tags=["x"], captured_at=T0)
    )
    InsightStore(db).add(
        Insight(key="insight:plain", text="No art yet.", tags=["y"], captured_at=T0)
    )
    CoverStore(db).put("insight:good", _svg().decode())
    return TestClient(create_app(db))


def test_cards_use_the_illustration_when_one_exists(tmp_path):
    client = _app(tmp_path)
    body = client.get("/?view=latest").text
    assert body.count('<div class="cover has-art"') == 1
    assert 'src="/covers/good.svg?v=' in body
    assert '<span class="cover-topic">#y</span>' in body  # fallback gradient
    assert "has-art" in client.get("/i/insight%3Agood").text
    # Read next thumbnails use it too.
    assert 'class="cover thumb has-art"' in client.get("/i/insight%3Aplain").text


def test_covers_are_served_locked_down_from_the_database(tmp_path):
    client = _app(tmp_path)
    res = client.get("/covers/good.svg")
    assert res.status_code == 200 and res.text == _svg().decode()
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert "default-src 'none'" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"
    assert client.get("/covers/plain.svg").status_code == 404
    assert client.get("/covers/..%2Fsecret.svg").status_code == 404
    assert client.get("/covers/Good.svg").status_code == 404


def test_feed_works_with_no_covers_at_all(tmp_path):
    db = str(tmp_path / "n.db")
    InsightStore(db).add(Insight(key="insight:a", text="A.", captured_at=T0))
    client = TestClient(create_app(db))
    assert "has-art" not in client.get("/?view=latest").text
    assert client.get("/covers/a.svg").status_code == 404


def _cli(monkeypatch, tmp_path, db, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", db)
    cli.get_settings.cache_clear()
    try:
        return CliRunner().invoke(cli.app, ["insights", *args])
    finally:
        cli.get_settings.cache_clear()


def test_covers_import_command_stores_into_the_database(monkeypatch, tmp_path):
    db = str(tmp_path / "cli.db")
    InsightStore(db).add(Insight(key="insight:good", text="A.", captured_at=T0))
    source = tmp_path / "in"
    source.mkdir()
    (source / "good.svg").write_bytes(_svg())
    (source / "nope.svg").write_bytes(_svg())
    result = _cli(
        monkeypatch,
        tmp_path,
        db,
        "covers-import",
        str(source),
        "--label",
        "subagent:fable",
    )
    assert result.exit_code == 0, result.output
    assert "Imported 1 cover(s) into the database" in result.output
    assert "1 for unknown" in result.output
    assert CoverStore(db).get("insight:good") == _svg().decode()


async def test_generate_retries_with_feedback_and_isolates_failures(tmp_path):
    db = str(tmp_path / "g.db")
    store = InsightStore(db)
    for key in ("first-try", "second-try", "never", "boom", "has-cover"):
        store.add(
            Insight(key=f"insight:{key}", text=key, title=key.upper(), captured_at=T0)
        )
    covers = CoverStore(db)
    covers.put("insight:has-cover", _svg().decode(), source="subagent:fable")
    calls: list[tuple[str, list[str] | None]] = []

    async def draw(record, feedback):
        key = record["key"].removeprefix("insight:")
        calls.append((key, feedback))
        assert record["title"] == key.upper()
        if key == "boom":
            raise RuntimeError("status_code: 503, overloaded\ntrace")
        if key == "first-try" or (key == "second-try" and feedback):
            return _svg().decode()
        return _svg("<script/>").decode()

    result = await generate_covers(store, covers, draw, source="model:x", concurrency=2)
    assert result.written == 2
    assert set(result.failed) == {"insight:never", "insight:boom"}
    assert result.failed["insight:boom"] == ["status_code: 503, overloaded"]
    assert any("<script>" in p for p in result.failed["insight:never"])
    retry = [fb for key, fb in calls if key == "second-try"]
    assert retry[0] is None and any("<script>" in p for p in retry[1])
    # Insights that already have a cover are never sent to the model.
    assert "has-cover" not in {key for key, _ in calls}
    assert covers.keys() == {
        "insight:first-try",
        "insight:second-try",
        "insight:has-cover",
    }


async def test_generate_respects_limit_and_overwrite(tmp_path):
    db = str(tmp_path / "l.db")
    store = InsightStore(db)
    for key in "abc":
        store.add(Insight(key=f"insight:{key}", text=key, captured_at=T0))
    covers = CoverStore(db)
    covers.put("insight:a", _svg('<circle r="1"/>').decode())
    drawn = []

    async def draw(record, feedback):
        drawn.append(record["key"])
        return _svg().decode()

    assert (await generate_covers(store, covers, draw, limit=1)).written == 1
    assert drawn == ["insight:b"]
    await generate_covers(store, covers, draw, overwrite=True)
    assert covers.get("insight:a") == _svg().decode()


async def test_empty_replies_are_retried_then_reported(tmp_path):
    db = str(tmp_path / "e.db")
    InsightStore(db).add(Insight(key="insight:a", text="A.", captured_at=T0))

    async def draw(record, feedback):
        return ""

    result = await generate_covers(InsightStore(db), CoverStore(db), draw)
    assert result.failed == {"insight:a": ["reply contained no <svg>"]}


def test_extract_svg_from_chatty_replies():
    from daily_agent.agents.cover_illustrator import extract_svg

    svg = _svg().decode()
    assert extract_svg(f"Here you go:\n```svg\n{svg}\n```\nEnjoy!") == svg
    assert extract_svg("Sorry, I can't draw that.") == ""


def test_covers_generate_command(monkeypatch, tmp_path):
    db = str(tmp_path / "gen.db")
    InsightStore(db).add(Insight(key="insight:a", text="A.", captured_at=T0))
    seen_models = []

    async def fake_draw(model, record, feedback=None):
        seen_models.append(model)
        return _svg().decode()

    monkeypatch.setattr(cli, "draw_cover", fake_draw)
    monkeypatch.setenv("DAILY_AGENT_MODEL", "test:default")
    result = _cli(
        monkeypatch, tmp_path, db, "covers-generate", "--model", "test:artist"
    )
    assert result.exit_code == 0, result.output
    assert "Drew 1 cover(s) with test:artist" in result.output
    assert seen_models == ["test:artist"]
    assert CoverStore(db).get("insight:a") == _svg().decode()
