import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from legacy_database import Database, create_database_from_env, redact_database_url
from migrate_sqlite_to_postgres import (
    MigrationSafetyError,
    PostgresReadOnlyInspector,
    build_dry_run_report,
    is_production_like_database_url,
    main as migration_main,
    plan_anniversary_preservation,
    plan_milestone_mapping,
    plan_premium_mapping,
    plan_settings_mapping,
)


class SQLiteFallbackTests(unittest.TestCase):
    def make_db(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
        db = Database(sqlite_path=tmp.name, database_url="")
        self.addCleanup(db.close)
        return db

    def test_sqlite_fallback(self):
        db = create_database_from_env(database_url="", sqlite_path=":memory:")
        self.addCleanup(db.close)
        self.assertEqual(db.backend, "sqlite")

    def test_schema_initialization_is_explicit(self):
        db = Database(sqlite_path=":memory:", database_url="", initialize_schema=False)
        self.addCleanup(db.close)
        tables = list(db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
        self.assertEqual(tables, [])
        db.initialize_schema()
        tables = {row["name"] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("guild_settings", tables)

    def test_premium_and_guild_settings(self):
        db = self.make_db()
        self.assertFalse(db.get_guild_settings(100)["premium"])
        db.set_premium(100, True)
        db.set_report_channel(100, 200)
        db.set_alerts_enabled(100, False)
        db.set_milestone_role(100, 500, 900)
        settings = db.get_guild_settings(100)
        self.assertTrue(settings["premium"])
        self.assertEqual(settings["report_channel_id"], 200)
        self.assertFalse(settings["alerts_enabled"])
        self.assertEqual(settings["milestone_roles"], {500: 900})

    def test_growth_upsert_accumulates(self):
        db = self.make_db()
        db.increment_growth(100, "2026-08-30", joins=2, leaves=1)
        db.increment_growth(100, "2026-08-30", joins=3, leaves=0)
        self.assertEqual(db.get_growth_for_date(100, "2026-08-30"), {"joins": 5, "leaves": 1, "net": 4})

    def test_vote_tracking(self):
        db = self.make_db()
        db.set_vote_user(10, 1, 1, "vote1", "until1")
        db.set_vote_user(10, 2, 2, "vote2", "until2")
        db.record_vote_event(10, "tester", "topgg", True, "vote2", {"ok": True})
        self.assertEqual(db.get_vote_user(10)["total_votes"], 2)
        self.assertEqual(len(db.get_recent_vote_events()), 1)

    def test_billing_upsert_and_events(self):
        db = self.make_db()
        db.upsert_guild_billing(100, 10, "sub", "cust", "order", "Premium", "Monthly", "active", "Active", None, None, "portal", "pay", "created", "checkout", False)
        db.upsert_guild_billing(100, 10, "sub", "cust", "order", "Premium", "Monthly", "cancelled", "Cancelled", None, "future", "portal", "pay", "cancelled", "checkout", False)
        db.record_billing_event("subscription_cancelled", 100, "sub", {"event": "subscription_cancelled"})
        self.assertEqual(db.get_guild_billing(100)["status"], "cancelled")
        self.assertEqual(db.get_guild_billing_by_subscription_id("sub")["guild_id"], 100)
        self.assertEqual(len(db.get_recent_billing_events()), 1)

    def test_transaction_rolls_back_on_failure(self):
        db = self.make_db()
        with self.assertRaises(RuntimeError):
            with db.transaction():
                db.execute("INSERT INTO install_stats (key, value) VALUES (?, ?)", ("rollback_test", 1))
                raise RuntimeError("force rollback")
        row = db.fetchone("SELECT value FROM install_stats WHERE key = ?", ("rollback_test",))
        self.assertIsNone(row)


class PostgresAdapterTests(unittest.TestCase):
    def test_postgres_preferred_when_url_present(self):
        with mock.patch("legacy_database.Database._connect", return_value=mock.Mock()), mock.patch("legacy_database.Database.initialize_schema"):
            db = Database(sqlite_path="ignored.db", database_url="postgres://user:secret@example/db")
            self.assertEqual(db.backend, "postgres")

    def test_postgres_connection_can_skip_schema_initialization(self):
        with mock.patch("legacy_database.Database._connect", return_value=mock.Mock()) as connect, mock.patch("legacy_database.Database.initialize_schema") as init:
            db = Database(sqlite_path="ignored.db", database_url="postgres://user:secret@example/db", initialize_schema=False)
            self.assertEqual(db.backend, "postgres")
            connect.assert_called_once()
            init.assert_not_called()

    def test_postgres_placeholder_generation(self):
        with mock.patch("legacy_database.Database._connect", return_value=mock.Mock()), mock.patch("legacy_database.Database.initialize_schema"):
            db = Database(sqlite_path="ignored.db", database_url="postgres://user:secret@example/db")
            self.assertEqual(db.sql("SELECT * FROM x WHERE a = ? AND b = ?"), "SELECT * FROM x WHERE a = %s AND b = %s")

    def test_database_url_redacted(self):
        self.assertEqual(redact_database_url("postgres://user:secret@example/db"), "[redacted DATABASE_URL]")


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def fetchall(self):
        return self.rows


class FakePostgresConnection:
    def __init__(self):
        self.statements = []
        self.tables = {
            "guild_settings": ["guild_id", "premium", "milestone_roles"],
            "premium_guilds": ["guild_id"],
            "anniversary_log": ["guild_id", "user_id", "anniversary_date"],
        }
        self.counts = {"guild_settings": 1, "premium_guilds": 1, "anniversary_log": 2}
    def execute(self, statement, params=()):
        self.statements.append(statement)
        if "information_schema.tables" in statement:
            return FakeCursor([{"exists": 1}] if params and params[0] in self.tables else [])
        if "information_schema.columns" in statement:
            return FakeCursor([{"column_name": col} for col in self.tables.get(params[0], [])])
        if statement.strip().upper().startswith("SELECT COUNT"):
            table = statement.split("FROM", 1)[1].strip().split()[0]
            return FakeCursor([{"count": self.counts.get(table, 0)}])
        return FakeCursor([])
    def close(self):
        pass


class MigrationSafetyTests(unittest.TestCase):
    def make_sqlite(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, premium INTEGER, milestone_roles TEXT)")
        conn.execute("INSERT INTO guild_settings VALUES (100, 1, '{}')")
        return conn

    def test_dry_run_uses_no_schema_or_data_writes(self):
        pg_conn = FakePostgresConnection()
        inspector = PostgresReadOnlyInspector("postgres://user:secret@example/db", connection=pg_conn)
        report = build_dry_run_report(self.make_sqlite(), inspector)
        self.assertFalse(report["writes_performed"])
        written = "\n".join(pg_conn.statements).upper()
        for keyword in ("CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ", "DROP "):
            self.assertNotIn(keyword, written)

    def test_read_only_inspector_blocks_write_sql(self):
        inspector = PostgresReadOnlyInspector("postgres://user:secret@example/db", connection=FakePostgresConnection())
        with self.assertRaises(MigrationSafetyError):
            inspector.fetchone("CREATE TABLE nope (id int)")

    def test_dry_run_reports_missing_columns(self):
        report = build_dry_run_report(self.make_sqlite(), PostgresReadOnlyInspector("postgres://user:secret@example/db", connection=FakePostgresConnection()))
        self.assertIn("report_channel_id", report["postgres"]["guild_settings"]["missing_columns"])

    def test_init_schema_is_explicit_mode_only(self):
        with mock.patch("migrate_sqlite_to_postgres.PostgresReadOnlyInspector") as inspector, mock.patch("migrate_sqlite_to_postgres.Database") as database:
            inspector.return_value = PostgresReadOnlyInspector("postgres://user:secret@example/db", connection=FakePostgresConnection())
            with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
                sqlite_path = tmp.name
            self.addCleanup(lambda: os.path.exists(sqlite_path) and os.remove(sqlite_path))
            conn = sqlite3.connect(sqlite_path)
            conn.execute("CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY)")
            conn.close()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = migration_main(["--dry-run", "--sqlite-path", sqlite_path, "--database-url", "postgres://user:secret@example/db"])
            self.assertEqual(code, 0)
            database.assert_not_called()

    def test_production_like_url_requires_override_for_write_modes(self):
        self.assertTrue(is_production_like_database_url("postgres://user:pass@legacy-bot.railway.internal/db"))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = migration_main(["--init-schema", "--database-url", "postgres://user:pass@legacy-bot.railway.internal/db"])
        self.assertEqual(code, 2)
        self.assertNotIn("pass", stderr.getvalue())

    def test_apply_requires_explicit_confirmation(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = migration_main(["--apply", "--sqlite-path", "x.db", "--database-url", "postgres://user:secret@example/db"])
        self.assertEqual(code, 2)
        self.assertIn("--yes-i-understand", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())

    def test_premium_legacy_mapping(self):
        actions = plan_premium_mapping(["100", "200"], {"100": {"premium": True}, "200": {"premium": False}})
        self.assertEqual(actions[0]["action"], "preserve")
        self.assertEqual(actions[1]["action"], "set_premium_true")

    def test_settings_conflict_detection(self):
        plans = plan_settings_mapping(
            [{"guild_id": "100", "channel_id": "111", "custom_message": "old"}],
            {"100": {"report_channel_id": "222", "custom_message": "new"}},
        )
        self.assertEqual(len(plans[0]["conflicts"]), 2)

    def test_milestone_unresolved_role_behavior(self):
        plans = plan_milestone_mapping([{"guild_id": "100", "milestone_year": 1, "role_name": "Veteran"}])
        self.assertEqual(plans[0]["status"], "unresolved_role_name")

    def test_anniversary_preservation(self):
        self.assertFalse(plan_anniversary_preservation(5)["discard"])


if __name__ == "__main__":
    unittest.main()
