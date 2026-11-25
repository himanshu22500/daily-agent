"""The feed as an installable Android app (issue #154): manifest, icons and
service worker, exercised offline through Starlette's TestClient."""

from __future__ import annotations

import re
import struct

import pytest
from starlette.testclient import TestClient

from daily_agent.web.app import create_app


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(create_app(str(tmp_path / "web.db")))


def _png_size(body: bytes) -> tuple[int, int]:
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", body[16:24])


def test_manifest_meets_chromes_install_criteria(client):
    reply = client.get("/static/manifest.webmanifest")
    assert reply.status_code == 200
    manifest = reply.json()
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/" and manifest["scope"] == "/"
    assert manifest["id"] == "/"
    assert manifest["name"] and manifest["short_name"]
    pngs = {
        (icon["sizes"], icon["purpose"]): icon["src"]
        for icon in manifest["icons"]
        if icon["type"] == "image/png"
    }
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= set(
        pngs
    )


def test_manifest_icons_are_served_at_their_declared_size(client):
    for icon in client.get("/static/manifest.webmanifest").json()["icons"]:
        reply = client.get(icon["src"])
        assert reply.status_code == 200, icon["src"]
        if icon["type"] == "image/png":
            width, height = _png_size(reply.content)
            assert f"{width}x{height}" == icon["sizes"], icon["src"]


def test_service_worker_is_served_from_the_root_uncached(client):
    reply = client.get("/sw.js")
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/javascript")
    assert reply.headers["cache-control"] == "no-cache"
    assert 'addEventListener("fetch"' in reply.text


def test_service_worker_carries_the_asset_version(client):
    """A deploy that changes app.css/app.js changes the worker's cache name, so
    the phone drops the old assets; the precached CSS/JS match the page's."""
    page = client.get("/").text
    version = re.search(r"/static/app\.js\?v=(\w+)", page).group(1)
    worker = client.get("/sw.js").text
    assert "__VERSION__" not in worker
    assert f'const VERSION = "{version}";' in worker
    for asset in re.findall(r'"(/static/[^"]+)"', worker):
        assert client.get(asset).status_code == 200, asset


def test_pages_link_the_manifest_and_register_the_worker(client):
    page = client.get("/").text
    assert 'rel="manifest" href="/static/manifest.webmanifest"' in page
    assert 'rel="apple-touch-icon"' in page
    script = client.get("/static/app.js").text
    assert 'navigator.serviceWorker.register("/sw.js")' in script
    assert "window.isSecureContext" in script
