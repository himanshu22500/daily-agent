"""Insight capture substrate: store, transcript parsing, marker lane (offline)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from daily_agent.feed.insights_capture import (
    canonical_key,
    collect_marked,
    extract_marked,
)
from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.transcripts import TranscriptMessage, parse_line
from daily_agent.models import Insight


# --- fixtures -------------------------------------------------------------- #
def _user(text):
    return json.dumps(
        {
            "type": "user",
            "message": {"content": text},
            "sessionId": "s1",
            "gitBranch": "main",
            "timestamp": "2026-06-30T10:00:00Z",
        }
    )


def _tool_result():
    return json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result"}]},
            "sessionId": "s1",
        }
    )


def _assistant(text):
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "text", "text": text},
                    {"type": "tool_use", "name": "Bash"},
                ]
            },
            "sessionId": "s1",
            "gitBranch": "main",
            "timestamp": "2026-06-30T10:00:01Z",
        }
    )


def _insight(key="insight:abc", text="x", score=0.0):
    return Insight(
        key=key, text=text, score=score, captured_at=datetime.now(timezone.utc)
    )


# --- transcript parsing ---------------------------------------------------- #
def test_parse_user_typed_string():
    msg = parse_line(_user("hello there"))
    assert msg is not None and msg.role == "user" and msg.text == "hello there"
    assert msg.session_id == "s1" and msg.git_branch == "main"


def test_parse_skips_tool_result_user_records():
    assert parse_line(_tool_result()) is None  # array content, not typed text


def test_parse_assistant_joins_text_blocks_only():
    msg = parse_line(_assistant("the reply"))
    assert msg is not None and msg.role == "assistant" and msg.text == "the reply"


def test_parse_assistant_without_text_block_is_none():
    rec = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "x"}]},
        }
    )
    assert parse_line(rec) is None


def test_parse_skips_metadata_and_garbage():
    assert parse_line(json.dumps({"type": "ai-title", "title": "x"})) is None
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("not json{") is None


# --- marker lane ----------------------------------------------------------- #
def test_extract_marked_captures_text_after_marker():
    msg = TranscriptMessage(
        "s1", "main", "2026-06-30T10:00:00Z", "user", "insight: use yarn"
    )
    ins = extract_marked(msg, "insight:")
    assert ins is not None
    assert ins.text == "use yarn"
    assert ins.score == 1.0 and ins.tags == ["marked"] and ins.type == "general"
    assert ins.source_session == "s1" and ins.git_branch == "main"


def test_extract_marked_is_case_insensitive():
    msg = TranscriptMessage(
        "s1", "", "", "user", "Some note. INSIGHT: cache is permanent"
    )
    ins = extract_marked(msg, "insight:")
    assert ins is not None and ins.text == "cache is permanent"


def test_extract_marked_none_when_absent_or_empty():
    base = TranscriptMessage("s1", "", "", "user", "no marker here")
    assert extract_marked(base, "insight:") is None
    empty = TranscriptMessage("s1", "", "", "user", "insight:   ")
    assert extract_marked(empty, "insight:") is None


def test_extract_marked_ignores_assistant_messages():
    msg = TranscriptMessage("s1", "", "", "assistant", "insight: not from the user")
    assert extract_marked(msg, "insight:") is None


def test_canonical_key_normalizes_for_dedup():
    assert canonical_key("Use   YARN") == canonical_key("use yarn")
    assert canonical_key("a") != canonical_key("b")


# --- store ----------------------------------------------------------------- #
def test_store_add_dedups_on_key(tmp_path):
    store = InsightStore(tmp_path / "db.sqlite")
    assert store.add(_insight(key="k1", text="first")) is True
    assert store.add(_insight(key="k1", text="dup")) is False  # exact-key dedup
    got = store.get("k1")
    assert got is not None and got.text == "first"  # first capture wins


def test_store_all_ranked_by_score(tmp_path):
    store = InsightStore(tmp_path / "db.sqlite")
    store.add(_insight(key="low", score=0.2))
    store.add(_insight(key="high", score=0.9))
    assert [i.key for i in store.all()] == ["high", "low"]


def test_store_cursor_roundtrip(tmp_path):
    store = InsightStore(tmp_path / "db.sqlite")
    assert store.cursor("f.jsonl") == 0
    store.set_cursor("f.jsonl", 5)
    assert store.cursor("f.jsonl") == 5
    store.set_cursor("f.jsonl", 9)  # upsert
    assert store.cursor("f.jsonl") == 9


# --- collect (end to end over a fixture transcript) ----------------------- #
def test_collect_marked_end_to_end(tmp_path):
    tx = tmp_path / "proj"
    tx.mkdir()
    f = tx / "session.jsonl"
    f.write_text(
        "\n".join(
            [
                _user("insight: the bridge needs yarn not npm"),
                _user("just a normal question"),
                _tool_result(),
                _assistant("insight: this should NOT be captured (assistant)"),
                json.dumps({"type": "ai-title", "title": "x"}),
            ]
        )
    )
    store = InsightStore(tmp_path / "db.sqlite")

    new, scanned = collect_marked(store, tx, "insight:")
    assert new == 1 and scanned == 5
    items = store.all()
    assert len(items) == 1 and items[0].text == "the bridge needs yarn not npm"
    assert store.cursor(str(f)) == 5

    # Re-run: watermark skips everything already read -> nothing new.
    assert collect_marked(store, tx, "insight:") == (0, 0)


def test_collect_marked_incremental_and_dedup(tmp_path):
    tx = tmp_path / "proj"
    tx.mkdir()
    f = tx / "session.jsonl"
    f.write_text(_user("insight: first one"))
    store = InsightStore(tmp_path / "db.sqlite")
    assert collect_marked(store, tx, "insight:")[0] == 1

    # Append a brand-new marked line -> captured on the next run.
    f.write_text(f.read_text() + "\n" + _user("insight: second one"))
    assert collect_marked(store, tx, "insight:")[0] == 1
    assert len(store.all()) == 2

    # Append the SAME insight text again -> read, but exact-key dedup drops it.
    f.write_text(f.read_text() + "\n" + _user("insight:  first   one "))
    new, scanned = collect_marked(store, tx, "insight:")
    assert new == 0 and scanned == 1
    assert len(store.all()) == 2
