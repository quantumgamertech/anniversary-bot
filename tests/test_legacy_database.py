import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from legacy_database import Database, create_database_from_env, redact_database_url


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

    def test_schema_initialization(self):
        db = self.make_db()
        tables = {row["name"] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({
            "guild_settings", "install_stats", "install_events", "growth_stats",
            "vote_users", "vote_events", "guild_billing", "billing_events",
        }.issubset(tables))

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


class PostgresAdapterTests(unittest.TestCase):
    def test_postgres_preferred_when_url_present(self):
        with mock.patch("legacy_database.Database._connect", return_value=mock.Mock()), mock.patch("legacy_database.Database._setup"):
            db = Database(sqlite_path="ignored.db", database_url="postgres://user:secret@example/db")
            self.assertEqual(db.backend, "postgres")

    def test_postgres_placeholder_generation(self):
        with mock.patch("legacy_database.Database._connect", return_value=mock.Mock()), mock.patch("legacy_database.Database._setup"):
            db = Database(sqlite_path="ignored.db", database_url="postgres://user:secret@example/db")
            self.assertEqual(db.sql("SELECT * FROM x WHERE a = ? AND b = ?"), "SELECT * FROM x WHERE a = %s AND b = %s")

    def test_postgres_schema_mentions_current_and_legacy_tables(self):
        statements = []

        class FakePostgres(Database):
            def _connect(self):
                return mock.Mock()
            def execute(self, statement, params=()):
                statements.append(self.sql(" ".join(statement.split())))
                cursor = mock.Mock()
                cursor.fetchone.return_value = None
                return cursor
            def _column_exists(self, table_name, column_name):
                return False

        db = FakePostgres(database_url="postgres://user:secret@example/db")
        self.addCleanup(db.close)
        joined = "\n".join(statements)
        for table in ("guild_settings", "anniversary_log", "premium_guilds", "guild_milestones", "growth_stats", "vote_users", "guild_billing"):
            self.assertIn(table, joined)
        self.assertIn("%s", db.sql("SELECT ?"))

    def test_database_url_redacted(self):
        self.assertEqual(redact_database_url("postgres://user:secret@example/db"), "[redacted DATABASE_URL]")


if __name__ == "__main__":
    unittest.main()
