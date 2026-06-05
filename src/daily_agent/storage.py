"""SQLite-backed store so activity accumulates over time.

Pull requests and commits are upserted (dedup by repo+number / repo+sha), so
running ``collect`` repeatedly is idempotent and the DB becomes a growing
history of everything that's happened across the org.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Commit, PullRequest, RepoActivity

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_requests (
    repo         TEXT    NOT NULL,
    number       INTEGER NOT NULL,
    title        TEXT    NOT NULL,
    author       TEXT    NOT NULL,
    state        TEXT    NOT NULL,
    merged       INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    merged_at    TEXT,
    url          TEXT    NOT NULL,
    body         TEXT    NOT NULL DEFAULT '',
    additions    INTEGER NOT NULL DEFAULT 0,
    deletions    INTEGER NOT NULL DEFAULT 0,
    changed_files INTEGER NOT NULL DEFAULT 0,
    seen_at      TEXT    NOT NULL,
    PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS commits (
    repo     TEXT NOT NULL,
    sha      TEXT NOT NULL,
    author   TEXT NOT NULL,
    message  TEXT NOT NULL,
    date     TEXT NOT NULL,
    url      TEXT NOT NULL,
    seen_at  TEXT NOT NULL,
    PRIMARY KEY (repo, sha)
);

CREATE INDEX IF NOT EXISTS idx_pr_merged_at ON pull_requests (merged_at);
CREATE INDEX IF NOT EXISTS idx_commit_date ON commits (date);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- writes ----------------------------------------------------------- #
    def save_activity(self, activity: RepoActivity) -> tuple[int, int]:
        """Upsert all PRs and commits. Returns (#prs, #commits) written."""
        seen = _now_iso()
        with self._conn() as conn:
            for pr in activity.pull_requests:
                conn.execute(
                    """
                    INSERT INTO pull_requests
                      (repo, number, title, author, state, merged, created_at,
                       merged_at, url, body, additions, deletions, changed_files, seen_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(repo, number) DO UPDATE SET
                      title=excluded.title, state=excluded.state,
                      merged=excluded.merged, merged_at=excluded.merged_at,
                      body=excluded.body, additions=excluded.additions,
                      deletions=excluded.deletions,
                      changed_files=excluded.changed_files
                    """,
                    (
                        pr.repo, pr.number, pr.title, pr.author, pr.state,
                        int(pr.merged), pr.created_at.isoformat(),
                        pr.merged_at.isoformat() if pr.merged_at else None,
                        pr.url, pr.body, pr.additions, pr.deletions,
                        pr.changed_files, seen,
                    ),
                )
            for c in activity.commits:
                conn.execute(
                    """
                    INSERT INTO commits (repo, sha, author, message, date, url, seen_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(repo, sha) DO NOTHING
                    """,
                    (c.repo, c.sha, c.author, c.message, c.date.isoformat(),
                     c.url, seen),
                )
        return len(activity.pull_requests), len(activity.commits)

    # --- reads ------------------------------------------------------------ #
    def activity_since(self, since: datetime) -> list[RepoActivity]:
        """Reconstruct per-repo activity for everything on/after ``since``."""
        cutoff = since.isoformat()
        by_repo: dict[str, RepoActivity] = {}
        with self._conn() as conn:
            for row in conn.execute(
                "SELECT * FROM pull_requests WHERE created_at >= ? OR "
                "(merged_at IS NOT NULL AND merged_at >= ?) ORDER BY created_at",
                (cutoff, cutoff),
            ):
                pr = _row_to_pr(row)
                by_repo.setdefault(pr.repo, RepoActivity(repo=pr.repo)).pull_requests.append(pr)
            for row in conn.execute(
                "SELECT * FROM commits WHERE date >= ? ORDER BY date", (cutoff,)
            ):
                c = _row_to_commit(row)
                by_repo.setdefault(c.repo, RepoActivity(repo=c.repo)).commits.append(c)
        return list(by_repo.values())


def _row_to_pr(row: sqlite3.Row) -> PullRequest:
    return PullRequest(
        repo=row["repo"], number=row["number"], title=row["title"],
        author=row["author"], state=row["state"], merged=bool(row["merged"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        merged_at=datetime.fromisoformat(row["merged_at"]) if row["merged_at"] else None,
        url=row["url"], body=row["body"], additions=row["additions"],
        deletions=row["deletions"], changed_files=row["changed_files"],
    )


def _row_to_commit(row: sqlite3.Row) -> Commit:
    return Commit(
        repo=row["repo"], sha=row["sha"], author=row["author"],
        message=row["message"], date=datetime.fromisoformat(row["date"]),
        url=row["url"],
    )
