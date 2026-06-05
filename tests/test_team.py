"""Team mapping load + resolution (offline). Uses fake placeholder identities."""

from __future__ import annotations

import json

from daily_agent.team import load_team, resolve_member

# Fake data only — never put real teammate names/handles (PII) in the repo.
_TEAM = {
    "_comment": "ignored",
    "Jane Doe": {"huly": "Jane Doe", "github": "janedoe"},
    "John Smith": {"huly": "John Smith", "github": "jsmith-corp"},
}


def _write(tmp_path):
    p = tmp_path / "team.json"
    p.write_text(json.dumps(_TEAM))
    return load_team(p)


def test_load_skips_comment_keys(tmp_path):
    team = _write(tmp_path)
    assert set(team) == {"Jane Doe", "John Smith"}


def test_missing_file_is_empty(tmp_path):
    assert load_team(tmp_path / "nope.json") == {}


def test_resolve_by_github_login(tmp_path):
    team = _write(tmp_path)
    assert resolve_member(team, "jsmith-corp").name == "John Smith"


def test_resolve_by_substring(tmp_path):
    team = _write(tmp_path)
    assert resolve_member(team, "jane").github == "janedoe"


def test_resolve_me(tmp_path):
    team = _write(tmp_path)
    assert resolve_member(team, "me", me="Jane Doe").github == "janedoe"


def test_resolve_me_without_config_returns_none(tmp_path):
    team = _write(tmp_path)
    assert resolve_member(team, "me", me="") is None


def test_unknown_returns_none(tmp_path):
    team = _write(tmp_path)
    assert resolve_member(team, "nobody") is None
