import os
import io
import json
import sqlite3
import logging
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Optional

from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks

# =========================
# CONFIG
# =========================
BOT_NAME = "Legacy Bot"
DEFAULT_PREFIX = "!"
DATABASE_PATH = os.getenv("DATABASE_PATH", "legacy_bot.db")
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or ""
OWNER_IDS = {207279875902537731}

SUPPORT_SERVER_URL = os.getenv(
    "SUPPORT_SERVER_URL",
    "https://discord.gg/your-support-server",
)
BOT_INVITE_URL = os.getenv(
    "BOT_INVITE_URL",
    "https://discord.com/oauth2/authorize?client_id=1483943578148405279&permissions=8&integration_type=0&scope=bot+applications.commands",
)

TOPGG_WEBHOOK_AUTH = os.getenv("TOPGG_WEBHOOK_AUTH", "")
TOPGG_WEBHOOK_ROUTE = os.getenv("TOPGG_WEBHOOK_ROUTE", "/topgg")
TOPGG_WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
TOPGG_WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
TOPGG_VOTE_PREMIUM_HOURS = int(os.getenv("TOPGG_VOTE_PREMIUM_HOURS", "12"))
TOPGG_VOTE_URL = os.getenv("TOPGG_VOTE_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(BOT_NAME)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

UTC = timezone.utc
DAILY_REPORT_TIME_UTC = dt_time(hour=0, minute=5, tzinfo=UTC)


# =========================
# DATABASE
# =========================
class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._setup()

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(row["name"] == column_name for row in rows)

    def _setup(self):
        with self.conn:
            self.conn.execute(
                """
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
                """
            )

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS install_stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS install_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    guild_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    member_count INTEGER NOT NULL DEFAULT 0,
                    timestamp TEXT NOT NULL
                )
                """
            )

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS growth_stats (
                    guild_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    joins INTEGER NOT NULL DEFAULT 0,
                    leaves INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, date)
                )
                """
            )

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vote_users (
                    user_id INTEGER PRIMARY KEY,
                    total_votes INTEGER NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0,
                    last_vote_at TEXT,
                    premium_until TEXT,
                    last_vote_source TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vote_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    source TEXT NOT NULL DEFAULT 'topgg',
                    is_weekend INTEGER NOT NULL DEFAULT 0,
                    voted_at TEXT NOT NULL,
                    raw_payload TEXT
                )
                """
            )

        if not self._column_exists("guild_settings", "report_channel_id"):
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE guild_settings ADD COLUMN report_channel_id INTEGER"
                )

        if not self._column_exists("guild_settings", "last_daily_report_date"):
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE guild_settings ADD COLUMN last_daily_report_date TEXT"
                )

        if not self._column_exists("guild_settings", "growth_alert_threshold"):
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE guild_settings ADD COLUMN growth_alert_threshold INTEGER NOT NULL DEFAULT 25"
                )

        if not self._column_exists("guild_settings", "last_alert_net"):
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE guild_settings ADD COLUMN last_alert_net INTEGER"
                )

        if not self._column_exists("guild_settings", "alerts_enabled"):
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE guild_settings ADD COLUMN alerts_enabled INTEGER NOT NULL DEFAULT 1"
                )

        if not self._column_exists("guild_settings", "vote_reward_role_id"):
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE guild_settings ADD COLUMN vote_reward_role_id INTEGER"
                )

        self._ensure_stat("join_count", 0)
        self._ensure_stat("remove_count", 0)
        self._ensure_stat("topgg_votes_total", 0)

    def _ensure_stat(self, key: str, default_value: int):
        row = self.conn.execute(
            "SELECT value FROM install_stats WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO install_stats (key, value) VALUES (?, ?)",
                    (key, default_value),
                )

    def ensure_guild(self, guild_id: int):
        row = self.conn.execute(
            "SELECT guild_id FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()

        if row is None:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO guild_settings (
                        guild_id,
                        premium,
                        milestone_roles,
                        joined_at,
                        report_channel_id,
                        last_daily_report_date,
                        growth_alert_threshold,
                        last_alert_net,
                        alerts_enabled,
                        vote_reward_role_id
                    )
                    VALUES (?, 0, '{}', ?, NULL, NULL, 25, NULL, 1, NULL)
                    """,
                    (guild_id, datetime.now(UTC).isoformat()),
                )

    def remove_guild(self, guild_id: int):
        with self.conn:
            self.conn.execute(
                "DELETE FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            )
            self.conn.execute(
                "DELETE FROM growth_stats WHERE guild_id = ?",
                (guild_id,),
            )

    def get_guild_settings(self, guild_id: int):
        self.ensure_guild(guild_id)
        row = self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()

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
        raw = row["milestone_roles"] or "{}"
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                milestone_roles = {int(k): int(v) for k, v in parsed.items()}
        except Exception:
            milestone_roles = {}

        return {
            "guild_id": row["guild_id"],
            "premium": bool(row["premium"]),
            "milestone_roles": milestone_roles,
            "joined_at": row["joined_at"],
            "report_channel_id": row["report_channel_id"],
            "last_daily_report_date": row["last_daily_report_date"],
            "growth_alert_threshold": int(row["growth_alert_threshold"] or 25),
            "last_alert_net": row["last_alert_net"],
            "alerts_enabled": bool(row["alerts_enabled"]),
            "vote_reward_role_id": row["vote_reward_role_id"],
        }

    def set_premium(self, guild_id: int, enabled: bool):
        self.ensure_guild(guild_id)
        with self.conn:
            self.conn.execute(
                "UPDATE guild_settings SET premium = ? WHERE guild_id = ?",
                (1 if enabled else 0, guild_id),
            )

    def set_milestone_role(self, guild_id: int, member_count: int, role_id: int):
        data = self.get_guild_settings(guild_id)
        mapping = data["milestone_roles"]
        mapping[int(member_count)] = int(role_id)

        with self.conn:
            self.conn.execute(
                "UPDATE guild_settings SET milestone_roles = ? WHERE guild_id = ?",
                (json.dumps({str(k): v for k, v in mapping.items()}), guild_id),
            )

    def remove_milestone_role(self, guild_id: int, member_count: int):
        data = self.get_guild_settings(guild_id)
        mapping = data["milestone_roles"]

        if int(member_count) in mapping:
            del mapping[int(member_count)]
            with self.conn:
                self.conn.execute(
                    "UPDATE guild_settings SET milestone_roles = ? WHERE guild_id = ?",
                    (json.dumps({str(k): v for k, v in mapping.items()}), guild_id),
                )

    def get_milestone_roles(self, guild_id: int):
        return self.get_guild_settings(guild_id)["milestone_roles"]

    def set_report_channel(self, guild_id: int, channel_id: Optional[int]):
        self.ensure_guild(guild_id)
        with self.conn:
            self.conn.execute(
                "UPDATE guild_settings SET report_channel_id = ? WHERE guild_id = ?",
                (channel_id, guild_id),
            )

    def set_vote_reward_role(self, guild_id: int, role_id: Optional[int]):
        self.ensure_guild(guild_id)
        with self.conn:
            self.conn.execute(
                "UPDATE guild_settings SET vote_reward_role_id = ? WHERE guild_id = ?",
                (role_id, guild_id),
            )

    def set_last_daily_report_date(self, guild_id: int, day_str: str):
        self.ensure_guild(guild_id)
        with self.conn:
            self.conn.execute(
                "UPDATE guild_settings SET last_daily_report_date = ? WHERE guild_id = ?",
                (day_str, guild_id),
            )

    def set_growth_alert_threshold(self, guild_id: int, threshold: int):
        self.ensure_guild(guild_id)
        with self.conn:
            self.conn.execute(
                "UPDATE guild_settings SET growth_alert_threshold = ? WHERE guild_id = ?",
                (threshold, guild_id),
            )

    def set_last_alert_net(self, guild_id: int, net_value: Optional[int]):
        self.ensure_guild(guild_id)
        with self.conn:
            self.conn.execute(
                "UPDATE guild_settings SET last_alert_net = ? WHERE guild_id = ?",
                (net_value, guild_id),
            )

    def set_alerts_enabled(self, guild_id: int, enabled: bool):
        self.ensure_guild(guild_id)
        with self.conn:
            self.conn.execute(
                "UPDATE guild_settings SET alerts_enabled = ? WHERE guild_id = ?",
                (1 if enabled else 0, guild_id),
            )

    def increment_stat(self, key: str, amount: int = 1):
        self._ensure_stat(key, 0)
        with self.conn:
            self.conn.execute(
                "UPDATE install_stats SET value = value + ? WHERE key = ?",
                (amount, key),
            )

    def get_stat(self, key: str) -> int:
        self._ensure_stat(key, 0)
        row = self.conn.execute(
            "SELECT value FROM install_stats WHERE key = ?",
            (key,),
        ).fetchone()
        return int(row["value"]) if row else 0

    def record_install_event(
        self,
        guild_id: int,
        guild_name: str,
        event_type: str,
        member_count: int,
    ):
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO install_events (guild_id, guild_name, event_type, member_count, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    guild_name,
                    event_type,
                    member_count,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_recent_install_events(self, limit: int = 10):
        return self.conn.execute(
            """
            SELECT guild_id, guild_name, event_type, member_count, timestamp
            FROM install_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def increment_growth(
        self,
        guild_id: int,
        day_str: str,
        joins: int = 0,
        leaves: int = 0,
    ):
        self.ensure_guild(guild_id)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO growth_stats (guild_id, date, joins, leaves)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, date)
                DO UPDATE SET
                    joins = joins + excluded.joins,
                    leaves = leaves + excluded.leaves
                """,
                (guild_id, day_str, joins, leaves),
            )

    def get_growth_for_date(self, guild_id: int, day_str: str):
        row = self.conn.execute(
            """
            SELECT joins, leaves
            FROM growth_stats
            WHERE guild_id = ? AND date = ?
            """,
            (guild_id, day_str),
        ).fetchone()

        if row is None:
            return {"joins": 0, "leaves": 0, "net": 0}

        joins = int(row["joins"] or 0)
        leaves = int(row["leaves"] or 0)
        return {"joins": joins, "leaves": leaves, "net": joins - leaves}

    def get_growth_range(self, guild_id: int, start_day: str, end_day: str):
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(joins), 0) AS joins, COALESCE(SUM(leaves), 0) AS leaves
            FROM growth_stats
            WHERE guild_id = ? AND date >= ? AND date <= ?
            """,
            (guild_id, start_day, end_day),
        ).fetchone()

        joins = int(row["joins"] or 0)
        leaves = int(row["leaves"] or 0)
        return {"joins": joins, "leaves": leaves, "net": joins - leaves}

    def get_top_growth_days(self, guild_id: int, limit: int = 5):
        return self.conn.execute(
            """
            SELECT date, joins, leaves, (joins - leaves) AS net
            FROM growth_stats
            WHERE guild_id = ?
            ORDER BY net DESC, joins DESC, date DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()

    def get_best_growth_day(self, guild_id: int):
        row = self.conn.execute(
            """
            SELECT date, joins, leaves, (joins - leaves) AS net
            FROM growth_stats
            WHERE guild_id = ?
            ORDER BY net DESC, joins DESC
            LIMIT 1
            """,
            (guild_id,),
        ).fetchone()

        if row is None:
            return None

        return {
            "date": row["date"],
            "joins": int(row["joins"]),
            "leaves": int(row["leaves"]),
            "net": int(row["net"]),
        }

    def get_vote_user(self, user_id: int):
        row = self.conn.execute(
            "SELECT * FROM vote_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

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
            "user_id": row["user_id"],
            "total_votes": int(row["total_votes"] or 0),
            "streak": int(row["streak"] or 0),
            "last_vote_at": row["last_vote_at"],
            "premium_until": row["premium_until"],
            "last_vote_source": row["last_vote_source"],
            "updated_at": row["updated_at"],
        }

    def set_vote_user(
        self,
        user_id: int,
        total_votes: int,
        streak: int,
        last_vote_at: Optional[str],
        premium_until: Optional[str],
        last_vote_source: str = "topgg",
    ):
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO vote_users (
                    user_id, total_votes, streak, last_vote_at, premium_until, last_vote_source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET
                    total_votes = excluded.total_votes,
                    streak = excluded.streak,
                    last_vote_at = excluded.last_vote_at,
                    premium_until = excluded.premium_until,
                    last_vote_source = excluded.last_vote_source,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    total_votes,
                    streak,
                    last_vote_at,
                    premium_until,
                    last_vote_source,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def record_vote_event(
        self,
        user_id: int,
        username: Optional[str],
        source: str,
        is_weekend: bool,
        voted_at: str,
        raw_payload: dict,
    ):
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO vote_events (user_id, username, source, is_weekend, voted_at, raw_payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    source,
                    1 if is_weekend else 0,
                    voted_at,
                    json.dumps(raw_payload),
                ),
            )

    def get_recent_vote_events(self, limit: int = 10):
        return self.conn.execute(
            """
            SELECT id, user_id, username, source, is_weekend, voted_at
            FROM vote_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def get_top_voters(self, limit: int = 10):
        return self.conn.execute(
            """
            SELECT user_id, total_votes, streak, last_vote_at, premium_until
            FROM vote_users
            ORDER BY total_votes DESC, last_vote_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


db = Database(DATABASE_PATH)


# =========================
# BOT SETUP
# =========================
async def get_prefix(bot_instance, message):
    return DEFAULT_PREFIX


bot = commands.Bot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None,
    case_insensitive=True,
)


# =========================
# HELPERS
# =========================
def is_owner_user(user_id: int) -> bool:
    return user_id in OWNER_IDS


def owner_only():
    async def predicate(ctx: commands.Context):
        if is_owner_user(ctx.author.id):
            return True
        raise commands.CheckFailure("This command is restricted to the bot owner.")
    return commands.check(predicate)


def admin_or_manage_guild():
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            raise commands.CheckFailure("This command can only be used in a server.")
        if (
            ctx.author.guild_permissions.administrator
            or ctx.author.guild_permissions.manage_guild
        ):
            return True
        raise commands.CheckFailure(
            "You need Administrator or Manage Server permissions to use this command."
        )
    return commands.check(predicate)


def premium_required():
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            raise commands.CheckFailure("This command can only be used in a server.")
        settings = db.get_guild_settings(ctx.guild.id)
        if settings["premium"]:
            return True
        raise commands.CheckFailure("This feature is premium-only for this server.")
    return commands.check(predicate)


def current_utc_day_str() -> str:
    return datetime.now(UTC).date().isoformat()


def yesterday_utc_day_str() -> str:
    return (datetime.now(UTC).date() - timedelta(days=1)).isoformat()


def safe_truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def build_main_embed(
    title: str,
    description: str = "",
    color: discord.Color = discord.Color.blurple(),
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(UTC),
    )
    embed.set_footer(text=BOT_NAME)
    return embed


def total_member_estimate() -> int:
    return sum(guild.member_count or 0 for guild in bot.guilds)


def get_report_channel(guild: discord.Guild):
    settings = db.get_guild_settings(guild.id)
    channel_id = settings.get("report_channel_id")
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        channel = bot.get_channel(channel_id)
    return channel


def get_vote_reward_role(guild: discord.Guild):
    settings = db.get_guild_settings(guild.id)
    role_id = settings.get("vote_reward_role_id")
    if not role_id:
        return None
    return guild.get_role(role_id)


def iso_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def format_dt_safe(value: Optional[str], style: str = "R") -> str:
    dt = iso_to_dt(value)
    if dt is None:
        return "Never"
    return discord.utils.format_dt(dt, style=style)


def get_topgg_vote_url() -> str:
    if TOPGG_VOTE_URL:
        return TOPGG_VOTE_URL
    if bot.user:
        return f"https://top.gg/bot/{bot.user.id}/vote"
    return "https://top.gg/"


def is_vote_premium_active(user_id: int) -> bool:
    data = db.get_vote_user(user_id)
    premium_until = iso_to_dt(data.get("premium_until"))
    if premium_until is None:
        return False
    return premium_until > datetime.now(UTC)


def get_vote_premium_remaining_text(user_id: int) -> str:
    data = db.get_vote_user(user_id)
    premium_until = iso_to_dt(data.get("premium_until"))
    if premium_until is None:
        return "Inactive"

    now_dt = datetime.now(UTC)
    if premium_until <= now_dt:
        return "Expired"

    delta = premium_until - now_dt
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60

    if hours > 0:
        return f"{hours}h {minutes}m remaining"
    return f"{minutes}m remaining"


def growth_message_for_stats(joins: int, leaves: int) -> str:
    net = joins - leaves
    if net > 0:
        return "📈 You’re growing — keep it up!"
    if net < 0:
        return "⚠️ Membership dipped a bit — time to re-engage your community."
    return "📊 Flat day today — tomorrow can be your push."


def medal_for_rank(rank: int) -> str:
    if rank == 1:
        return "🥇"
    if rank == 2:
        return "🥈"
    if rank == 3:
        return "🥉"
    return "🔹"



def get_growth_series(guild_id: int, days: int = 7):
    days = max(3, min(int(days), 30))
    end_date = datetime.now(UTC).date()
    start_date = end_date - timedelta(days=days - 1)

    points = []
    current_date = start_date
    cumulative_total = 0

    while current_date <= end_date:
        day_str = current_date.isoformat()
        stats = db.get_growth_for_date(guild_id, day_str)
        joins = int(stats["joins"])
        leaves = int(stats["leaves"])
        net = int(stats["net"])
        cumulative_total += net

        points.append(
            {
                "date": day_str,
                "label": current_date.strftime("%m/%d"),
                "joins": joins,
                "leaves": leaves,
                "net": net,
                "cumulative_net": cumulative_total,
            }
        )
        current_date += timedelta(days=1)

    return points


def generate_dashboard_chart(guild: discord.Guild, days: int = 7) -> io.BytesIO:
    series = get_growth_series(guild.id, days)
    labels = [point["label"] for point in series]
    net_values = [point["net"] for point in series]
    cumulative_values = [point["cumulative_net"] for point in series]

    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.plot(labels, net_values, marker="o", linewidth=2, label="Daily Net Growth")
    ax.plot(labels, cumulative_values, marker="s", linewidth=2, linestyle="--", label="Trend")
    ax.axhline(0, linewidth=1)
    ax.set_title(f"{guild.name} Growth Dashboard")
    ax.set_xlabel("Date")
    ax.set_ylabel("Members")
    ax.legend()
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=150, bbox_inches="tight")
    buffer.seek(0)
    plt.close(fig)
    return buffer


def build_dashboard_embed(guild: discord.Guild, days: int = 7) -> discord.Embed:
    days = max(3, min(int(days), 30))
    settings = db.get_guild_settings(guild.id)
    end_date_obj = datetime.now(UTC).date()
    start_date_obj = end_date_obj - timedelta(days=days - 1)

    window_stats = db.get_growth_range(
        guild.id,
        start_date_obj.isoformat(),
        end_date_obj.isoformat(),
    )
    today_stats = db.get_growth_for_date(guild.id, end_date_obj.isoformat())
    week_start = end_date_obj - timedelta(days=6)
    week_stats = db.get_growth_range(
        guild.id,
        week_start.isoformat(),
        end_date_obj.isoformat(),
    )
    best_day = db.get_best_growth_day(guild.id)
    top_days = db.get_top_growth_days(guild.id, limit=3)
    series = get_growth_series(guild.id, days)

    positive_days = sum(1 for point in series if point["net"] > 0)
    negative_days = sum(1 for point in series if point["net"] < 0)
    flat_days = sum(1 for point in series if point["net"] == 0)

    trend_text = "Flat"
    if series:
        recent_slice = series[-3:] if len(series) >= 3 else series
        recent_avg = sum(point["net"] for point in recent_slice) / len(recent_slice)
        full_avg = sum(point["net"] for point in series) / len(series)
        if recent_avg > full_avg and recent_avg > 0:
            trend_text = "Upward"
        elif recent_avg < full_avg and recent_avg < 0:
            trend_text = "Downward"
        elif window_stats["net"] > 0:
            trend_text = "Positive"
        elif window_stats["net"] < 0:
            trend_text = "Negative"

    embed = build_main_embed(
        "📊 Premium Growth Dashboard",
        (
            f"Detailed analytics for **{guild.name}**\n"
            f"Window: **{start_date_obj.isoformat()}** to **{end_date_obj.isoformat()}** UTC"
        ),
        discord.Color.gold(),
    )

    embed.add_field(name="Members", value=str(guild.member_count or 0), inline=True)
    embed.add_field(name="Server Premium", value="Enabled" if settings["premium"] else "Disabled", inline=True)
    embed.add_field(name="Trend", value=trend_text, inline=True)

    embed.add_field(name=f"{days}-Day Joins", value=f"+{window_stats['joins']}", inline=True)
    embed.add_field(name=f"{days}-Day Leaves", value=f"-{window_stats['leaves']}", inline=True)
    embed.add_field(name=f"{days}-Day Net", value=f"{window_stats['net']:+d}", inline=True)

    embed.add_field(name="Today", value=f"{today_stats['net']:+d} net", inline=True)
    embed.add_field(name="7-Day Net", value=f"{week_stats['net']:+d}", inline=True)
    embed.add_field(name="Positive Days", value=str(positive_days), inline=True)

    best_day_text = "No growth data recorded yet."
    if best_day:
        best_day_text = (
            f"**{best_day['date']}** • Net **{best_day['net']:+d}**\n"
            f"(+{best_day['joins']} joins / -{best_day['leaves']} leaves)"
        )
    embed.add_field(name="Best Day Performance", value=best_day_text, inline=False)

    if top_days:
        lines = []
        for idx, row in enumerate(top_days, start=1):
            lines.append(
                f"{medal_for_rank(idx)} **{row['date']}** • **{int(row['net']):+d}** "
                f"(+{int(row['joins'])} / -{int(row['leaves'])})"
            )
        embed.add_field(name="Top Growth Days", value="\n".join(lines), inline=False)

    last_seven = get_growth_series(guild.id, 7)
    if last_seven:
        recent_lines = []
        for point in last_seven:
            recent_lines.append(
                f"**{point['label']}** • {point['net']:+d} net "
                f"(+{point['joins']} / -{point['leaves']})"
            )
        embed.add_field(
            name="Last 7 Days",
            value="\n".join(recent_lines[:7]),
            inline=False,
        )

    embed.add_field(
        name="Activity Split",
        value=f"Up days: **{positive_days}** • Down days: **{negative_days}** • Flat days: **{flat_days}**",
        inline=False,
    )

    embed.set_image(url="attachment://dashboard_growth.png")
    return embed


def build_growth_leaderboard_embed(guild: discord.Guild) -> discord.Embed:
    rows = db.get_top_growth_days(guild.id, limit=10)

    if not rows:
        return build_main_embed(
            "🏆 Growth Leaderboard",
            "No growth data recorded yet.",
            discord.Color.blurple(),
        )

    lines = []
    for idx, row in enumerate(rows, start=1):
        lines.append(
            f"{medal_for_rank(idx)} **#{idx}** • **{row['date']}** • "
            f"Net **{int(row['net']):+d}** "
            f"(+{int(row['joins'])} / -{int(row['leaves'])})"
        )

    best_row = rows[0]
    embed = build_main_embed(
        "🏆 Growth Leaderboard",
        "Top growth days recorded for this server.",
        discord.Color.gold(),
    )
    embed.add_field(name="Leaderboard", value="\n".join(lines), inline=False)
    embed.add_field(
        name="Current Champion",
        value=(
            f"**{best_row['date']}** with **{int(best_row['net']):+d}** net growth\n"
            f"(+{int(best_row['joins'])} joins / -{int(best_row['leaves'])} leaves)"
        ),
        inline=False,
    )
    return embed


def build_vote_status_embed(
    user: discord.abc.User,
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    data = db.get_vote_user(user.id)
    active = is_vote_premium_active(user.id)
    reward_role_text = "Not configured"

    if guild is not None:
        role = get_vote_reward_role(guild)
        reward_role_text = role.mention if role else "Not configured"

    embed = build_main_embed(
        "🗳️ Vote Status",
        f"Top.gg vote rewards for **{user}**",
        discord.Color.gold() if active else discord.Color.blurple(),
    )
    embed.add_field(name="Total Votes", value=str(data["total_votes"]), inline=True)
    embed.add_field(name="Streak", value=str(data["streak"]), inline=True)
    embed.add_field(
        name="Vote Premium",
        value="Active" if active else "Inactive",
        inline=True,
    )
    embed.add_field(
        name="Last Vote",
        value=format_dt_safe(data.get("last_vote_at"), "R"),
        inline=True,
    )
    embed.add_field(
        name="Premium Until",
        value=format_dt_safe(data.get("premium_until"), "F")
        if data.get("premium_until")
        else "Not active",
        inline=True,
    )
    embed.add_field(
        name="Time Remaining",
        value=get_vote_premium_remaining_text(user.id),
        inline=True,
    )

    if guild is not None:
        embed.add_field(name="Reward Role", value=reward_role_text, inline=False)

    embed.add_field(
        name="Vote Link",
        value=f"[Vote on Top.gg]({get_topgg_vote_url()})",
        inline=False,
    )
    return embed


def build_help_embed(include_owner: bool = False) -> discord.Embed:
    embed = build_main_embed(
        f"{BOT_NAME} Help",
        "Here are the available commands.",
    )

    embed.add_field(
        name="General",
        value=(
            f"`{DEFAULT_PREFIX}ping` - Check bot latency\n"
            f"`{DEFAULT_PREFIX}help` - Show this help menu\n"
            f"`{DEFAULT_PREFIX}about` - About the bot\n"
            f"`{DEFAULT_PREFIX}invite` - Bot invite link\n"
            f"`{DEFAULT_PREFIX}stats` - Global bot stats\n"
            f"`{DEFAULT_PREFIX}serverstatus` - Current server info\n"
            f"`{DEFAULT_PREFIX}premium` - Check premium status\n"
            f"`{DEFAULT_PREFIX}vote` - Top.gg vote link\n"
            f"`{DEFAULT_PREFIX}votestatus` - Check your vote rewards"
        ),
        inline=False,
    )

    embed.add_field(
        name="Setup / Milestones",
        value=(
            f"`{DEFAULT_PREFIX}setup` - Show setup instructions\n"
            f"`{DEFAULT_PREFIX}setmilestone <member_count> @role` - Set milestone role\n"
            f"`{DEFAULT_PREFIX}removemilestone <member_count>` - Remove milestone role\n"
            f"`{DEFAULT_PREFIX}milestones` - List milestone roles\n"
            f"`{DEFAULT_PREFIX}setvoterole @role` - Set vote reward role"
        ),
        inline=False,
    )

    embed.add_field(
        name="Growth Tracking",
        value=(
            f"`{DEFAULT_PREFIX}setreport #channel` - Set daily report channel\n"
            f"`{DEFAULT_PREFIX}reportchannel` - Show report channel\n"
            f"`{DEFAULT_PREFIX}growthtoday` - Show today's growth stats\n"
            f"`{DEFAULT_PREFIX}growthweek` - Weekly growth analytics (Premium)\n"
            f"`{DEFAULT_PREFIX}bestday` - Best growth day record\n"
            f"`{DEFAULT_PREFIX}growthleaderboard` - Top server growth days\n"
            f"`{DEFAULT_PREFIX}dashboard [days]` - Premium analytics dashboard\n"
            f"`{DEFAULT_PREFIX}setalertthreshold <number>` - Set alert threshold (Premium)\n"
            f"`{DEFAULT_PREFIX}alerts on/off` - Toggle alerts (Premium)"
        ),
        inline=False,
    )

    embed.add_field(
        name="Slash Commands",
        value=(
            "`/ping` - Check bot latency\n"
            "`/help` - Show this help menu\n"
            "`/growthleaderboard` - Show top growth days\n"
            "`/dashboard` - Premium growth dashboard\n"
            "`/vote` - Get Top.gg vote link\n"
            "`/votestatus` - Check your vote rewards"
        ),
        inline=False,
    )

    if include_owner:
        embed.add_field(
            name="Owner",
            value=(
                f"`{DEFAULT_PREFIX}servers` - View install tracking and server list\n"
                f"`{DEFAULT_PREFIX}setpremium <guild_id>` - Enable premium\n"
                f"`{DEFAULT_PREFIX}removepremium <guild_id>` - Disable premium\n"
                f"`{DEFAULT_PREFIX}voteadmin` - View recent vote events\n"
                f"`{DEFAULT_PREFIX}testvote <user_id>` - Simulate a vote"
            ),
            inline=False,
        )

    return embed


async def maybe_fire_milestone(guild: discord.Guild):
    if guild is None:
        return

    settings = db.get_guild_settings(guild.id)
    milestone_roles = settings.get("milestone_roles", {})
    current_count = guild.member_count or 0

    if current_count not in milestone_roles:
        return

    role_id = milestone_roles[current_count]
    role = guild.get_role(role_id)
    if role is None:
        return

    target_member = guild.owner
    if target_member is None:
        return

    try:
        if role not in target_member.roles:
            await target_member.add_roles(
                role,
                reason=f"{BOT_NAME} milestone reached: {current_count} members",
            )
    except discord.Forbidden:
        log.warning("Missing permissions to assign milestone role in guild %s", guild.id)
    except discord.HTTPException as e:
        log.warning("Failed assigning milestone role in guild %s: %s", guild.id, e)


async def send_daily_report_for_guild(guild: discord.Guild, report_day_str: str):
    channel = get_report_channel(guild)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return

    perms = channel.permissions_for(guild.me)
    if not perms.send_messages or not perms.embed_links:
        return

    stats = db.get_growth_for_date(guild.id, report_day_str)
    joins = stats["joins"]
    leaves = stats["leaves"]
    net = stats["net"]

    embed = build_main_embed(
        "📊 Daily Server Report",
        f"Report for **{report_day_str} UTC**",
        discord.Color.green()
        if net > 0
        else discord.Color.orange()
        if net < 0
        else discord.Color.blurple(),
    )
    embed.add_field(name="Joins", value=f"+{joins}", inline=True)
    embed.add_field(name="Leaves", value=f"-{leaves}", inline=True)
    embed.add_field(name="Net Growth", value=f"{net:+d}", inline=True)
    embed.add_field(
        name="Message",
        value=growth_message_for_stats(joins, leaves),
        inline=False,
    )

    try:
        await channel.send(embed=embed)
        db.set_last_daily_report_date(guild.id, report_day_str)
        db.set_last_alert_net(guild.id, None)
    except discord.HTTPException as e:
        log.warning("Failed sending daily report in guild %s: %s", guild.id, e)


async def maybe_send_growth_alert(guild: discord.Guild):
    settings = db.get_guild_settings(guild.id)
    if not settings["premium"]:
        return
    if not settings["alerts_enabled"]:
        return

    channel = get_report_channel(guild)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return

    perms = channel.permissions_for(guild.me)
    if not perms.send_messages or not perms.embed_links:
        return

    today = current_utc_day_str()
    stats = db.get_growth_for_date(guild.id, today)
    net = stats["net"]
    threshold = max(1, int(settings.get("growth_alert_threshold", 25)))
    last_alert_net = settings.get("last_alert_net")

    if -threshold < net < threshold:
        if last_alert_net is not None:
            db.set_last_alert_net(guild.id, None)
        return

    if net >= threshold:
        if last_alert_net == threshold:
            return

        embed = build_main_embed(
            "🚀 Growth Alert",
            f"Your server has reached **{net:+d}** net growth today.",
            discord.Color.green(),
        )
        embed.add_field(
            name="Today",
            value=f"+{stats['joins']} joins / -{stats['leaves']} leaves",
            inline=False,
        )
        embed.add_field(
            name="Threshold",
            value=f"Alert threshold: **+{threshold}**",
            inline=False,
        )

        try:
            await channel.send(embed=embed)
            db.set_last_alert_net(guild.id, threshold)
        except discord.HTTPException:
            pass
        return

    if net <= -threshold:
        if last_alert_net == -threshold:
            return

        embed = build_main_embed(
            "⚠️ Drop Alert",
            f"Your server has reached **{net:+d}** net growth today.",
            discord.Color.red(),
        )
        embed.add_field(
            name="Today",
            value=f"+{stats['joins']} joins / -{stats['leaves']} leaves",
            inline=False,
        )
        embed.add_field(
            name="Threshold",
            value=f"Alert threshold: **-{threshold}**",
            inline=False,
        )

        try:
            await channel.send(embed=embed)
            db.set_last_alert_net(guild.id, -threshold)
        except discord.HTTPException:
            pass


async def sync_vote_reward_role_for_member(member: discord.Member):
    role = get_vote_reward_role(member.guild)
    if role is None:
        return

    active = is_vote_premium_active(member.id)
    try:
        if active and role not in member.roles:
            await member.add_roles(role, reason=f"{BOT_NAME} vote premium active")
        elif not active and role in member.roles:
            await member.remove_roles(role, reason=f"{BOT_NAME} vote premium expired")
    except discord.Forbidden:
        log.warning("Missing permissions to manage vote reward role in guild %s", member.guild.id)
    except discord.HTTPException as e:
        log.warning("Failed syncing vote reward role in guild %s: %s", member.guild.id, e)


async def sync_vote_reward_roles_for_user(user_id: int):
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member is not None:
            await sync_vote_reward_role_for_member(member)


async def sync_all_vote_reward_roles():
    for guild in bot.guilds:
        role = get_vote_reward_role(guild)
        if role is None:
            continue

        for member in guild.members:
            if member.bot:
                continue
            await sync_vote_reward_role_for_member(member)


def calculate_next_vote_streak(old_last_vote_at: Optional[str], old_streak: int) -> int:
    if not old_last_vote_at:
        return 1

    last_dt = iso_to_dt(old_last_vote_at)
    if last_dt is None:
        return 1

    old_date = last_dt.date()
    new_date = datetime.now(UTC).date()
    diff = (new_date - old_date).days

    if diff <= 0:
        return max(1, old_streak)
    if diff == 1:
        return max(1, old_streak) + 1
    return 1


async def process_topgg_vote(user_id: int, payload: dict, source: str = "topgg") -> dict:
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except Exception:
            user = None

    username = str(user) if user else payload.get("username") or payload.get("user")
    is_weekend = bool(payload.get("isWeekend") or payload.get("is_weekend"))
    now_dt = datetime.now(UTC)
    now_iso = now_dt.isoformat()

    current = db.get_vote_user(user_id)
    new_total_votes = int(current["total_votes"]) + 1
    new_streak = calculate_next_vote_streak(
        current.get("last_vote_at"),
        int(current.get("streak") or 0),
    )

    current_until = iso_to_dt(current.get("premium_until"))
    base_dt = max(now_dt, current_until) if current_until else now_dt

    added_hours = TOPGG_VOTE_PREMIUM_HOURS
    if is_weekend:
        added_hours *= 2

    new_premium_until = (base_dt + timedelta(hours=added_hours)).isoformat()

    db.set_vote_user(
        user_id=user_id,
        total_votes=new_total_votes,
        streak=new_streak,
        last_vote_at=now_iso,
        premium_until=new_premium_until,
        last_vote_source=source,
    )

    db.record_vote_event(
        user_id=user_id,
        username=username,
        source=source,
        is_weekend=is_weekend,
        voted_at=now_iso,
        raw_payload=payload,
    )

    db.increment_stat("topgg_votes_total", 1)
    await sync_vote_reward_roles_for_user(user_id)

    return {
        "user_id": user_id,
        "username": username,
        "is_weekend": is_weekend,
        "total_votes": new_total_votes,
        "streak": new_streak,
        "premium_until": new_premium_until,
        "added_hours": added_hours,
    }


# =========================
# WEB SERVER
# =========================
web_app: Optional[web.Application] = None
web_runner: Optional[web.AppRunner] = None
web_site: Optional[web.TCPSite] = None


async def healthcheck_handler(request: web.Request):
    return web.json_response({"ok": True, "bot": str(bot.user) if bot.user else None})


async def topgg_vote_handler(request: web.Request):
    if TOPGG_WEBHOOK_AUTH:
        auth = request.headers.get("Authorization", "")
        if auth != TOPGG_WEBHOOK_AUTH:
            return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    raw_user = payload.get("user") or payload.get("id")
    if raw_user is None:
        return web.json_response({"ok": False, "error": "Missing user"}, status=400)

    try:
        user_id = int(raw_user)
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid user"}, status=400)

    vote_type = str(payload.get("type", "upvote")).lower()
    if vote_type not in {"upvote", "test"}:
        return web.json_response({"ok": False, "error": "Unsupported vote type"}, status=400)

    try:
        result = await process_topgg_vote(user_id, payload, source=f"topgg_{vote_type}")
        return web.json_response({"ok": True, "result": result})
    except Exception as e:
        log.exception("Top.gg vote processing failed: %s", e)
        return web.json_response({"ok": False, "error": "Internal error"}, status=500)


async def start_web_server():
    global web_app, web_runner, web_site

    if web_runner is not None:
        return

    app = web.Application()
    app.router.add_get("/", healthcheck_handler)
    app.router.add_get("/health", healthcheck_handler)
    app.router.add_post(TOPGG_WEBHOOK_ROUTE, topgg_vote_handler)

    if TOPGG_WEBHOOK_ROUTE != "/topgg":
        app.router.add_post("/topgg", topgg_vote_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, TOPGG_WEB_HOST, TOPGG_WEB_PORT)
    await site.start()

    web_app = app
    web_runner = runner
    web_site = site
    log.info(
        "Webhook server started on %s:%s route=%s",
        TOPGG_WEB_HOST,
        TOPGG_WEB_PORT,
        TOPGG_WEBHOOK_ROUTE,
    )


# =========================
# BACKGROUND TASKS
# =========================
@tasks.loop(time=DAILY_REPORT_TIME_UTC)
async def daily_reports_loop():
    report_day_str = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()

    for guild in bot.guilds:
        try:
            settings = db.get_guild_settings(guild.id)
            if not settings.get("report_channel_id"):
                continue
            if settings.get("last_daily_report_date") == report_day_str:
                continue
            await send_daily_report_for_guild(guild, report_day_str)
        except Exception as e:
            log.warning("Daily report loop failed for guild %s: %s", guild.id, e)


@daily_reports_loop.before_loop
async def before_daily_reports_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def vote_reward_role_loop():
    try:
        await sync_all_vote_reward_roles()
    except Exception as e:
        log.warning("Vote reward sync loop failed: %s", e)


@vote_reward_role_loop.before_loop
async def before_vote_reward_role_loop():
    await bot.wait_until_ready()


# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")

    for guild in bot.guilds:
        db.ensure_guild(guild.id)

    if not daily_reports_loop.is_running():
        daily_reports_loop.start()

    if not vote_reward_role_loop.is_running():
        vote_reward_role_loop.start()

    try:
        await start_web_server()
    except Exception as e:
        log.warning("Failed starting webhook server: %s", e)

    try:
        synced = await bot.tree.sync()
        log.info("Synced %s application commands.", len(synced))
    except Exception as e:
        log.warning("App command sync failed: %s", e)


@bot.event
async def on_guild_join(guild: discord.Guild):
    db.ensure_guild(guild.id)
    db.increment_stat("join_count", 1)
    db.record_install_event(
        guild_id=guild.id,
        guild_name=guild.name,
        event_type="join",
        member_count=guild.member_count or 0,
    )
    log.info("Joined guild: %s (%s)", guild.name, guild.id)


@bot.event
async def on_guild_remove(guild: discord.Guild):
    db.increment_stat("remove_count", 1)
    db.record_install_event(
        guild_id=guild.id,
        guild_name=guild.name,
        event_type="remove",
        member_count=guild.member_count or 0,
    )
    db.remove_guild(guild.id)
    log.info("Removed from guild: %s (%s)", guild.name, guild.id)


@bot.event
async def on_member_join(member: discord.Member):
    try:
        today = current_utc_day_str()
        db.increment_growth(member.guild.id, today, joins=1, leaves=0)
        await maybe_fire_milestone(member.guild)
        await maybe_send_growth_alert(member.guild)
        await sync_vote_reward_role_for_member(member)
    except Exception as e:
        log.warning("on_member_join handling failed in guild %s: %s", member.guild.id, e)


@bot.event
async def on_member_remove(member: discord.Member):
    try:
        if member.guild is None:
            return
        today = current_utc_day_str()
        db.increment_growth(member.guild.id, today, joins=0, leaves=1)
        await maybe_send_growth_alert(member.guild)
    except Exception as e:
        log.warning(
            "on_member_remove handling failed in guild %s: %s",
            getattr(member.guild, "id", "unknown"),
            e,
        )


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.CheckFailure):
        return await ctx.send(
            embed=build_main_embed(
                "Access Denied",
                str(error),
                discord.Color.red(),
            )
        )

    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(
            embed=build_main_embed(
                "Missing Argument",
                f"You are missing a required argument: `{error.param.name}`",
                discord.Color.orange(),
            )
        )

    if isinstance(error, commands.BadArgument):
        return await ctx.send(
            embed=build_main_embed(
                "Invalid Argument",
                "One or more arguments were invalid. Please check your command and try again.",
                discord.Color.orange(),
            )
        )

    log.exception("Unhandled command error: %s", error)
    await ctx.send(
        embed=build_main_embed(
            "Error",
            "Something went wrong while running that command.",
            discord.Color.red(),
        )
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    log.exception("Slash command error: %s", error)

    message = "Something went wrong while running that slash command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass


# =========================
# PREFIX COMMANDS
# =========================
@bot.command(name="ping")
async def ping_command(ctx: commands.Context):
    latency = round(bot.latency * 1000)
    embed = build_main_embed(
        "🏓 Pong!",
        f"Latency: **{latency} ms**",
        discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    embed = build_help_embed(include_owner=is_owner_user(ctx.author.id))
    await ctx.send(embed=embed)


@bot.command(name="setup")
@admin_or_manage_guild()
async def setup_command(ctx: commands.Context):
    db.ensure_guild(ctx.guild.id)
    settings = db.get_guild_settings(ctx.guild.id)

    report_channel_text = (
        f"<#{settings['report_channel_id']}>"
        if settings.get("report_channel_id")
        else "Not set"
    )

    vote_role_text = (
        f"<@&{settings['vote_reward_role_id']}>"
        if settings.get("vote_reward_role_id")
        else "Not set"
    )

    embed = build_main_embed(
        f"{BOT_NAME} Setup",
        "Configure milestone roles, premium info, growth reporting, and vote rewards for this server.",
    )

    embed.add_field(
        name="Milestone Setup",
        value=(
            f"Use `{DEFAULT_PREFIX}setmilestone <member_count> @role` to assign a role "
            "when your server reaches a specific member count.\n"
            f"Use `{DEFAULT_PREFIX}removemilestone <member_count>` to remove one.\n"
            f"Use `{DEFAULT_PREFIX}milestones` to view current milestone roles."
        ),
        inline=False,
    )

    embed.add_field(
        name="Growth Reports",
        value=(
            f"Use `{DEFAULT_PREFIX}setreport #channel` to choose where daily reports are sent.\n"
            f"Current report channel: {report_channel_text}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Vote Rewards",
        value=(
            f"Use `{DEFAULT_PREFIX}setvoterole @role` to assign a temporary reward role for active voters.\n"
            f"Current vote reward role: {vote_role_text}\n"
            f"Use `{DEFAULT_PREFIX}vote` to get the Top.gg vote link."
        ),
        inline=False,
    )

    embed.add_field(
        name="Premium",
        value=(
            f"Use `{DEFAULT_PREFIX}premium` to view this server's premium status.\n"
            "Premium unlocks weekly growth stats and live alerts."
        ),
        inline=False,
    )

    await ctx.send(embed=embed)


@bot.command(name="about")
async def about_command(ctx: commands.Context):
    embed = build_main_embed(
        f"About {BOT_NAME}",
        f"{BOT_NAME} is a multi-server Discord bot with premium support, milestone role tools, install tracking, growth notifications, Top.gg vote rewards, and a growth leaderboard.",
    )
    embed.add_field(name="Prefix", value=f"`{DEFAULT_PREFIX}`", inline=True)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(
        name="Support",
        value=f"[Join Support Server]({SUPPORT_SERVER_URL})",
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="invite")
async def invite_command(ctx: commands.Context):
    embed = build_main_embed(
        f"Invite {BOT_NAME}",
        f"[Click here to invite {BOT_NAME}]({BOT_INVITE_URL})",
    )
    embed.add_field(
        name="Support",
        value=f"[Support Server]({SUPPORT_SERVER_URL})",
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="vote")
async def vote_command(ctx: commands.Context):
    embed = build_main_embed(
        "🗳️ Vote for Legacy Bot",
        f"[Click here to vote on Top.gg]({get_topgg_vote_url()})",
        discord.Color.gold(),
    )
    embed.add_field(
        name="Reward",
        value=f"Each vote grants **{TOPGG_VOTE_PREMIUM_HOURS} hours** of temporary vote premium.",
        inline=False,
    )
    embed.add_field(
        name="Bonus",
        value="If Top.gg marks the vote as weekend, the premium time is doubled automatically.",
        inline=False,
    )

    if ctx.guild is not None:
        role = get_vote_reward_role(ctx.guild)
        embed.add_field(
            name="This Server's Reward Role",
            value=role.mention if role else "Not configured",
            inline=False,
        )

    await ctx.send(embed=embed)


@bot.command(name="votestatus")
async def votestatus_command(
    ctx: commands.Context,
    member: Optional[discord.Member] = None,
):
    target = member or ctx.author
    embed = build_vote_status_embed(target, ctx.guild)
    await ctx.send(embed=embed)


@bot.command(name="stats")
async def stats_command(ctx: commands.Context):
    join_count = db.get_stat("join_count")
    remove_count = db.get_stat("remove_count")
    current_servers = len(bot.guilds)
    net_installs = join_count - remove_count
    total_members = total_member_estimate()
    total_votes = db.get_stat("topgg_votes_total")

    embed = build_main_embed(
        f"{BOT_NAME} Stats",
        "Global bot statistics.",
    )
    embed.add_field(name="Current Servers", value=str(current_servers), inline=True)
    embed.add_field(name="Join Events", value=str(join_count), inline=True)
    embed.add_field(name="Remove Events", value=str(remove_count), inline=True)
    embed.add_field(name="Net Installs", value=str(net_installs), inline=True)
    embed.add_field(name="Users Reached", value=str(total_members), inline=True)
    embed.add_field(name="Top.gg Votes", value=str(total_votes), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="serverstatus")
async def serverstatus_command(ctx: commands.Context):
    settings = db.get_guild_settings(ctx.guild.id)
    milestone_roles = settings.get("milestone_roles", {})

    report_channel_display = (
        f"<#{settings['report_channel_id']}>"
        if settings.get("report_channel_id")
        else "Not Set"
    )
    vote_role_display = (
        f"<@&{settings['vote_reward_role_id']}>"
        if settings.get("vote_reward_role_id")
        else "Not Set"
    )

    embed = build_main_embed(
        f"Server Status - {ctx.guild.name}",
        "Current server information.",
    )
    embed.add_field(name="Server ID", value=str(ctx.guild.id), inline=True)
    embed.add_field(name="Members", value=str(ctx.guild.member_count or 0), inline=True)
    embed.add_field(name="Premium", value="Yes" if settings["premium"] else "No", inline=True)
    embed.add_field(
        name="Owner",
        value=str(ctx.guild.owner) if ctx.guild.owner else "Unknown",
        inline=True,
    )
    embed.add_field(name="Report Channel", value=report_channel_display, inline=True)
    embed.add_field(name="Milestone Roles", value=str(len(milestone_roles)), inline=True)
    embed.add_field(name="Alerts Enabled", value="Yes" if settings["alerts_enabled"] else "No", inline=True)
    embed.add_field(name="Vote Reward Role", value=vote_role_display, inline=True)
    embed.add_field(
        name="Created",
        value=discord.utils.format_dt(ctx.guild.created_at, style="F"),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="premium")
async def premium_command(ctx: commands.Context):
    settings = db.get_guild_settings(ctx.guild.id)

    embed = build_main_embed(
        "Premium Status",
        f"This server premium status is: **{'Enabled' if settings['premium'] else 'Disabled'}**",
        discord.Color.gold() if settings["premium"] else discord.Color.blurple(),
    )

    embed.add_field(
        name="Server Premium Features",
        value=(
            "• Weekly growth report command\n"
            "• Real-time growth/drop alerts\n"
            "• Custom growth alert threshold\n"
            "• Alert toggle controls"
        ),
        inline=False,
    )

    embed.add_field(
        name="Your Vote Premium",
        value=(
            f"**Active** — {get_vote_premium_remaining_text(ctx.author.id)}"
            if is_vote_premium_active(ctx.author.id)
            else "Inactive — vote to unlock temporary personal perks and reward role access"
        ),
        inline=False,
    )

    await ctx.send(embed=embed)


@bot.command(name="setmilestone")
@admin_or_manage_guild()
async def setmilestone_command(
    ctx: commands.Context,
    member_count: int,
    role: discord.Role,
):
    if member_count <= 0:
        return await ctx.send(
            embed=build_main_embed(
                "Invalid Member Count",
                "Member count must be greater than 0.",
                discord.Color.red(),
            )
        )

    db.set_milestone_role(ctx.guild.id, member_count, role.id)
    embed = build_main_embed(
        "Milestone Saved",
        f"At **{member_count}** members, the role {role.mention} will be assigned to the server owner.",
        discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="removemilestone")
@admin_or_manage_guild()
async def removemilestone_command(ctx: commands.Context, member_count: int):
    db.remove_milestone_role(ctx.guild.id, member_count)
    embed = build_main_embed(
        "Milestone Removed",
        f"Removed milestone role for **{member_count}** members.",
        discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="milestones")
async def milestones_command(ctx: commands.Context):
    mapping = db.get_milestone_roles(ctx.guild.id)

    if not mapping:
        return await ctx.send(
            embed=build_main_embed(
                "Milestone Roles",
                "No milestone roles have been configured for this server yet.",
            )
        )

    lines = []
    for member_count in sorted(mapping.keys()):
        role = ctx.guild.get_role(mapping[member_count])
        role_text = role.mention if role else f"`Deleted Role ({mapping[member_count]})`"
        lines.append(f"**{member_count} members** → {role_text}")

    embed = build_main_embed(
        "Milestone Roles",
        "\n".join(lines),
    )
    await ctx.send(embed=embed)


@bot.command(name="setreport")
@admin_or_manage_guild()
async def setreport_command(ctx: commands.Context, channel: discord.TextChannel):
    perms = channel.permissions_for(ctx.guild.me)
    if not perms.send_messages or not perms.embed_links:
        return await ctx.send(
            embed=build_main_embed(
                "Missing Permissions",
                f"I need **Send Messages** and **Embed Links** in {channel.mention}.",
                discord.Color.red(),
            )
        )

    db.set_report_channel(ctx.guild.id, channel.id)
    embed = build_main_embed(
        "Report Channel Updated",
        f"Daily growth reports will be sent in {channel.mention}.",
        discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="setvoterole")
@admin_or_manage_guild()
async def setvoterole_command(
    ctx: commands.Context,
    role: Optional[discord.Role] = None,
):
    if role is None:
        db.set_vote_reward_role(ctx.guild.id, None)
        return await ctx.send(
            embed=build_main_embed(
                "Vote Reward Role Cleared",
                "The vote reward role has been cleared for this server.",
                discord.Color.orange(),
            )
        )

    db.set_vote_reward_role(ctx.guild.id, role.id)

    for member in ctx.guild.members:
        if member.bot:
            continue
        await sync_vote_reward_role_for_member(member)

    embed = build_main_embed(
        "Vote Reward Role Updated",
        f"Active Top.gg voters will receive {role.mention} while their vote premium is active.",
        discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="reportchannel")
async def reportchannel_command(ctx: commands.Context):
    settings = db.get_guild_settings(ctx.guild.id)
    channel_id = settings.get("report_channel_id")

    if not channel_id:
        return await ctx.send(
            embed=build_main_embed(
                "Report Channel",
                f"No report channel has been set yet. Use `{DEFAULT_PREFIX}setreport #channel`.",
                discord.Color.orange(),
            )
        )

    channel = ctx.guild.get_channel(channel_id)
    channel_text = channel.mention if channel else f"`Deleted Channel ({channel_id})`"

    embed = build_main_embed(
        "Report Channel",
        f"Daily growth reports are set to {channel_text}.",
        discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="growthtoday")
async def growthtoday_command(ctx: commands.Context):
    stats = db.get_growth_for_date(ctx.guild.id, current_utc_day_str())
    embed = build_main_embed(
        "📊 Today's Growth",
        f"Tracking for **{current_utc_day_str()} UTC**",
        discord.Color.green()
        if stats["net"] > 0
        else discord.Color.orange()
        if stats["net"] < 0
        else discord.Color.blurple(),
    )
    embed.add_field(name="Joins", value=f"+{stats['joins']}", inline=True)
    embed.add_field(name="Leaves", value=f"-{stats['leaves']}", inline=True)
    embed.add_field(name="Net Growth", value=f"{stats['net']:+d}", inline=True)
    embed.add_field(
        name="Message",
        value=growth_message_for_stats(stats["joins"], stats["leaves"]),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="growthweek")
@premium_required()
async def growthweek_command(ctx: commands.Context):
    end_date_obj = datetime.now(UTC).date()
    start_date_obj = end_date_obj - timedelta(days=6)

    stats = db.get_growth_range(
        ctx.guild.id,
        start_date_obj.isoformat(),
        end_date_obj.isoformat(),
    )
    top_days = db.get_top_growth_days(ctx.guild.id, limit=3)

    embed = build_main_embed(
        "📈 Weekly Growth Report",
        f"Stats from **{start_date_obj.isoformat()}** to **{end_date_obj.isoformat()}** UTC",
        discord.Color.gold(),
    )
    embed.add_field(name="Joins", value=f"+{stats['joins']}", inline=True)
    embed.add_field(name="Leaves", value=f"-{stats['leaves']}", inline=True)
    embed.add_field(name="Net Growth", value=f"{stats['net']:+d}", inline=True)

    if top_days:
        lines = []
        for row in top_days:
            lines.append(
                f"**{row['date']}** • Net {int(row['net']):+d} "
                f"(+{int(row['joins'])} / -{int(row['leaves'])})"
            )
        embed.add_field(name="Best Growth Days", value="\n".join(lines), inline=False)

    await ctx.send(embed=embed)


@bot.command(name="bestday")
async def bestday_command(ctx: commands.Context):
    data = db.get_best_growth_day(ctx.guild.id)

    if not data:
        return await ctx.send(
            embed=build_main_embed(
                "🏆 Best Growth Day",
                "No growth data recorded yet.",
                discord.Color.blurple(),
            )
        )

    embed = build_main_embed(
        "🏆 Best Growth Day",
        f"**{data['net']:+d} members** on **{data['date']}**",
        discord.Color.gold(),
    )
    embed.add_field(name="Joins", value=f"+{data['joins']}", inline=True)
    embed.add_field(name="Leaves", value=f"-{data['leaves']}", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="growthleaderboard")
async def growthleaderboard_command(ctx: commands.Context):
    embed = build_growth_leaderboard_embed(ctx.guild)
    await ctx.send(embed=embed)


@bot.command(name="setalertthreshold")
@admin_or_manage_guild()
@premium_required()
async def setalertthreshold_command(ctx: commands.Context, threshold: int):
    if threshold <= 0:
        return await ctx.send(
            embed=build_main_embed(
                "Invalid Threshold",
                "Threshold must be greater than 0.",
                discord.Color.red(),
            )
        )

    db.set_growth_alert_threshold(ctx.guild.id, threshold)
    db.set_last_alert_net(ctx.guild.id, None)

    embed = build_main_embed(
        "Alert Threshold Updated",
        f"Growth alerts will now trigger at **±{threshold}** net growth in one UTC day.",
        discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="alerts")
@admin_or_manage_guild()
@premium_required()
async def alerts_command(ctx: commands.Context, state: str):
    normalized = state.lower().strip()
    if normalized not in {"on", "off"}:
        return await ctx.send(
            embed=build_main_embed(
                "Invalid Option",
                f"Use `{DEFAULT_PREFIX}alerts on` or `{DEFAULT_PREFIX}alerts off`.",
                discord.Color.orange(),
            )
        )

    enabled = normalized == "on"
    db.set_alerts_enabled(ctx.guild.id, enabled)
    if enabled:
        db.set_last_alert_net(ctx.guild.id, None)

    embed = build_main_embed(
        "Alerts Updated",
        f"Growth alerts are now **{'enabled' if enabled else 'disabled'}** for this server.",
        discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="senddailyreport")
@admin_or_manage_guild()
async def senddailyreport_command(ctx: commands.Context):
    settings = db.get_guild_settings(ctx.guild.id)
    if not settings.get("report_channel_id"):
        return await ctx.send(
            embed=build_main_embed(
                "Report Channel Not Set",
                f"Use `{DEFAULT_PREFIX}setreport #channel` first.",
                discord.Color.orange(),
            )
        )

    report_day_str = yesterday_utc_day_str()
    await send_daily_report_for_guild(ctx.guild, report_day_str)
    await ctx.send(
        embed=build_main_embed(
            "Daily Report Sent",
            f"Attempted to send the daily report for **{report_day_str} UTC**.",
            discord.Color.green(),
        )
    )

@bot.command(name="dashboard")
@premium_required()
async def dashboard_command(ctx: commands.Context, days: Optional[int] = 7):
    days = max(3, min(int(days or 7), 30))
    chart_buffer = generate_dashboard_chart(ctx.guild, days)
    file = discord.File(chart_buffer, filename="dashboard_growth.png")
    embed = build_dashboard_embed(ctx.guild, days)
    await ctx.send(embed=embed, file=file)




# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="ping", description="Check bot latency")
async def ping_slash(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    embed = build_main_embed(
        "🏓 Pong!",
        f"Latency: **{latency} ms**",
        discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Show the bot help menu")
async def help_slash(interaction: discord.Interaction):
    embed = build_help_embed(include_owner=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="growthleaderboard", description="Show this server's top growth days")
async def growthleaderboard_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    embed = build_growth_leaderboard_embed(interaction.guild)
    await interaction.response.send_message(embed=embed)



@bot.tree.command(name="dashboard", description="Premium growth dashboard for this server")
@app_commands.describe(days="How many days to show on the chart (3-30)")
async def dashboard_slash(interaction: discord.Interaction, days: Optional[int] = 7):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    settings = db.get_guild_settings(interaction.guild.id)
    if not settings["premium"]:
        return await interaction.response.send_message(
            "🚫 This dashboard is a premium-only feature for this server.",
            ephemeral=True,
        )

    days = max(3, min(int(days or 7), 30))
    chart_buffer = generate_dashboard_chart(interaction.guild, days)
    file = discord.File(chart_buffer, filename="dashboard_growth.png")
    embed = build_dashboard_embed(interaction.guild, days)
    await interaction.response.send_message(embed=embed, file=file, ephemeral=True)


@bot.tree.command(name="vote", description="Get the Top.gg vote link")
async def vote_slash(interaction: discord.Interaction):
    embed = build_main_embed(
        "🗳️ Vote for Legacy Bot",
        f"[Click here to vote on Top.gg]({get_topgg_vote_url()})",
        discord.Color.gold(),
    )
    embed.add_field(
        name="Reward",
        value=f"Each vote grants **{TOPGG_VOTE_PREMIUM_HOURS} hours** of temporary vote premium.",
        inline=False,
    )

    if interaction.guild is not None:
        role = get_vote_reward_role(interaction.guild)
        embed.add_field(
            name="This Server's Reward Role",
            value=role.mention if role else "Not configured",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="votestatus", description="Check your Top.gg vote rewards")
async def votestatus_slash(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
):
    target = member or interaction.user
    embed = build_vote_status_embed(target, interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================
# OWNER COMMANDS
# =========================
@bot.command(name="amowner")
async def amowner_command(ctx: commands.Context):
    await ctx.send(
        f"Your ID: {ctx.author.id}\nOWNER_IDS: {OWNER_IDS}\nOwner: {ctx.author.id in OWNER_IDS}"
    )


@bot.command(name="setpremium")
@owner_only()
async def setpremium_command(ctx: commands.Context, guild_id: int):
    db.set_premium(guild_id, True)
    embed = build_main_embed(
        "Premium Enabled",
        f"Premium has been enabled for guild ID `{guild_id}`.",
        discord.Color.gold(),
    )
    await ctx.send(embed=embed)


@bot.command(name="removepremium")
@owner_only()
async def removepremium_command(ctx: commands.Context, guild_id: int):
    db.set_premium(guild_id, False)
    embed = build_main_embed(
        "Premium Disabled",
        f"Premium has been disabled for guild ID `{guild_id}`.",
        discord.Color.orange(),
    )
    await ctx.send(embed=embed)


@bot.command(name="testvote")
@owner_only()
async def testvote_command(ctx: commands.Context, user_id: int):
    payload = {
        "user": str(user_id),
        "type": "test",
        "isWeekend": False,
        "manual": True,
    }
    result = await process_topgg_vote(user_id, payload, source="manual_testvote")

    embed = build_main_embed(
        "Test Vote Processed",
        f"Processed a simulated vote for `{user_id}`.",
        discord.Color.green(),
    )
    embed.add_field(name="Total Votes", value=str(result["total_votes"]), inline=True)
    embed.add_field(name="Streak", value=str(result["streak"]), inline=True)
    embed.add_field(
        name="Premium Until",
        value=format_dt_safe(result["premium_until"], "F"),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="voteadmin")
@owner_only()
async def voteadmin_command(ctx: commands.Context):
    events = db.get_recent_vote_events(limit=10)
    top_rows = db.get_top_voters(limit=10)
    total_votes = db.get_stat("topgg_votes_total")

    embed = build_main_embed(
        "Top.gg Vote Admin",
        f"Total recorded votes: **{total_votes}**",
        discord.Color.gold(),
    )

    if top_rows:
        lines = []
        for idx, row in enumerate(top_rows, start=1):
            lines.append(
                f"{medal_for_rank(idx)} `<@{row['user_id']}>` • **{int(row['total_votes'])}** votes • streak **{int(row['streak'])}**"
            )
        embed.add_field(name="Top Voters", value="\n".join(lines), inline=False)

    if events:
        event_lines = []
        for event in events:
            event_lines.append(
                safe_truncate(
                    f"• **{event['source']}** • user `{event['user_id']}` • {format_dt_safe(event['voted_at'], 'R')}",
                    1000,
                )
            )
        embed.add_field(
            name="Recent Vote Events",
            value="\n".join(event_lines),
            inline=False,
        )

    await ctx.send(embed=embed)


@bot.command(name="servers")
@owner_only()
async def servers_command(ctx: commands.Context):
    join_count = db.get_stat("join_count")
    remove_count = db.get_stat("remove_count")
    current_servers = len(bot.guilds)

    guild_lines = []
    sorted_guilds = sorted(
        bot.guilds,
        key=lambda g: g.member_count or 0,
        reverse=True,
    )

    for guild in sorted_guilds[:20]:
        settings = db.get_guild_settings(guild.id)
        premium_tag = " | Premium" if settings["premium"] else ""
        report_tag = " | Reports" if settings.get("report_channel_id") else ""
        alerts_tag = (
            " | Alerts"
            if settings["premium"] and settings["alerts_enabled"]
            else ""
        )
        vote_role_tag = " | VoteRole" if settings.get("vote_reward_role_id") else ""

        line = (
            f"`{guild.id}` • **{guild.name}** • {guild.member_count or 0} members"
            f"{premium_tag}{report_tag}{alerts_tag}{vote_role_tag}"
        )
        guild_lines.append(safe_truncate(line, 1000))

    recent_events = db.get_recent_install_events(limit=8)
    event_lines = []
    for event in recent_events:
        symbol = "➕" if event["event_type"] == "join" else "➖"
        try:
            ts = datetime.fromisoformat(event["timestamp"])
            ts_text = discord.utils.format_dt(ts, style="R")
        except Exception:
            ts_text = event["timestamp"]

        event_lines.append(
            safe_truncate(
                f"{symbol} **{event['guild_name']}** (`{event['guild_id']}`) • {event['member_count']} members • {ts_text}",
                1000,
            )
        )

    embed = build_main_embed(
        "Installed Servers",
        f"Tracking installs for {BOT_NAME}.",
    )
    embed.add_field(name="Current Servers", value=str(current_servers), inline=True)
    embed.add_field(name="Join Events", value=str(join_count), inline=True)
    embed.add_field(name="Remove Events", value=str(remove_count), inline=True)
    embed.add_field(
        name="Server List",
        value="\n".join(guild_lines) if guild_lines else "No servers found.",
        inline=False,
    )
    embed.add_field(
        name="Recent Install Events",
        value="\n".join(event_lines) if event_lines else "No install events recorded yet.",
        inline=False,
    )

    await ctx.send(embed=embed)


# =========================
# START
# =========================
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing.")

bot.run(TOKEN)