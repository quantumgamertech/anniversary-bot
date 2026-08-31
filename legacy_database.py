import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

UTC = timezone.utc

LEGACY_POSTGRES_TABLES = ("anniversary_log", "guild_milestones", "premium_guilds")

CURRENT_GUILD_COLUMNS_POSTGRES = {
    "channel_id": "TEXT",
    "custom_message": "TEXT",
    "premium": "BOOLEAN NOT NULL DEFAULT FALSE",
    "milestone_roles": "TEXT NOT NULL DEFAULT '{}'",
    "joined_at": "TEXT",
    "report_channel_id": "TEXT",
    "last_daily_report_date": "TEXT",
    "growth_alert_threshold": "INTEGER NOT NULL DEFAULT 25",
    "last_alert_net": "INTEGER",
    "alerts_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
    "vote_reward_role_id": "TEXT",
}

CURRENT_GUILD_COLUMNS_SQLITE = {
    "premium": "INTEGER NOT NULL DEFAULT 0",
    "milestone_roles": "TEXT NOT NULL DEFAULT '{}'",
    "joined_at": "TEXT",
    "report_channel_id": "INTEGER",
    "last_daily_report_date": "TEXT",
    "growth_alert_threshold": "INTEGER NOT NULL DEFAULT 25",
    "last_alert_net": "INTEGER",
    "alerts_enabled": "INTEGER NOT NULL DEFAULT 1",
    "vote_reward_role_id": "INTEGER",
}


class Database:
    def __init__(self, sqlite_path: str = "legacy_bot.db", database_url: str = "", initialize_schema: bool = True):
        self.sqlite_path = sqlite_path
        self.database_url = (database_url or "").strip()
        self.backend = "postgres" if self.database_url else "sqlite"
        self.conn = self._connect()
        if initialize_schema:
            self.initialize_schema()

    def _connect(self):
        if self.backend == "postgres":
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError("DATABASE_URL is set, but psycopg[binary] is not installed.") from exc
            return psycopg.connect(self.database_url, row_factory=dict_row)
        conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self):
        self.conn.close()

    def sql(self, statement: str) -> str:
        return statement.replace("?", "%s") if self.backend == "postgres" else statement

    def db_bool(self, value: bool):
        return bool(value) if self.backend == "postgres" else (1 if value else 0)

    def db_guild_id(self, guild_id: int):
        return str(guild_id) if self.backend == "postgres" else guild_id

    def db_discord_id(self, discord_id: Optional[int]):
        if discord_id is None:
            return None
        return str(discord_id) if self.backend == "postgres" else discord_id

    def row_dict(self, row):
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        return dict(row)

    @contextmanager
    def transaction(self):
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def execute(self, statement: str, params=()):
        return self.conn.execute(self.sql(statement), params)

    def fetchone(self, statement: str, params=()):
        return self.execute(statement, params).fetchone()

    def fetchall(self, statement: str, params=()):
        return self.execute(statement, params).fetchall()

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        if self.backend == "postgres":
            row = self.fetchone(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ? AND column_name = ?
                LIMIT 1
                """,
                (table_name, column_name),
            )
            return row is not None
        rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(row["name"] == column_name for row in rows)

    def _add_column_if_missing(self, table_name: str, column_name: str, definition: str):
        if not self._column_exists(table_name, column_name):
            with self.transaction():
                self.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def initialize_schema(self):
        if self.backend == "postgres":
            self._setup_postgres()
        else:
            self._setup_sqlite()
        self._ensure_stat("join_count", 0)
        self._ensure_stat("remove_count", 0)
        self._ensure_stat("topgg_votes_total", 0)

    def _setup_sqlite(self):
        with self.transaction():
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    premium INTEGER NOT NULL DEFAULT 0,
                    milestone_roles TEXT NOT NULL DEFAULT '{}',
                    joined_at TEXT,
                    report_channel_id INTEGER,
                    last_daily_report_date TEXT,
                    growth_alert_threshold INTEGER NOT NULL DEFAULT 25,
                    last_alert_net INTEGER,
                    alerts_enabled INTEGER NOT NULL DEFAULT 1,
                    vote_reward_role_id INTEGER
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS install_stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS install_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    guild_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    member_count INTEGER NOT NULL DEFAULT 0,
                    timestamp TEXT NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS growth_stats (
                    guild_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    joins INTEGER NOT NULL DEFAULT 0,
                    leaves INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, date)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS vote_users (
                    user_id INTEGER PRIMARY KEY,
                    total_votes INTEGER NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0,
                    last_vote_at TEXT,
                    premium_until TEXT,
                    last_vote_source TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS vote_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    source TEXT NOT NULL DEFAULT 'topgg',
                    is_weekend INTEGER NOT NULL DEFAULT 0,
                    voted_at TEXT NOT NULL,
                    raw_payload TEXT
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_billing (
                    guild_id INTEGER PRIMARY KEY,
                    discord_user_id INTEGER,
                    lemonsqueezy_subscription_id TEXT,
                    lemonsqueezy_customer_id TEXT,
                    order_id TEXT,
                    product_name TEXT,
                    variant_name TEXT,
                    status TEXT,
                    status_formatted TEXT,
                    renews_at TEXT,
                    ends_at TEXT,
                    customer_portal_url TEXT,
                    update_payment_url TEXT,
                    last_event_name TEXT,
                    checkout_url TEXT,
                    test_mode INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS billing_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_name TEXT NOT NULL,
                    guild_id INTEGER,
                    subscription_id TEXT,
                    created_at TEXT NOT NULL,
                    raw_payload TEXT
                )
            """)
        for column, definition in CURRENT_GUILD_COLUMNS_SQLITE.items():
            self._add_column_if_missing("guild_settings", column, definition)

    def _setup_postgres(self):
        with self.transaction():
            self.execute("CREATE TABLE IF NOT EXISTS guild_settings (guild_id TEXT PRIMARY KEY)")
            self.execute("""
                CREATE TABLE IF NOT EXISTS anniversary_log (
                    guild_id TEXT,
                    user_id TEXT,
                    anniversary_date TEXT,
                    PRIMARY KEY (guild_id, user_id, anniversary_date)
                )
            """)
            self.execute("CREATE TABLE IF NOT EXISTS premium_guilds (guild_id TEXT PRIMARY KEY)")
            self.execute("""
                CREATE TABLE IF NOT EXISTS guild_milestones (
                    guild_id TEXT,
                    milestone_year INTEGER,
                    role_name TEXT,
                    PRIMARY KEY (guild_id, milestone_year)
                )
            """)
            self.execute("CREATE TABLE IF NOT EXISTS install_stats (key TEXT PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0)")
            self.execute("""
                CREATE TABLE IF NOT EXISTS install_events (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    guild_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    member_count INTEGER NOT NULL DEFAULT 0,
                    timestamp TEXT NOT NULL
                )
            """)
            self.execute("""
                CREATE TABLE IF NOT EXISTS growth_stats (
                    guild_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    joins INTEGER NOT NULL DEFAULT 0,
                    leaves INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, date)
                )
            """)
            self.execute("""
                CREATE TABLE IF NOT EXISTS vote_users (
                    user_id BIGINT PRIMARY KEY,
                    total_votes INTEGER NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0,
                    last_vote_at TEXT,
                    premium_until TEXT,
                    last_vote_source TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            self.execute("""
                CREATE TABLE IF NOT EXISTS vote_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    source TEXT NOT NULL DEFAULT 'topgg',
                    is_weekend BOOLEAN NOT NULL DEFAULT FALSE,
                    voted_at TEXT NOT NULL,
                    raw_payload TEXT
                )
            """)
            self.execute("""
                CREATE TABLE IF NOT EXISTS guild_billing (
                    guild_id TEXT PRIMARY KEY,
                    discord_user_id BIGINT,
                    lemonsqueezy_subscription_id TEXT,
                    lemonsqueezy_customer_id TEXT,
                    order_id TEXT,
                    product_name TEXT,
                    variant_name TEXT,
                    status TEXT,
                    status_formatted TEXT,
                    renews_at TEXT,
                    ends_at TEXT,
                    customer_portal_url TEXT,
                    update_payment_url TEXT,
                    last_event_name TEXT,
                    checkout_url TEXT,
                    test_mode BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            self.execute("""
                CREATE TABLE IF NOT EXISTS billing_events (
                    id BIGSERIAL PRIMARY KEY,
                    event_name TEXT NOT NULL,
                    guild_id TEXT,
                    subscription_id TEXT,
                    created_at TEXT NOT NULL,
                    raw_payload TEXT
                )
            """)
        for column, definition in CURRENT_GUILD_COLUMNS_POSTGRES.items():
            self._add_column_if_missing("guild_settings", column, definition)

    def _ensure_stat(self, key: str, default_value: int):
        with self.transaction():
            self.execute(
                "INSERT INTO install_stats (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                (key, default_value),
            )

    def ensure_guild(self, guild_id: int):
        with self.transaction():
            self.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, premium, milestone_roles, joined_at, report_channel_id,
                    last_daily_report_date, growth_alert_threshold, last_alert_net,
                    alerts_enabled, vote_reward_role_id
                )
                VALUES (?, ?, '{}', ?, NULL, NULL, 25, NULL, ?, NULL)
                ON CONFLICT(guild_id) DO NOTHING
                """,
                (self.db_guild_id(guild_id), self.db_bool(False), datetime.now(UTC).isoformat(), self.db_bool(True)),
            )

    def remove_guild(self, guild_id: int):
        with self.transaction():
            self.execute("DELETE FROM guild_settings WHERE guild_id = ?", (self.db_guild_id(guild_id),))
            self.execute("DELETE FROM growth_stats WHERE guild_id = ?", (self.db_guild_id(guild_id),))

    def get_guild_settings(self, guild_id: int):
        self.ensure_guild(guild_id)
        row = self.row_dict(self.fetchone("SELECT * FROM guild_settings WHERE guild_id = ?", (self.db_guild_id(guild_id),)))
        if row is None:
            return {
                "guild_id": guild_id,
                "premium": False,
                "milestone_roles": {},
                "joined_at": None,
                "report_channel_id": None,
                "last_daily_report_date": None,
                "growth_alert_threshold": 25,
                "last_alert_net": None,
                "alerts_enabled": True,
                "vote_reward_role_id": None,
            }
        milestone_roles = {}
        raw = row.get("milestone_roles") or "{}"
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                milestone_roles = {int(k): int(v) for k, v in parsed.items()}
        except Exception:
            milestone_roles = {}
        return {
            "guild_id": int(row["guild_id"]),
            "premium": bool(row.get("premium")),
            "milestone_roles": milestone_roles,
            "joined_at": row.get("joined_at"),
            "report_channel_id": int(row.get("report_channel_id")) if row.get("report_channel_id") is not None else None,
            "last_daily_report_date": row.get("last_daily_report_date"),
            "growth_alert_threshold": int(row.get("growth_alert_threshold") or 25),
            "last_alert_net": row.get("last_alert_net"),
            "alerts_enabled": bool(row.get("alerts_enabled", True)),
            "vote_reward_role_id": int(row.get("vote_reward_role_id")) if row.get("vote_reward_role_id") is not None else None,
        }

    def set_premium(self, guild_id: int, enabled: bool):
        self.ensure_guild(guild_id)
        with self.transaction():
            self.execute("UPDATE guild_settings SET premium = ? WHERE guild_id = ?", (self.db_bool(enabled), self.db_guild_id(guild_id)))

    def set_milestone_role(self, guild_id: int, member_count: int, role_id: int):
        data = self.get_guild_settings(guild_id)
        mapping = data["milestone_roles"]
        mapping[int(member_count)] = int(role_id)
        with self.transaction():
            self.execute(
                "UPDATE guild_settings SET milestone_roles = ? WHERE guild_id = ?",
                (json.dumps({str(k): v for k, v in mapping.items()}), self.db_guild_id(guild_id)),
            )

    def remove_milestone_role(self, guild_id: int, member_count: int):
        data = self.get_guild_settings(guild_id)
        mapping = data["milestone_roles"]
        if int(member_count) in mapping:
            del mapping[int(member_count)]
            with self.transaction():
                self.execute(
                    "UPDATE guild_settings SET milestone_roles = ? WHERE guild_id = ?",
                    (json.dumps({str(k): v for k, v in mapping.items()}), self.db_guild_id(guild_id)),
                )

    def get_milestone_roles(self, guild_id: int):
        return self.get_guild_settings(guild_id)["milestone_roles"]

    def set_report_channel(self, guild_id: int, channel_id: Optional[int]):
        self.ensure_guild(guild_id)
        with self.transaction():
            self.execute("UPDATE guild_settings SET report_channel_id = ? WHERE guild_id = ?", (self.db_discord_id(channel_id), self.db_guild_id(guild_id)))

    def set_vote_reward_role(self, guild_id: int, role_id: Optional[int]):
        self.ensure_guild(guild_id)
        with self.transaction():
            self.execute("UPDATE guild_settings SET vote_reward_role_id = ? WHERE guild_id = ?", (self.db_discord_id(role_id), self.db_guild_id(guild_id)))

    def set_last_daily_report_date(self, guild_id: int, day_str: str):
        self.ensure_guild(guild_id)
        with self.transaction():
            self.execute("UPDATE guild_settings SET last_daily_report_date = ? WHERE guild_id = ?", (day_str, self.db_guild_id(guild_id)))

    def set_growth_alert_threshold(self, guild_id: int, threshold: int):
        self.ensure_guild(guild_id)
        with self.transaction():
            self.execute("UPDATE guild_settings SET growth_alert_threshold = ? WHERE guild_id = ?", (threshold, self.db_guild_id(guild_id)))

    def set_last_alert_net(self, guild_id: int, net_value: Optional[int]):
        self.ensure_guild(guild_id)
        with self.transaction():
            self.execute("UPDATE guild_settings SET last_alert_net = ? WHERE guild_id = ?", (net_value, self.db_guild_id(guild_id)))

    def set_alerts_enabled(self, guild_id: int, enabled: bool):
        self.ensure_guild(guild_id)
        with self.transaction():
            self.execute("UPDATE guild_settings SET alerts_enabled = ? WHERE guild_id = ?", (self.db_bool(enabled), self.db_guild_id(guild_id)))

    def increment_stat(self, key: str, amount: int = 1):
        self._ensure_stat(key, 0)
        with self.transaction():
            self.execute("UPDATE install_stats SET value = value + ? WHERE key = ?", (amount, key))

    def get_stat(self, key: str) -> int:
        self._ensure_stat(key, 0)
        row = self.row_dict(self.fetchone("SELECT value FROM install_stats WHERE key = ?", (key,)))
        return int(row["value"]) if row else 0

    def record_install_event(self, guild_id: int, guild_name: str, event_type: str, member_count: int):
        with self.transaction():
            self.execute(
                "INSERT INTO install_events (guild_id, guild_name, event_type, member_count, timestamp) VALUES (?, ?, ?, ?, ?)",
                (self.db_guild_id(guild_id), guild_name, event_type, member_count, datetime.now(UTC).isoformat()),
            )

    def get_recent_install_events(self, limit: int = 10):
        return self.fetchall(
            "SELECT guild_id, guild_name, event_type, member_count, timestamp FROM install_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def increment_growth(self, guild_id: int, day_str: str, joins: int = 0, leaves: int = 0):
        self.ensure_guild(guild_id)
        with self.transaction():
            self.execute(
                """
                INSERT INTO growth_stats (guild_id, date, joins, leaves)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, date)
                DO UPDATE SET joins = growth_stats.joins + excluded.joins,
                              leaves = growth_stats.leaves + excluded.leaves
                """,
                (self.db_guild_id(guild_id), day_str, joins, leaves),
            )

    def get_growth_for_date(self, guild_id: int, day_str: str):
        row = self.row_dict(self.fetchone("SELECT joins, leaves FROM growth_stats WHERE guild_id = ? AND date = ?", (self.db_guild_id(guild_id), day_str)))
        if row is None:
            return {"joins": 0, "leaves": 0, "net": 0}
        joins = int(row["joins"] or 0)
        leaves = int(row["leaves"] or 0)
        return {"joins": joins, "leaves": leaves, "net": joins - leaves}

    def get_growth_range(self, guild_id: int, start_day: str, end_day: str):
        row = self.row_dict(self.fetchone(
            """
            SELECT COALESCE(SUM(joins), 0) AS joins, COALESCE(SUM(leaves), 0) AS leaves
            FROM growth_stats
            WHERE guild_id = ? AND date >= ? AND date <= ?
            """,
            (self.db_guild_id(guild_id), start_day, end_day),
        ))
        joins = int(row["joins"] or 0)
        leaves = int(row["leaves"] or 0)
        return {"joins": joins, "leaves": leaves, "net": joins - leaves}

    def get_top_growth_days(self, guild_id: int, limit: int = 5):
        return self.fetchall(
            """
            SELECT date, joins, leaves, (joins - leaves) AS net
            FROM growth_stats
            WHERE guild_id = ?
            ORDER BY net DESC, joins DESC, date DESC
            LIMIT ?
            """,
            (self.db_guild_id(guild_id), limit),
        )

    def get_best_growth_day(self, guild_id: int):
        row = self.row_dict(self.fetchone(
            """
            SELECT date, joins, leaves, (joins - leaves) AS net
            FROM growth_stats
            WHERE guild_id = ?
            ORDER BY net DESC, joins DESC
            LIMIT 1
            """,
            (self.db_guild_id(guild_id),),
        ))
        if row is None:
            return None
        return {"date": row["date"], "joins": int(row["joins"]), "leaves": int(row["leaves"]), "net": int(row["net"])}

    def get_vote_user(self, user_id: int):
        row = self.row_dict(self.fetchone("SELECT * FROM vote_users WHERE user_id = ?", (user_id,)))
        if row is None:
            return {
                "user_id": user_id,
                "total_votes": 0,
                "streak": 0,
                "last_vote_at": None,
                "premium_until": None,
                "last_vote_source": None,
                "updated_at": None,
            }
        return {
            "user_id": int(row["user_id"]),
            "total_votes": int(row["total_votes"] or 0),
            "streak": int(row["streak"] or 0),
            "last_vote_at": row["last_vote_at"],
            "premium_until": row["premium_until"],
            "last_vote_source": row["last_vote_source"],
            "updated_at": row["updated_at"],
        }

    def set_vote_user(self, user_id: int, total_votes: int, streak: int, last_vote_at: Optional[str], premium_until: Optional[str], last_vote_source: str = "topgg"):
        with self.transaction():
            self.execute(
                """
                INSERT INTO vote_users (user_id, total_votes, streak, last_vote_at, premium_until, last_vote_source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    total_votes = excluded.total_votes,
                    streak = excluded.streak,
                    last_vote_at = excluded.last_vote_at,
                    premium_until = excluded.premium_until,
                    last_vote_source = excluded.last_vote_source,
                    updated_at = excluded.updated_at
                """,
                (user_id, total_votes, streak, last_vote_at, premium_until, last_vote_source, datetime.now(UTC).isoformat()),
            )

    def record_vote_event(self, user_id: int, username: Optional[str], source: str, is_weekend: bool, voted_at: str, raw_payload: dict):
        with self.transaction():
            self.execute(
                "INSERT INTO vote_events (user_id, username, source, is_weekend, voted_at, raw_payload) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, source, self.db_bool(is_weekend), voted_at, json.dumps(raw_payload)),
            )

    def get_recent_vote_events(self, limit: int = 10):
        return self.fetchall(
            "SELECT id, user_id, username, source, is_weekend, voted_at FROM vote_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def get_top_voters(self, limit: int = 10):
        return self.fetchall(
            "SELECT user_id, total_votes, streak, last_vote_at, premium_until FROM vote_users ORDER BY total_votes DESC, last_vote_at DESC LIMIT ?",
            (limit,),
        )

    def upsert_guild_billing(self, guild_id: int, discord_user_id: Optional[int], subscription_id: Optional[str], customer_id: Optional[str], order_id: Optional[str], product_name: Optional[str], variant_name: Optional[str], status: Optional[str], status_formatted: Optional[str], renews_at: Optional[str], ends_at: Optional[str], customer_portal_url: Optional[str], update_payment_url: Optional[str], last_event_name: Optional[str], checkout_url: Optional[str], test_mode: bool):
        now_iso = datetime.now(UTC).isoformat()
        existing = self.get_guild_billing(guild_id)
        with self.transaction():
            self.execute(
                """
                INSERT INTO guild_billing (
                    guild_id, discord_user_id, lemonsqueezy_subscription_id, lemonsqueezy_customer_id,
                    order_id, product_name, variant_name, status, status_formatted, renews_at, ends_at,
                    customer_portal_url, update_payment_url, last_event_name, checkout_url, test_mode, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    discord_user_id = excluded.discord_user_id,
                    lemonsqueezy_subscription_id = excluded.lemonsqueezy_subscription_id,
                    lemonsqueezy_customer_id = excluded.lemonsqueezy_customer_id,
                    order_id = excluded.order_id,
                    product_name = excluded.product_name,
                    variant_name = excluded.variant_name,
                    status = excluded.status,
                    status_formatted = excluded.status_formatted,
                    renews_at = excluded.renews_at,
                    ends_at = excluded.ends_at,
                    customer_portal_url = excluded.customer_portal_url,
                    update_payment_url = excluded.update_payment_url,
                    last_event_name = excluded.last_event_name,
                    checkout_url = excluded.checkout_url,
                    test_mode = excluded.test_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    self.db_guild_id(guild_id), self.db_discord_id(discord_user_id), subscription_id, customer_id, order_id, product_name, variant_name,
                    status, status_formatted, renews_at, ends_at, customer_portal_url, update_payment_url,
                    last_event_name, checkout_url, self.db_bool(test_mode), existing.get("created_at") if existing else now_iso, now_iso,
                ),
            )

    def get_guild_billing(self, guild_id: int):
        row = self.row_dict(self.fetchone("SELECT * FROM guild_billing WHERE guild_id = ?", (self.db_guild_id(guild_id),)))
        if row is None:
            return None
        return {
            "guild_id": int(row["guild_id"]),
            "discord_user_id": int(row["discord_user_id"]) if row["discord_user_id"] is not None else None,
            "lemonsqueezy_subscription_id": row["lemonsqueezy_subscription_id"],
            "lemonsqueezy_customer_id": row["lemonsqueezy_customer_id"],
            "order_id": row["order_id"],
            "product_name": row["product_name"],
            "variant_name": row["variant_name"],
            "status": row["status"],
            "status_formatted": row["status_formatted"],
            "renews_at": row["renews_at"],
            "ends_at": row["ends_at"],
            "customer_portal_url": row["customer_portal_url"],
            "update_payment_url": row["update_payment_url"],
            "last_event_name": row["last_event_name"],
            "checkout_url": row["checkout_url"],
            "test_mode": bool(row["test_mode"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_guild_billing_by_subscription_id(self, subscription_id: str):
        row = self.row_dict(self.fetchone(
            "SELECT guild_id FROM guild_billing WHERE lemonsqueezy_subscription_id = ? LIMIT 1",
            (subscription_id,),
        ))
        if row is None:
            return None
        return self.get_guild_billing(int(row["guild_id"]))

    def record_billing_event(self, event_name: str, guild_id: Optional[int], subscription_id: Optional[str], raw_payload: dict):
        with self.transaction():
            self.execute(
                "INSERT INTO billing_events (event_name, guild_id, subscription_id, created_at, raw_payload) VALUES (?, ?, ?, ?, ?)",
                (event_name, self.db_guild_id(guild_id) if guild_id is not None else None, subscription_id, datetime.now(UTC).isoformat(), json.dumps(raw_payload)),
            )

    def get_recent_billing_events(self, limit: int = 10):
        return self.fetchall(
            "SELECT id, event_name, guild_id, subscription_id, created_at FROM billing_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def get_legacy_postgres_migration_counts(self):
        if self.backend != "postgres":
            return {}
        counts = {}
        for table in LEGACY_POSTGRES_TABLES:
            try:
                row = self.row_dict(self.fetchone(f"SELECT COUNT(*) AS count FROM {table}"))
                counts[table] = int(row["count"] or 0)
            except Exception:
                self.conn.rollback()
                counts[table] = None
        return counts


def create_database_from_env(
    database_url: Optional[str] = None,
    sqlite_path: Optional[str] = None,
    initialize_schema: bool = True,
) -> Database:
    url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
    path = sqlite_path or os.getenv("DATABASE_PATH", "legacy_bot.db")
    return Database(sqlite_path=path, database_url=url or "", initialize_schema=initialize_schema)


def redact_database_url(value: str) -> str:
    if not value:
        return ""
    return "[redacted DATABASE_URL]"
