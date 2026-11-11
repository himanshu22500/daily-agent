"""Project names for insights, resolved from Claude Code transcript dir names."""

from __future__ import annotations

from daily_agent.feed.projects import (
    mangle,
    project_label,
    resolve_project_dir,
    session_projects,
)


def test_resolves_hyphenated_names_against_the_filesystem(tmp_path):
    (tmp_path / "home" / "tranzact" / "tranzact-v2").mkdir(parents=True)
    (tmp_path / "home" / "tranzact" / "tranzact").mkdir()
    (tmp_path / "home" / ".config" / "tool").mkdir(parents=True)

    target = tmp_path / "home" / "tranzact" / "tranzact-v2"
    assert resolve_project_dir(mangle(str(target)), tmp_path) == target
    dotted = tmp_path / "home" / ".config" / "tool"
    assert resolve_project_dir(mangle(str(dotted)), tmp_path) == dotted
    assert resolve_project_dir("-somewhere-else", tmp_path) is None


def test_labels_fall_back_when_the_directory_is_gone(tmp_path):
    home = tmp_path / "home"
    (home / "work" / "daily-agent").mkdir(parents=True)
    root, h = str(tmp_path), str(home)
    assert project_label(mangle(str(home / "work" / "daily-agent")), root, h) == (
        "daily-agent"
    )
    assert project_label(mangle(str(home)), root, h) == "home"
    gone = mangle(str(home / "old" / "deleted-proj"))
    assert project_label(gone, root, h) == "deleted-proj"


def test_session_projects_maps_session_ids(tmp_path):
    home = tmp_path / "home"
    (home / "repo-x").mkdir(parents=True)
    projects = tmp_path / "projects" / mangle(str(home / "repo-x"))
    paths = [str(projects / "abc-123.jsonl"), str(projects / "notes.txt")]
    assert session_projects(paths, root=str(tmp_path), home=str(home)) == {
        "abc-123": "repo-x"
    }
