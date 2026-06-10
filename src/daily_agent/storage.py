"""SQLite-backed store so activity accumulates over time.

Pull requests and commits are upserted (dedup by repo+number / repo+sha), so
running ``collect`` repeatedly is idempotent and the DB becomes a growing
history of everything that's happened across the org.

Persistence is SQLModel over the shared engine (see ``daily_agent.db``); the
public API returns the domain models (:class:`PullRequest` / :class:`Commit`)
unchanged. Timestamps stay ISO strings in TEXT columns — the on-disk schema is
unchanged so existing DBs keep working without a migration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, select

from .db import create_tables, make_engine, session_scope
from .models import Commit, PullRequest, RepoActivity


class PullRequestRow(SQLModel, table=True):
    __tablename__ = "pull_requests"

    repo: str = Field(primary_key=True)
    number: int = Field(primary_key=True)
    title: str
    author: str
    state: str
    merged: int
    created_at: str
    merged_at: str | None = None
    url: str
    body: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    seen_at: str


class CommitRow(SQLModel, table=True):
    __tablename__ = "commits"

    repo: str = Field(primary_key=True)
    sha: str = Field(primary_key=True)
    author: str
    message: str
    date: str
    url: str
    seen_at: str


# Columns refreshed on a PR re-sync; identity/origin columns (repo, number,
# created_at, url, author) are deliberately left untouched on conflict.
_PR_MUTABLE = (
    "title",
    "state",
    "merged",
    "merged_at",
    "body",
    "additions",
    "deletions",
    "changed_files",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._engine = make_engine(self.db_path)
        create_tables(self._engine, PullRequestRow, CommitRow)

    # --- writes ----------------------------------------------------------- #
    def save_activity(self, activity: RepoActivity) -> tuple[int, int]:
        """Upsert all PRs and commits. Returns (#prs, #commits) written."""
        seen = _now_iso()
        with session_scope(self._engine) as session:
            for pr in activity.pull_requests:
                stmt = sqlite_insert(PullRequestRow).values(
                    repo=pr.repo,
                    number=pr.number,
                    title=pr.title,
                    author=pr.author,
                    state=pr.state,
                    merged=int(pr.merged),
                    created_at=pr.created_at.isoformat(),
                    merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
                    url=pr.url,
                    body=pr.body,
                    additions=pr.additions,
                    deletions=pr.deletions,
                    changed_files=pr.changed_files,
                    seen_at=seen,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["repo", "number"],
                    set_={c: getattr(stmt.excluded, c) for c in _PR_MUTABLE},
                )
                session.execute(stmt)
            for c in activity.commits:
                stmt = sqlite_insert(CommitRow).values(
                    repo=c.repo,
                    sha=c.sha,
                    author=c.author,
                    message=c.message,
                    date=c.date.isoformat(),
                    url=c.url,
                    seen_at=seen,
                ).on_conflict_do_nothing(index_elements=["repo", "sha"])
                session.execute(stmt)
        return len(activity.pull_requests), len(activity.commits)

    # --- reads ------------------------------------------------------------ #
    def activity_since(self, since: datetime) -> list[RepoActivity]:
        """Reconstruct per-repo activity for everything on/after ``since``."""
        cutoff = since.isoformat()
        by_repo: dict[str, RepoActivity] = {}
        with session_scope(self._engine) as session:
            pr_rows = session.exec(
                select(PullRequestRow)
                .where(
                    (PullRequestRow.created_at >= cutoff)
                    | (
                        PullRequestRow.merged_at.is_not(None)
                        & (PullRequestRow.merged_at >= cutoff)
                    )
                )
                .order_by(PullRequestRow.created_at)
            ).all()
            for row in pr_rows:
                pr = _to_pr(row)
                by_repo.setdefault(
                    pr.repo, RepoActivity(repo=pr.repo)
                ).pull_requests.append(pr)
            commit_rows = session.exec(
                select(CommitRow)
                .where(CommitRow.date >= cutoff)
                .order_by(CommitRow.date)
            ).all()
            for row in commit_rows:
                c = _to_commit(row)
                by_repo.setdefault(c.repo, RepoActivity(repo=c.repo)).commits.append(c)
        return list(by_repo.values())


def _to_pr(row: PullRequestRow) -> PullRequest:
    return PullRequest(
        repo=row.repo,
        number=row.number,
        title=row.title,
        author=row.author,
        state=row.state,
        merged=bool(row.merged),
        created_at=datetime.fromisoformat(row.created_at),
        merged_at=datetime.fromisoformat(row.merged_at) if row.merged_at else None,
        url=row.url,
        body=row.body,
        additions=row.additions,
        deletions=row.deletions,
        changed_files=row.changed_files,
    )


def _to_commit(row: CommitRow) -> Commit:
    return Commit(
        repo=row.repo,
        sha=row.sha,
        author=row.author,
        message=row.message,
        date=datetime.fromisoformat(row.date),
        url=row.url,
    )
