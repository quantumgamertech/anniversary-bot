import argparse
import json
import sqlite3
import sys
from contextlib import closing
from typing import Iterable
from urllib.parse import urlsplit

from legacy_database import Database, redact_database_url

CURRENT_TABLES = {
    "guild_settings": ("guild_id", "premium", "milestone_roles", "joined_at", "report_channel_id", "last_daily_report_date", "growth_alert_threshold", "last_alert_net", "alerts_enabled", "vote_reward_role_id"),
    "install_stats": ("key", "value"),
    "install_events": ("id", "guild_id", "guild_name", "event_type", "member_count", "timestamp"),
    "growth_stats": ("guild_id", "date", "joins", "leaves"),
    "vote_users": ("user_id", "total_votes", "streak", "last_vote_at", "premium_until", "last_vote_source", "updated_at"),
    "vote_events": ("id", "user_id", "username", "source", "is_weekend", "voted_at", "raw_payload"),
    "guild_billing": ("guild_id", "discord_user_id", "lemonsqueezy_subscription_id", "lemonsqueezy_customer_id", "order_id", "product_name", "variant_name", "status", "status_formatted", "renews_at", "ends_at", "customer_portal_url", "update_payment_url", "last_event_name", "checkout_url", "test_mode", "created_at", "updated_at"),
    "billing_events": ("id", "event_name", "guild_id", "subscription_id", "created_at", "raw_payload"),
}

LEGACY_POSTGRES_TABLES = {
    "anniversary_log": ("guild_id", "user_id", "anniversary_date"),
    "guild_milestones": ("guild_id", "milestone_year", "role_name"),
    "premium_guilds": ("guild_id",),
}

WRITE_KEYWORDS = ("CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE ", "VACUUM ")


class MigrationSafetyError(RuntimeError):
    pass


def is_production_like_database_url(database_url: str) -> bool:
    parsed = urlsplit(database_url or "")
    host = (parsed.hostname or "").lower()
    raw = (database_url or "").lower()
    return "legacy-bot" in raw or "railway.internal" in host or "proxy.rlwy.net" in host


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1", (table,)).fetchone()
    return row is not None


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not sqlite_table_exists(conn, table):
        return []
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def sqlite_count(conn: sqlite3.Connection, table: str) -> int | None:
    if not sqlite_table_exists(conn, table):
        return None
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class PostgresReadOnlyInspector:
    def __init__(self, database_url: str, connection=None):
        self.database_url = database_url
        self._external_connection = connection is not None
        if connection is not None:
            self.conn = connection
        else:
            import psycopg
            from psycopg.rows import dict_row
            self.conn = psycopg.connect(database_url, row_factory=dict_row)

    def close(self):
        if not self._external_connection:
            self.conn.close()

    def fetchone(self, statement: str, params=()):
        self._assert_read_only_statement(statement)
        return self.conn.execute(statement, params).fetchone()

    def fetchall(self, statement: str, params=()):
        self._assert_read_only_statement(statement)
        return self.conn.execute(statement, params).fetchall()

    def _assert_read_only_statement(self, statement: str):
        normalized = " ".join(statement.upper().split()) + " "
        if any(keyword in normalized for keyword in WRITE_KEYWORDS):
            raise MigrationSafetyError(f"Dry-run attempted write SQL: {statement.split()[0]}")

    def table_exists(self, table: str) -> bool:
        row = self.fetchone(
            """
            SELECT 1 AS exists
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
            LIMIT 1
            """,
            (table,),
        )
        return row is not None

    def columns(self, table: str) -> list[str]:
        rows = self.fetchall(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [row["column_name"] for row in rows]

    def count(self, table: str) -> int | None:
        if not self.table_exists(table):
            return None
        row = self.fetchone(f"SELECT COUNT(*) AS count FROM {table}")
        return int(row["count"] or 0)

    def sample_rows(self, table: str, columns: tuple[str, ...], limit: int = 100) -> list[dict]:
        if not self.table_exists(table):
            return []
        safe_columns = ", ".join(columns)
        rows = self.fetchall(f"SELECT {safe_columns} FROM {table} LIMIT %s", (limit,))
        return [dict(row) for row in rows]


def inspect_sqlite(conn: sqlite3.Connection) -> dict:
    tables = {}
    for table, expected_columns in CURRENT_TABLES.items():
        existing_columns = sqlite_columns(conn, table)
        tables[table] = {
            "exists": bool(existing_columns),
            "rows": sqlite_count(conn, table),
            "columns": existing_columns,
            "missing_columns": [col for col in expected_columns if col not in existing_columns],
        }
    return tables


def inspect_postgres(inspector: PostgresReadOnlyInspector) -> dict:
    tables = {}
    for table, expected_columns in {**CURRENT_TABLES, **LEGACY_POSTGRES_TABLES}.items():
        exists = inspector.table_exists(table)
        columns = inspector.columns(table) if exists else []
        tables[table] = {
            "exists": exists,
            "rows": inspector.count(table) if exists else None,
            "columns": columns,
            "missing_columns": [col for col in expected_columns if col not in columns],
        }
    return tables


def classify_conflict(field: str, sqlite_value, postgres_value) -> dict | None:
    if sqlite_value in (None, "") or sqlite_value == postgres_value:
        return None
    if postgres_value in (None, ""):
        return {"field": field, "sqlite_value": sqlite_value, "postgres_value": postgres_value, "classification": "postgres_empty", "proposed_resolution": "copy_sqlite_value_in_apply_mode"}
    return {"field": field, "sqlite_value": sqlite_value, "postgres_value": postgres_value, "classification": "both_sources_have_values", "proposed_resolution": "manual_review_required"}


def plan_premium_mapping(premium_guild_ids: Iterable[str], canonical_settings: dict[str, dict]) -> list[dict]:
    actions = []
    for guild_id in sorted({str(g) for g in premium_guild_ids}):
        current = canonical_settings.get(guild_id, {})
        if bool(current.get("premium")):
            actions.append({"guild_id": guild_id, "action": "preserve", "reason": "already_premium"})
        else:
            actions.append({"guild_id": guild_id, "action": "set_premium_true", "source": "premium_guilds", "requires_apply": True})
    return actions


def plan_settings_mapping(legacy_rows: Iterable[dict], canonical_settings: dict[str, dict]) -> list[dict]:
    plans = []
    for row in legacy_rows:
        guild_id = str(row.get("guild_id"))
        canonical = canonical_settings.get(guild_id, {})
        conflicts = []
        mappings = []
        for legacy_field, canonical_field in (("channel_id", "report_channel_id"), ("custom_message", "custom_message")):
            conflict = classify_conflict(canonical_field, row.get(legacy_field), canonical.get(canonical_field))
            if conflict and conflict["classification"] == "both_sources_have_values":
                conflicts.append(conflict)
            elif conflict:
                mappings.append(conflict)
        plans.append({"guild_id": guild_id, "mappings": mappings, "conflicts": conflicts})
    return plans


def plan_milestone_mapping(legacy_rows: Iterable[dict], role_name_to_id: dict[str, int] | None = None) -> list[dict]:
    role_name_to_id = role_name_to_id or {}
    plans = []
    for row in legacy_rows:
        role_name = row.get("role_name")
        role_id = role_name_to_id.get(role_name)
        plans.append({
            "guild_id": str(row.get("guild_id")),
            "milestone_year": row.get("milestone_year"),
            "role_name": role_name,
            "mapped_role_id": role_id,
            "status": "mapped" if role_id else "unresolved_role_name",
            "proposed_resolution": "write_to_milestone_roles_in_apply_mode" if role_id else "manual_role_id_review_required",
        })
    return plans


def plan_anniversary_preservation(row_count: int | None) -> dict:
    return {"table": "anniversary_log", "rows": row_count, "action": "preserve_historical_data", "discard": False}


def build_dry_run_report(sqlite_conn: sqlite3.Connection, pg: PostgresReadOnlyInspector) -> dict:
    sqlite_tables = inspect_sqlite(sqlite_conn)
    postgres_tables = inspect_postgres(pg)
    return {
        "mode": "dry-run",
        "writes_performed": False,
        "sqlite": sqlite_tables,
        "postgres": postgres_tables,
        "legacy_mappings": {
            "premium": "premium_guilds.guild_id -> guild_settings.premium true; apply mode only",
            "settings": "legacy channel_id/custom_message preserve canonical values; conflicts require review",
            "milestones": "role_name rows are preserved; role IDs are never guessed",
            "anniversary_log": plan_anniversary_preservation(postgres_tables.get("anniversary_log", {}).get("rows")),
        },
    }


def init_schema(database_url: str, allow_production_url: bool) -> int:
    if is_production_like_database_url(database_url) and not allow_production_url:
        raise MigrationSafetyError("Refusing production-looking DATABASE_URL without --allow-production-url.")
    db = Database(database_url=database_url, initialize_schema=True)
    db.close()
    return 0


def apply_migration(database_url: str, sqlite_path: str, allow_production_url: bool, yes: bool) -> int:
    if not yes:
        raise MigrationSafetyError("--apply requires --yes-i-understand.")
    if is_production_like_database_url(database_url) and not allow_production_url:
        raise MigrationSafetyError("Refusing production-looking DATABASE_URL without --allow-production-url.")
    raise MigrationSafetyError("Apply mode is intentionally blocked until a reviewed write plan is approved.")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Legacy Bot SQLite/PostgreSQL migration safety tool.")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true", help="Inspect only. Performs no schema or data writes.")
    modes.add_argument("--init-schema", action="store_true", help="Explicitly initialize/upgrade destination PostgreSQL schema.")
    modes.add_argument("--apply", action="store_true", help="Explicit data migration mode. Disabled until write plan approval.")
    parser.add_argument("--sqlite-path", help="Source SQLite database path")
    parser.add_argument("--database-url", required=True, help="Destination PostgreSQL DATABASE_URL")
    parser.add_argument("--allow-production-url", action="store_true", help="Allow production-looking DATABASE_URL values")
    parser.add_argument("--yes-i-understand", action="store_true", help="Required for --apply")
    args = parser.parse_args(argv)

    try:
        if args.dry_run:
            if not args.sqlite_path:
                raise MigrationSafetyError("--dry-run requires --sqlite-path.")
            print("Legacy Bot migration dry run")
            print(f"SQLite source: {args.sqlite_path}")
            print(f"PostgreSQL destination: {redact_database_url(args.database_url)}")
            with closing(sqlite3.connect(args.sqlite_path)) as sqlite_conn:
                sqlite_conn.row_factory = sqlite3.Row
                inspector = PostgresReadOnlyInspector(args.database_url)
                try:
                    print(json.dumps(build_dry_run_report(sqlite_conn, inspector), indent=2, sort_keys=True))
                    return 0
                finally:
                    inspector.close()
        if args.init_schema:
            return init_schema(args.database_url, args.allow_production_url)
        if args.apply:
            if not args.sqlite_path:
                raise MigrationSafetyError("--apply requires --sqlite-path.")
            return apply_migration(args.database_url, args.sqlite_path, args.allow_production_url, args.yes_i_understand)
    except MigrationSafetyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
