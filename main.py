import os
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta, time as dt_time

import discord
from discord.ext import commands, tasks

# =========================
# CONFIG
# =========================
BOT_NAME = "Legacy Bot"
DEFAULT_PREFIX = "!"
DATABASE_PATH = os.getenv("DATABASE_PATH", "legacy_bot.db")
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or ""
OWNER_IDS = {207279875902537731}
SUPPORT_SERVER_URL = os.getenv("SUPPORT_SERVER_URL", "https://discord.gg/your-support-server")
BOT_INVITE_URL = os.getenv(
    "BOT_INVITE_URL",
    "https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=8&scope=bot%20applications.commands",
)

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
        self.conn = sqlite3.connect(self.path)
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
                    alerts_enabled INTEGER NOT NULL DEFAULT 1
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

        # Safe migrations for older DBs
        if not self._column_exists("guild_settings", "report_channel_id"):
            with self.conn:
                self.conn.execute("ALTER TABLE guild_settings ADD COLUMN report_channel_id INTEGER")
        if not self._column_exists("guild_settings", "last_daily_report_date"):
            with self.conn:
                self.conn.execute("ALTER TABLE guild_settings ADD COLUMN last_daily_report_date TEXT")
        if not self._column_exists("guild_settings", "growth_alert_threshold"):
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE guild_settings ADD COLUMN growth_alert_threshold INTEGER NOT NULL DEFAULT 25"
                )
        if not self._column_exists("guild_settings", "last_alert_net"):
            with self.conn:
                self.conn.execute("ALTER TABLE guild_settings ADD COLUMN last_alert_net INTEGER")
        if not self._column_exists("guild_settings", "alerts_enabled"):
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE guild_settings ADD COLUMN alerts_enabled INTEGER NOT NULL DEFAULT 1"
                )

        self._ensure_stat("join_count", 0)
        self._ensure_stat("remove_count", 0)

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
                        alerts_enabled
                    )
                    VALUES (?, 0, '{}', ?, NULL, NULL, 25, NULL, 1)
                    """,
                    (guild_id, datetime.now(UTC).isoformat()),
                )

    def remove_guild(self, guild_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
            self.conn.execute("DELETE FROM growth_stats WHERE guild_id = ?", (guild_id,))

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
                "report_channel_id": None,
                "last_daily_report_date": None,
                "growth_alert_threshold": 25,
                "last_alert_net": None,
                "alerts_enabled": True,
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

    def set_report_channel(self, guild_id: int, channel_id: int | None):
        self.ensure_guild(guild_id)
        with self.conn:
            self.conn.execute(
                "UPDATE guild_settings SET report_channel_id = ? WHERE guild_id = ?",
                (channel_id, guild_id),
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

    def set_last_alert_net(self, guild_id: int, net_value: int | None):
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

    def record_install_event(self, guild_id: int, guild_name: str, event_type: str, member_count: int):
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

    def increment_growth(self, guild_id: int, day_str: str, joins: int = 0, leaves: int = 0):
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
            ORDER BY net DESC, joins DESC
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


db = Database(DATABASE_PATH)

# =========================
# BOT SETUP
# =========================
async def get_prefix(bot, message):
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
        if ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild:
            return True
        raise commands.CheckFailure("You need Administrator or Manage Server permissions to use this command.")
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


def growth_message_for_stats(joins: int, leaves: int) -> str:
    net = joins - leaves
    if net > 0:
        return "📈 You’re growing — keep it up!"
    if net < 0:
        return "⚠️ Membership dipped a bit — time to re-engage your community."
    return "📊 Flat day today — tomorrow can be your push."


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
        discord.Color.green() if net > 0 else discord.Color.orange() if net < 0 else discord.Color.blurple(),
    )
    embed.add_field(name="Joins", value=f"+{joins}", inline=True)
    embed.add_field(name="Leaves", value=f"-{leaves}", inline=True)
    embed.add_field(name="Net Growth", value=f"{net:+d}", inline=True)
    embed.add_field(name="Message", value=growth_message_for_stats(joins, leaves), inline=False)

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

    # Reset alert memory if server comes back within threshold range
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
        embed.add_field(name="Today", value=f"+{stats['joins']} joins / -{stats['leaves']} leaves", inline=False)
        embed.add_field(name="Threshold", value=f"Alert threshold: **+{threshold}**", inline=False)

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
        embed.add_field(name="Today", value=f"+{stats['joins']} joins / -{stats['leaves']} leaves", inline=False)
        embed.add_field(name="Threshold", value=f"Alert threshold: **-{threshold}**", inline=False)

        try:
            await channel.send(embed=embed)
            db.set_last_alert_net(guild.id, -threshold)
        except discord.HTTPException:
            pass


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
        log.warning("on_member_remove handling failed in guild %s: %s", getattr(member.guild, "id", "unknown"), e)


# =========================
# ERROR HANDLER
# =========================
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


# =========================
# COMMANDS
# =========================
@bot.command(name="help")
async def help_command(ctx: commands.Context):
    embed = build_main_embed(
        f"{BOT_NAME} Help",
        "Here are the available commands.",
    )

    embed.add_field(
        name="General",
        value=(
            f"`{DEFAULT_PREFIX}help` - Show this help menu\n"
            f"`{DEFAULT_PREFIX}about` - About the bot\n"
            f"`{DEFAULT_PREFIX}invite` - Bot invite link\n"
            f"`{DEFAULT_PREFIX}stats` - Global bot stats\n"
            f"`{DEFAULT_PREFIX}serverstatus` - Current server info\n"
            f"`{DEFAULT_PREFIX}premium` - Check premium status"
        ),
        inline=False,
    )

    embed.add_field(
        name="Setup / Milestones",
        value=(
            f"`{DEFAULT_PREFIX}setup` - Show setup instructions\n"
            f"`{DEFAULT_PREFIX}setmilestone <member_count> @role` - Set milestone role\n"
            f"`{DEFAULT_PREFIX}removemilestone <member_count>` - Remove milestone role\n"
            f"`{DEFAULT_PREFIX}milestones` - List milestone roles"
        ),
        inline=False,
    )

    embed.add_field(
        name="Growth Tracking",
        value=(
            f"`{DEFAULT_PREFIX}setreport #channel` - Set daily report channel\n"
            f"`{DEFAULT_PREFIX}reportchannel` - Show report channel\n"
            f"`{DEFAULT_PREFIX}growthtoday` - Show today's growth stats\n"
            f"`{DEFAULT_PREFIX}growthweek` - 📈 Weekly growth analytics (Premium)\n"
            f"`{DEFAULT_PREFIX}bestday` - 🏆 Best growth day record\n"
            f"`{DEFAULT_PREFIX}setalertthreshold <number>` - 🚨 Set alert threshold (Premium)\n"
            f"`{DEFAULT_PREFIX}alerts on/off` - Toggle alerts (Premium)"
        ),
        inline=False,
    )

    if is_owner_user(ctx.author.id):
        embed.add_field(
            name="Owner",
            value=(
                f"`{DEFAULT_PREFIX}servers` - View install tracking and server list\n"
                f"`{DEFAULT_PREFIX}setpremium <guild_id>` - Enable premium\n"
                f"`{DEFAULT_PREFIX}removepremium <guild_id>` - Disable premium"
            ),
            inline=False,
        )

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

    embed = build_main_embed(
        f"{BOT_NAME} Setup",
        "Configure milestone roles, premium info, and growth reporting for this server.",
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
        f"{BOT_NAME} is a multi-server Discord bot with premium support, milestone role tools, install tracking, and growth notifications.",
    )
    embed.add_field(name="Prefix", value=f"`{DEFAULT_PREFIX}`", inline=True)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Support", value=f"[Join Support Server]({SUPPORT_SERVER_URL})", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="invite")
async def invite_command(ctx: commands.Context):
    embed = build_main_embed(
        f"Invite {BOT_NAME}",
        f"[Click here to invite {BOT_NAME}]({BOT_INVITE_URL})",
    )
    embed.add_field(name="Support", value=f"[Support Server]({SUPPORT_SERVER_URL})", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="stats")
async def stats_command(ctx: commands.Context):
    join_count = db.get_stat("join_count")
    remove_count = db.get_stat("remove_count")
    current_servers = len(bot.guilds)
    net_installs = join_count - remove_count
    total_members = total_member_estimate()

    embed = build_main_embed(
        f"{BOT_NAME} Stats",
        "Global bot statistics.",
    )
    embed.add_field(name="Current Servers", value=str(current_servers), inline=True)
    embed.add_field(name="Join Events", value=str(join_count), inline=True)
    embed.add_field(name="Remove Events", value=str(remove_count), inline=True)
    embed.add_field(name="Net Installs", value=str(net_installs), inline=True)
    embed.add_field(name="Users Reached", value=str(total_members), inline=True)
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

    embed = build_main_embed(
        f"Server Status - {ctx.guild.name}",
        "Current server information.",
    )
    embed.add_field(name="Server ID", value=str(ctx.guild.id), inline=True)
    embed.add_field(name="Members", value=str(ctx.guild.member_count or 0), inline=True)
    embed.add_field(name="Premium", value="Yes" if settings["premium"] else "No", inline=True)
    embed.add_field(name="Owner", value=str(ctx.guild.owner) if ctx.guild.owner else "Unknown", inline=True)
    embed.add_field(name="Report Channel", value=report_channel_display, inline=True)
    embed.add_field(name="Milestone Roles", value=str(len(milestone_roles)), inline=True)
    embed.add_field(name="Alerts Enabled", value="Yes" if settings["alerts_enabled"] else "No", inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(ctx.guild.created_at, style="F"), inline=False)
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
        name="Premium Features",
        value=(
            "• Weekly growth report command\n"
            "• Real-time growth/drop alerts\n"
            "• Custom growth alert threshold\n"
            "• Alert toggle controls"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="setmilestone")
@admin_or_manage_guild()
async def setmilestone_command(ctx: commands.Context, member_count: int, role: discord.Role):
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
        discord.Color.green() if stats["net"] > 0 else discord.Color.orange() if stats["net"] < 0 else discord.Color.blurple(),
    )
    embed.add_field(name="Joins", value=f"+{stats['joins']}", inline=True)
    embed.add_field(name="Leaves", value=f"-{stats['leaves']}", inline=True)
    embed.add_field(name="Net Growth", value=f"{stats['net']:+d}", inline=True)
    embed.add_field(name="Message", value=growth_message_for_stats(stats["joins"], stats["leaves"]), inline=False)
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


# =========================
# OWNER COMMANDS
# =========================
@bot.command(name="amowner")
async def amowner_command(ctx: commands.Context):
    await ctx.send(f"Your ID: {ctx.author.id}\nOWNER_IDS: {OWNER_IDS}\nOwner: {ctx.author.id in OWNER_IDS}")

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


@bot.command(name="servers")
@owner_only()
async def servers_command(ctx: commands.Context):
    join_count = db.get_stat("join_count")
    remove_count = db.get_stat("remove_count")
    current_servers = len(bot.guilds)

    guild_lines = []
    sorted_guilds = sorted(bot.guilds, key=lambda g: g.member_count or 0, reverse=True)

    for guild in sorted_guilds[:20]:
        settings = db.get_guild_settings(guild.id)
        premium_tag = " | Premium" if settings["premium"] else ""
        report_tag = " | Reports" if settings.get("report_channel_id") else ""
        alerts_tag = " | Alerts" if settings["premium"] and settings["alerts_enabled"] else ""
        line = f"`{guild.id}` • **{guild.name}** • {guild.member_count or 0} members{premium_tag}{report_tag}{alerts_tag}"
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