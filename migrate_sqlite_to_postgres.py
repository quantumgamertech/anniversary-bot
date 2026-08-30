import argparse
import json
import sqlite3
import sys
from contextlib import closing
from typing import Iterable

from legacy_database import Database, redact_database_url

CURRENT_TABLES = (
    "guild_settings",
    "install_stats",
    "install_events",
    "growth_stats",
    "vote_users",
    "vote_events",
    "guild_billing",
    "billing_events",
)

LEGACY_POSTGRES_TABLES = (
    "anniversary_log",
    "guild_milestones",
    "premium_guilds",
)


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def sqlite_count(conn: sqlite3.Connection, table: str) -> int | None:
    if not sqlite_table_exists(conn, table):
        return None
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def postgres_count(db: Database, table: str) -> int | None:
    try:
        row = db.row_dict(db.fetchone(f"SELECT COUNT(*) AS count FROM {table}"))
        return int(row["count"] or 0)
    except Exception:
        db.conn.rollback()
        return None


def report_counts(sqlite_conn: sqlite3.Connection, pg_db: Database) -> dict:
    current = {}
    for table in CURRENT_TABLES:
        current[table] = {
            "sqlite_rows": sqlite_count(sqlite_conn, table),
            "postgres_rows": postgres_count(pg_db, table),
        }
    legacy = {table: postgres_count(pg_db, table) for table in LEGACY_POSTGRES_TABLES}
    return {"current_tables": current, "legacy_postgres_tables": legacy}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run Legacy Bot SQLite to PostgreSQL migration planner.")
    parser.add_argument("--sqlite-path", required=True, help="Source SQLite database path")
    parser.add_argument("--database-url", required=True, help="Destination PostgreSQL DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing data")
    args = parser.parse_args(argv)

    if not args.dry_run:
        print("Refusing to run without --dry-run. Write migration is intentionally not implemented in Phase 1.", file=sys.stderr)
        return 2

    print("Legacy Bot migration dry run")
    print(f"SQLite source: {args.sqlite_path}")
    print(f"PostgreSQL destination: {redact_database_url(args.database_url)}")

    with closing(sqlite3.connect(args.sqlite_path)) as sqlite_conn:
        sqlite_conn.row_factory = sqlite3.Row
        pg_db = Database(database_url=args.database_url)
        try:
            print(json.dumps(report_counts(sqlite_conn, pg_db), indent=2, sort_keys=True))
            print("No writes were performed.")
            return 0
        finally:
            pg_db.close()


if __name__ == "__main__":
    raise SystemExit(main())
