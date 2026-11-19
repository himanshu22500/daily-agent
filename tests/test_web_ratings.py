"""👍 / 👎 in the web feed (issue #126), through Starlette's TestClient."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.ratings import Ratings
from daily_agent.feed.web_feed import WebFeed
from daily_agent.models import Insight
from daily_agent.web.app import create_app

T0 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
FETCH = {"X-Requested-With": "fetch"}
KEY = "insight:retries"
ITEM = "/items/insight%3Aretries"


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "web.db")
    InsightStore(path).add(
        Insight(
            key=KEY,
            text="Retry with backoff.",
            title="Back off before you retry",
            type="gotcha",
            captured_at=T0,
        )
    )
    return path


@pytest.fixture
def client(db) -> TestClient:
    return TestClient(create_app(db))


def _hidden(db) -> bool:
    return WebFeed(db).entry(KEY).hidden


def test_every_card_has_a_not_useful_button(client):
    page = client.get("/?view=latest").text
    assert f'action="{ITEM}/down?as=card&amp;next=' in page
    assert 'data-rate="down"' in page and 'data-kinds="gotchas"' in page
    assert 'aria-label="Not useful"' in page


def test_thumbs_down_removes_the_card_and_undo_brings_it_back(client, db):
    res = client.post(f"{ITEM}/down?as=card&next=/", headers=FETCH)
    assert res.status_code == 200
    assert "Removed from your feed." in res.text
    assert f'action="{ITEM}/unrate?as=card&next=%2F"' in res.text
    assert _hidden(db) and Ratings(db).get(KEY).down
    assert "Back off before you retry" not in client.get("/?view=latest").text
    hidden_tab = client.get("/?view=hidden").text
    assert "Back off before you retry" in hidden_tab
    assert 'data-rate="down"' not in hidden_tab  # no 👎 on what's already gone

    res = client.post(f"{ITEM}/unrate?as=card&next=/", headers=FETCH)
    assert 'class="story t-gotcha' in res.text  # the card again
    assert not _hidden(db) and Ratings(db).get(KEY) is None


def test_the_reader_ends_with_was_this_worth_reading(client):
    page = client.get(f"/i/{KEY}").text
    assert "<h2>Was this worth reading?</h2>" in page
    assert f'action="{ITEM}/up?as=rate&amp;next=%2F" data-rate="up"' in page
    assert f'action="{ITEM}/down?as=rate&amp;next=%2F" data-rate="down"' in page
    # After the text, before the Ask box.
    assert page.index('class="reader-text"') < page.index('class="rate"')
    assert page.index('class="rate"') < page.index('class="ask"')


def test_thumbs_up_in_the_reader(client, db):
    res = client.post(f"{ITEM}/up?as=rate&next=/", headers=FETCH)
    assert "Useful. Claude will write more like this." in res.text
    assert 'data-rate="undo"' in res.text and "is-rated" in res.text
    assert Ratings(db).get(KEY).up and not _hidden(db)
    assert "Useful. Claude will write more like this." in client.get(f"/i/{KEY}").text
    res = client.post(f"{ITEM}/unrate?as=rate&next=/", headers=FETCH)
    assert "<h2>Was this worth reading?</h2>" in res.text
    assert Ratings(db).get(KEY) is None


def test_thumbs_down_in_the_reader(client, db):
    res = client.post(f"{ITEM}/down?as=rate&next=/", headers=FETCH)
    assert "Removed from your feed. Claude will write fewer like this." in res.text
    assert _hidden(db)
    # From the overlay the page asks for the card's note instead.
    res = client.post(f"{ITEM}/down?as=card&next=/", headers=FETCH)
    assert 'class="story hidden-note"' in res.text


def test_thumbs_up_brings_back_a_hidden_item(client, db):
    client.post(f"{ITEM}/hide?next=/", headers=FETCH)
    assert _hidden(db)
    client.post(f"{ITEM}/up?as=rate&next=/", headers=FETCH)
    assert not _hidden(db)


def test_undo_of_a_thumbs_up_leaves_a_hidden_item_hidden(client, db):
    client.post(f"{ITEM}/up?as=rate&next=/", headers=FETCH)
    client.post(f"{ITEM}/hide?next=/", headers=FETCH)
    client.post(f"{ITEM}/unrate?as=rate&next=/", headers=FETCH)
    assert _hidden(db)


def test_without_js_rating_redirects_back(client, db):
    res = client.post(f"{ITEM}/down?next=/?view=latest", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/?view=latest"
    assert _hidden(db)


def test_saying_why(client, db):
    assert client.post(f"{ITEM}/reasons", data={"reasons": "basic"}).status_code == 409
    client.post(f"{ITEM}/down?as=card&next=/", headers=FETCH)
    res = client.post(f"{ITEM}/reasons", data={"reasons": "source,basic,bogus"})
    assert res.status_code == 200 and res.json() == {"reasons": ["basic", "source"]}
    assert Ratings(db).get(KEY).reasons == ("basic", "source")


def test_rating_refuses_cross_site_and_unknown_items(client, db):
    evil = {"Origin": "https://evil.example"}
    assert client.post(f"{ITEM}/down", headers=evil).status_code == 403
    assert client.post(f"{ITEM}/reasons", headers=evil).status_code == 403
    assert not _hidden(db) and Ratings(db).get(KEY) is None
    assert client.post("/items/insight%3Anope/down", headers=FETCH).status_code == 404
