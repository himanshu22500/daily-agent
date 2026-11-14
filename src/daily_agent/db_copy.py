"""Copy every store's tables from one database into another (issue #111).

Used once to move the live SQLite file into the shared Postgres, and safe to
re-run with ``replace=True`` (the target's store tables are emptied first). Only
tables a store owns are copied; anything else in the source (tables from before
the personal-insights refocus) is reported and left behind.

Rows go through each model's own columns, so values come back typed (and the
schema of an older source is tolerated: a column the source lacks takes the
model default). Row counts are compared after the copy, and Postgres sequences
are moved past the copied ids so new rows don't collide with them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path

from sqlalchemy import Table, func, inspect, select, text
from sqlmodel import SQLModel

from .db import make_engine

# Every module that defines a store table. Importing them registers the tables.
STORE_MODULES = (
    "channel_registry",
    "covers",
    "insights_store",
    "instagram_posts",
    "outbox",
    "prep",
    "questions",
    "ratings",
    "videos",
    "web_feed",
)


class CopyError(RuntimeError):
    pass


@dataclass
class CopyReport:
    copied: dict[str, int] = field(default_factory=dict)
    missing_in_source: list[str] = field(default_factory=list)
    left_behind: list[str] = field(default_factory=list)


def store_tables() -> list[Table]:
    """The store tables, parents before children."""
    for name in STORE_MODULES:
        import_module(f"daily_agent.feed.{name}")
    return list(SQLModel.metadata.sorted_tables)


def postgres_collation(engine) -> str | None:
    """The current database's collation on Postgres; None on other databases."""
    if engine.dialect.name != "postgresql":
        return None
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT datcollate FROM pg_database WHERE datname = current_database()"
            )
        ).scalar()


def table_counts(db: str | Path) -> dict[str, int | None]:
    """Row count per store table; None for a table that doesn't exist yet."""
    engine = make_engine(db)
    present = set(inspect(engine).get_table_names())
    counts: dict[str, int | None] = {}
    with engine.connect() as conn:
        for table in store_tables():
            counts[table.name] = (
                conn.execute(select(func.count()).select_from(table)).scalar_one()
                if table.name in present
                else None
            )
    return counts


def copy_database(
    source: str | Path,
    target: str | Path,
    *,
    replace: bool = False,
    batch_size: int = 500,
) -> CopyReport:
    src = make_engine(source)
    dst = make_engine(target)
    collation = postgres_collation(dst)
    if collation not in (None, "C", "POSIX"):
        raise CopyError(
            f"target database collation is {collation!r}, not 'C'. The stores "
            "compare timestamp strings byte by byte like SQLite does; recreate it "
            "with `createdb -l C -T template0`."
        )

    tables = store_tables()
    SQLModel.metadata.create_all(dst, tables=tables)
    src_inspector = inspect(src)
    src_names = set(src_inspector.get_table_names())
    report = CopyReport(
        left_behind=sorted(src_names - {t.name for t in tables}),
    )

    with dst.begin() as out:
        filled = [
            t.name
            for t in tables
            if out.execute(select(func.count()).select_from(t)).scalar_one()
        ]
        if filled and not replace:
            raise CopyError(
                "target already has rows in: "
                + ", ".join(filled)
                + " (pass replace to empty those tables first)"
            )
        for table in reversed(tables):
            out.execute(table.delete())

        with src.connect() as inp:
            for table in tables:
                if table.name not in src_names:
                    report.missing_in_source.append(table.name)
                    continue
                have = {c["name"] for c in src_inspector.get_columns(table.name)}
                columns = [c for c in table.columns if c.name in have]
                result = inp.execute(select(*columns))
                copied = 0
                while chunk := result.fetchmany(batch_size):
                    out.execute(table.insert(), [dict(row._mapping) for row in chunk])
                    copied += len(chunk)
                report.copied[table.name] = copied

        for name, copied in report.copied.items():
            table = SQLModel.metadata.tables[name]
            landed = out.execute(select(func.count()).select_from(table)).scalar_one()
            if landed != copied:
                raise CopyError(f"{name}: copied {copied} rows but {landed} landed")

        if dst.dialect.name == "postgresql":
            for table in tables:
                pk = table.autoincrement_column
                if pk is None:
                    continue
                out.execute(
                    text(
                        "SELECT setval(pg_get_serial_sequence(:table, :column), "
                        f"COALESCE((SELECT MAX({pk.name}) FROM {table.name}), 0) + 1, "
                        "false)"
                    ),
                    {"table": table.name, "column": pk.name},
                )
    return report
