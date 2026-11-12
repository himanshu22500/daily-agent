"""Headlines: cleanup, first-sentence fallback, export/import of written titles."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from typer.testing import CliRunner

from daily_agent import cli
from daily_agent.feed.headlines import (
    apply_titles,
    clean_headline,
    export_for_titling,
    fallback_headline,
    split_first_sentence,
)
from daily_agent.feed.insights_store import InsightStore
from daily_agent.models import Insight

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def _store(db: str, *texts: str, titled: dict[int, str] | None = None) -> InsightStore:
    store = InsightStore(db)
    for i, text in enumerate(texts):
        store.add(
            Insight(
                key=f"insight:k{i}",
                text=text,
                type="repo",
                tags=["t"],
                captured_at=T0,
                title=(titled or {}).get(i, ""),
            )
        )
    return store


def test_clean_headline_keeps_a_closing_quoted_phrase():
    assert clean_headline('Marques posts a short "Power user PSA"') == (
        'Marques posts a short "Power user PSA"'
    )
    assert clean_headline("“Whole headline in quotes.”") == "Whole headline in quotes"
    assert clean_headline('"Mixed "quotes" inside"') == '"Mixed "quotes" inside"'
    assert clean_headline("'It's fine'") == "It's fine"


def test_clean_headline_strips_markup_quotes_and_period():
    assert clean_headline('  "`uv sync` prunes **pip-installed** tools."  ') == (
        "uv sync prunes pip-installed tools"
    )
    cleaned = clean_headline("word " * 40)
    assert len(cleaned) <= 90 and cleaned.endswith("…")
    assert clean_headline("   ") == ""


def test_plain_text_keeps_identifiers_intact():
    from daily_agent.feed.headlines import plain

    assert plain("`payments_v3` reads **QA_PASSWORD**, QA_* and `__init__`") == (
        "payments_v3 reads QA_PASSWORD, QA_* and __init__"
    )
    assert clean_headline("QA runner reads QA_* vars from tranzact-v2/.env") == (
        "QA runner reads QA_* vars from tranzact-v2/.env"
    )


def test_first_sentence_fallback_and_rest():
    text = (
        "Pinia `persist` stores don't hot-swap. Reload the tab after edits. Then retry."
    )
    assert fallback_headline(text) == "Pinia persist stores don't hot-swap"
    assert split_first_sentence(text)[1] == "Reload the tab after edits. Then retry."
    # Abbreviations like "e.g." followed by lowercase don't end the sentence.
    assert fallback_headline("Use e.g. uv run for scripts. Next.") == (
        "Use e.g. uv run for scripts"
    )
    assert split_first_sentence("Only one sentence here.")[1] == ""


def test_export_lists_untitled_insights_unless_all(tmp_path):
    store = _store(str(tmp_path / "e.db"), "b", "a", titled={1: "Done"})
    assert export_for_titling(store) == [
        {"key": "insight:k0", "text": "b", "type": "repo", "tags": ["t"]}
    ]
    assert [r["key"] for r in export_for_titling(store, include_titled=True)] == [
        "insight:k0",
        "insight:k1",
    ]


def test_apply_titles_cleans_validates_and_keeps_existing(tmp_path):
    store = _store(str(tmp_path / "a.db"), "a", "b", "c", titled={2: "Original"})
    result = apply_titles(
        store,
        {
            "insight:k0": '"Fresh headline."',
            "insight:k1": "   ",
            "insight:k2": "Replacement",
            "insight:ghost": "Nope",
        },
    )
    assert result.applied == 1 and result.kept_existing == 1
    assert result.empty == ["insight:k1"] and result.unknown == ["insight:ghost"]
    assert store.get("insight:k0").title == "Fresh headline"
    assert store.get("insight:k2").title == "Original"
    assert (
        apply_titles(store, {"insight:k2": "Replacement"}, overwrite=True).applied == 1
    )
    assert store.get("insight:k2").title == "Replacement"


def test_store_title_column_is_added_to_old_databases(tmp_path):
    db = str(tmp_path / "old.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE insights (key VARCHAR PRIMARY KEY, text VARCHAR NOT NULL, "
            "type VARCHAR NOT NULL, tags VARCHAR NOT NULL, score FLOAT NOT NULL, "
            "source_session VARCHAR NOT NULL, git_branch VARCHAR NOT NULL, "
            "captured_at VARCHAR NOT NULL, status VARCHAR NOT NULL)"
        )
        conn.execute(
            "INSERT INTO insights VALUES ('insight:x', 't', 'repo', '', 0, 's', "
            "'main', '2026-07-01T10:00:00+00:00', 'queued')"
        )
    store = InsightStore(db)
    assert store.get("insight:x").title == ""
    assert [i.key for i in store.missing_titles()] == ["insight:x"]
    assert store.set_title("insight:x", "A headline")
    assert store.get("insight:x").title == "A headline"
    assert store.set_title("insight:missing", "x") is False


def _cli(monkeypatch, tmp_path, db: str, *args: str):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_AGENT_DB_PATH", db)
    cli.get_settings.cache_clear()
    try:
        return CliRunner().invoke(cli.app, ["insights", *args])
    finally:
        cli.get_settings.cache_clear()


def test_titles_export_then_import_round_trip(monkeypatch, tmp_path):
    db = str(tmp_path / "cli.db")
    _store(db, "alpha", "beta")
    out = tmp_path / "todo.json"
    exported = _cli(monkeypatch, tmp_path, db, "titles-export", "--out", str(out))
    assert exported.exit_code == 0, exported.output
    records = json.loads(out.read_text())
    assert [r["key"] for r in records] == ["insight:k0", "insight:k1"]

    titles = tmp_path / "titles.json"
    titles.write_text(
        json.dumps({"insight:k0": "Alpha headline", "insight:zzz": "Unknown"})
    )
    imported = _cli(monkeypatch, tmp_path, db, "titles-import", str(titles))
    assert imported.exit_code == 0, imported.output
    assert "Stored 1 headline(s)" in imported.output and "1 unknown" in imported.output
    assert InsightStore(db).get("insight:k0").title == "Alpha headline"

    remaining = _cli(monkeypatch, tmp_path, db, "titles-export")
    assert [r["key"] for r in json.loads(remaining.output)] == ["insight:k1"]


def test_titles_import_rejects_bad_files(monkeypatch, tmp_path):
    db = str(tmp_path / "bad.db")
    _store(db, "alpha")
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    assert _cli(monkeypatch, tmp_path, db, "titles-import", str(bad)).exit_code == 1
    bad.write_text("{not json")
    assert _cli(monkeypatch, tmp_path, db, "titles-import", str(bad)).exit_code == 1
