import os
import io
import json
import hmac
import hashlib
import logging
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
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
    "https://discord.gg/7htnU8d2bm",
)
BOT_INVITE_URL = os.getenv(
    "BOT_INVITE_URL",
    "https://discord.com/oauth2/authorize?client_id=1483943578148405279&permissions=8&integration_type=0&scope=bot+applications.commands",
)

TOPGG_WEBHOOK_AUTH = os.getenv("TOPGG_WEBHOOK_AUTH", "")
TOPGG_WEBHOOK_SECRET = os.getenv("TOPGG_WEBHOOK_SECRET", "")
TOPGG_WEBHOOK_ROUTE = os.getenv("TOPGG_WEBHOOK_ROUTE", "/topgg")
TOPGG_WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
TOPGG_WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
TOPGG_VOTE_PREMIUM_HOURS = int(os.getenv("TOPGG_VOTE_PREMIUM_HOURS", "12"))
TOPGG_VOTE_URL = os.getenv("TOPGG_VOTE_URL", "")
AUTO_PREMIUM_GUILD_IDS = {
    int(part.strip())
    for part in os.getenv("AUTO_PREMIUM_GUILD_IDS", "").split(",")
    if part.strip().isdigit()
}

LEMONSQUEEZY_CHECKOUT_URL = os.getenv("LEMONSQUEEZY_CHECKOUT_URL", "https://legacybot.lemonsqueezy.com/checkout/buy/97bb71c6-d255-4acc-85b8-a8447ff77020")
LEMONSQUEEZY_WEBHOOK_SECRET = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET", "")
LEMONSQUEEZY_WEBHOOK_ROUTE = os.getenv("LEMONSQUEEZY_WEBHOOK_ROUTE", "/lemonsqueezy/webhook")

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
from legacy_database import create_database_from_env


db = create_database_from_env(
    database_url=os.getenv("DATABASE_URL", ""),
    sqlite_path=DATABASE_PATH,
)


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


def apply_auto_premium_for_known_guilds():
    if not AUTO_PREMIUM_GUILD_IDS:
        return

    for guild_id in AUTO_PREMIUM_GUILD_IDS:
        try:
            db.set_premium(guild_id, True)
        except Exception as e:
            log.warning("Failed auto-premium for guild %s: %s", guild_id, e)


def build_lemonsqueezy_checkout_url(guild: discord.Guild, user: discord.abc.User) -> str:
    base = LEMONSQUEEZY_CHECKOUT_URL.strip()
    if not base:
        return ""

    split = urlsplit(base)
    query_items = dict(parse_qsl(split.query, keep_blank_values=True))
    query_items.update({
        "checkout[custom][guild_id]": str(guild.id),
        "checkout[custom][guild_name]": guild.name,
        "checkout[custom][user_id]": str(user.id),
        "checkout[custom][user_name]": str(user),
        "checkout[custom][source]": "legacy_bot",
    })
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query_items), split.fragment))


def verify_lemonsqueezy_signature(raw_body: bytes, signature: str) -> bool:
    if not LEMONSQUEEZY_WEBHOOK_SECRET:
        return False
    digest = hmac.new(
        LEMONSQUEEZY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, signature or "")


def verify_topgg_signature(raw_body: bytes, signature_header: str) -> bool:
    if not TOPGG_WEBHOOK_SECRET or not signature_header:
        return False

    signature_parts = {}
    for raw_part in signature_header.split(","):
        key, separator, value = raw_part.strip().partition("=")
        if separator:
            signature_parts[key] = value

    timestamp = signature_parts.get("t")
    received_signature = signature_parts.get("v1")
    if not timestamp or not received_signature:
        return False

    signed_payload = timestamp.encode("utf-8") + b"." + raw_body
    expected_signature = hmac.new(
        TOPGG_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, received_signature)


def billing_status_entitles_premium(status: Optional[str]) -> bool:
    return (status or "").lower() in {
        "active",
        "on_trial",
        "paused",
    }


def billing_has_valid_future_ends_at(billing: Optional[dict]) -> bool:
    if not billing:
        return False
    ends_at = iso_to_dt(billing.get("ends_at"))
    return ends_at is not None and ends_at > datetime.now(UTC)


def billing_record_entitles_premium(billing: Optional[dict]) -> bool:
    if not billing:
        return False

    status = (billing.get("status") or "").lower()
    if status in {"cancelled", "past_due", "unpaid"}:
        return billing_has_valid_future_ends_at(billing)
    return billing_status_entitles_premium(status)


def reconcile_guild_premium(guild_id: int):
    if guild_id in AUTO_PREMIUM_GUILD_IDS:
        db.set_premium(guild_id, True)
        return

    billing = db.get_guild_billing(guild_id)
    if billing_record_entitles_premium(billing):
        db.set_premium(guild_id, True)


def build_billing_status_embed(guild: discord.Guild) -> discord.Embed:
    settings = db.get_guild_settings(guild.id)
    billing = db.get_guild_billing(guild.id)
    embed = build_main_embed(
        "💳 Premium Billing",
        f"Billing status for **{guild.name}**",
        discord.Color.gold() if settings["premium"] else discord.Color.blurple(),
    )
    embed.add_field(name="Premium", value="Enabled" if settings["premium"] else "Disabled", inline=True)
    embed.add_field(name="Checkout", value="Configured" if LEMONSQUEEZY_CHECKOUT_URL else "Missing", inline=True)
    embed.add_field(name="Auto Premium", value="Yes" if guild.id in AUTO_PREMIUM_GUILD_IDS else "No", inline=True)

    if billing:
        embed.add_field(name="Subscription Status", value=billing.get("status_formatted") or billing.get("status") or "Unknown", inline=True)
        embed.add_field(name="Renews At", value=format_dt_safe(billing.get("renews_at"), "F") if billing.get("renews_at") else "Unknown", inline=True)
        embed.add_field(name="Ends At", value=format_dt_safe(billing.get("ends_at"), "F") if billing.get("ends_at") else "Not scheduled", inline=True)
        if billing.get("customer_portal_url"):
            embed.add_field(name="Customer Portal", value=f"[Manage Subscription]({billing['customer_portal_url']})", inline=False)
    else:
        embed.add_field(name="Subscription Status", value="No billing record linked yet.", inline=False)

    if LEMONSQUEEZY_CHECKOUT_URL:
        embed.add_field(name="Buy Premium", value=f"Use `/buypremium` or `{DEFAULT_PREFIX}buypremium` to generate a checkout link for this server.", inline=False)
    else:
        embed.add_field(name="Buy Premium", value="Set `LEMONSQUEEZY_CHECKOUT_URL` in your environment first.", inline=False)

    return embed


def should_enable_premium_from_billing_event(event_name: str, status: Optional[str]) -> bool:
    status = (status or "").lower()
    if event_name in {
        "subscription_created",
        "subscription_resumed",
        "subscription_unpaused",
        "subscription_payment_success",
        "subscription_payment_recovered",
    }:
        return True
    if event_name in {"subscription_cancelled", "subscription_paused"}:
        return True
    if event_name == "subscription_updated" and billing_status_entitles_premium(status):
        return True
    return False


def should_disable_premium_from_billing_event(event_name: str, status: Optional[str]) -> bool:
    status = (status or "").lower()
    if event_name == "subscription_expired":
        return True
    if event_name == "subscription_updated" and status == "expired":
        return True
    return False


def billing_record_requires_premium_removal(billing: Optional[dict]) -> bool:
    if not billing:
        return False
    status = (billing.get("status") or "").lower()
    return status in {"cancelled", "past_due", "unpaid", "expired"} and not billing_record_entitles_premium(billing)


async def process_lemonsqueezy_webhook(payload: dict) -> dict:
    meta = payload.get("meta") or {}
    event_name = str(meta.get("event_name") or "").strip()
    custom_data = meta.get("custom_data") or {}
    data = payload.get("data") or {}
    attributes = data.get("attributes") or {}

    raw_guild_id = custom_data.get("guild_id")
    try:
        guild_id = int(raw_guild_id) if raw_guild_id is not None else None
    except Exception:
        guild_id = None

    raw_user_id = custom_data.get("user_id")
    try:
        discord_user_id = int(raw_user_id) if raw_user_id is not None else None
    except Exception:
        discord_user_id = None

    raw_subscription_id = attributes.get("subscription_id")
    if raw_subscription_id is None and data.get("type") == "subscriptions":
        raw_subscription_id = data.get("id")
    subscription_id = str(raw_subscription_id) if raw_subscription_id is not None else None
    customer_id = str(attributes.get("customer_id")) if attributes.get("customer_id") is not None else None
    order_id = str(attributes.get("order_id")) if attributes.get("order_id") is not None else None
    status = attributes.get("status")
    status_formatted = attributes.get("status_formatted")
    renews_at = attributes.get("renews_at")
    ends_at = attributes.get("ends_at")
    urls = attributes.get("urls") or {}
    customer_portal_url = urls.get("customer_portal")
    update_payment_url = urls.get("update_payment_method")
    product_name = attributes.get("product_name") or attributes.get("product_id")
    variant_name = attributes.get("variant_name") or attributes.get("variant_id")
    test_mode = bool(attributes.get("test_mode"))

    if guild_id is None and subscription_id:
        existing_billing = db.get_guild_billing_by_subscription_id(subscription_id)
        if existing_billing:
            guild_id = int(existing_billing["guild_id"])
            if discord_user_id is None:
                discord_user_id = existing_billing.get("discord_user_id")

    if guild_id is not None:
        existing_billing = db.get_guild_billing(guild_id)
        if data.get("type") != "subscriptions" and existing_billing:
            discord_user_id = discord_user_id or existing_billing.get("discord_user_id")
            customer_id = customer_id or existing_billing.get("lemonsqueezy_customer_id")
            order_id = order_id or existing_billing.get("order_id")
            product_name = product_name or existing_billing.get("product_name")
            variant_name = variant_name or existing_billing.get("variant_name")
            status = existing_billing.get("status")
            status_formatted = existing_billing.get("status_formatted")
            renews_at = existing_billing.get("renews_at")
            ends_at = existing_billing.get("ends_at")
            customer_portal_url = existing_billing.get("customer_portal_url")
            update_payment_url = existing_billing.get("update_payment_url")

        db.ensure_guild(guild_id)
        db.upsert_guild_billing(
            guild_id=guild_id,
            discord_user_id=discord_user_id,
            subscription_id=subscription_id,
            customer_id=customer_id,
            order_id=order_id,
            product_name=str(product_name) if product_name is not None else None,
            variant_name=str(variant_name) if variant_name is not None else None,
            status=status,
            status_formatted=status_formatted,
            renews_at=renews_at,
            ends_at=ends_at,
            customer_portal_url=customer_portal_url,
            update_payment_url=update_payment_url,
            last_event_name=event_name,
            checkout_url=LEMONSQUEEZY_CHECKOUT_URL,
            test_mode=test_mode,
        )

        updated_billing = db.get_guild_billing(guild_id)
        if billing_record_entitles_premium(updated_billing):
            db.set_premium(guild_id, True)
        elif (
            (
                should_disable_premium_from_billing_event(event_name, status)
                or billing_record_requires_premium_removal(updated_billing)
            )
            and guild_id not in AUTO_PREMIUM_GUILD_IDS
        ):
            db.set_premium(guild_id, False)
    else:
        log.warning(
            "Lemon Squeezy event %s could not be linked to a guild; subscription_id=%s",
            event_name or "unknown",
            subscription_id or "unknown",
        )

    db.record_billing_event(event_name, guild_id, subscription_id, payload)

    return {
        "event_name": event_name,
        "guild_id": guild_id,
        "subscription_id": subscription_id,
        "status": status,
        "test_mode": test_mode,
    }


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


async def require_guild_context(ctx: commands.Context) -> bool:
    if ctx.guild is not None:
        return True
    await ctx.send(
        embed=build_main_embed(
            "Server Only",
            "Use this command in a server.",
            discord.Color.red(),
        )
    )
    return False


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




def get_growth_timeseries(guild_id: int, days: int = 7):
    days = max(3, min(int(days), 30))
    end_date = datetime.now(UTC).date()
    start_date = end_date - timedelta(days=days - 1)

    rows = []
    running_total = 0
    current = start_date
    while current <= end_date:
        stats = db.get_growth_for_date(guild_id, current.isoformat())
        running_total += stats["net"]
        rows.append(
            {
                "date": current.isoformat(),
                "label": current.strftime("%m/%d"),
                "joins": int(stats["joins"]),
                "leaves": int(stats["leaves"]),
                "net": int(stats["net"]),
                "cumulative_net": int(running_total),
            }
        )
        current += timedelta(days=1)

    return rows


def summarize_growth_timeseries(rows):
    joins = sum(row["joins"] for row in rows)
    leaves = sum(row["leaves"] for row in rows)
    net = joins - leaves
    positive_days = sum(1 for row in rows if row["net"] > 0)
    negative_days = sum(1 for row in rows if row["net"] < 0)
    flat_days = len(rows) - positive_days - negative_days
    avg_daily_net = (net / len(rows)) if rows else 0.0
    best_day = max(rows, key=lambda row: (row["net"], row["joins"], row["date"])) if rows else None
    worst_day = min(rows, key=lambda row: (row["net"], -row["joins"], row["date"])) if rows else None
    first_half = rows[: max(1, len(rows) // 2)]
    second_half = rows[len(rows) // 2 :] if rows else []
    first_half_net = sum(row["net"] for row in first_half)
    second_half_net = sum(row["net"] for row in second_half)

    return {
        "joins": joins,
        "leaves": leaves,
        "net": net,
        "positive_days": positive_days,
        "negative_days": negative_days,
        "flat_days": flat_days,
        "avg_daily_net": avg_daily_net,
        "best_day": best_day,
        "worst_day": worst_day,
        "first_half_net": first_half_net,
        "second_half_net": second_half_net,
    }


def count_growth_data_days(rows) -> int:
    return sum(1 for row in rows if int(row["joins"]) > 0 or int(row["leaves"]) > 0)


def build_not_enough_growth_data_embed(
    title: str,
    guild: discord.Guild,
    observed_days: int,
    required_days: int = 3,
) -> discord.Embed:
    embed = build_main_embed(
        title,
        f"Not enough growth data yet for **{guild.name}**.",
        discord.Color.blurple(),
    )
    embed.add_field(
        name="Data Needed",
        value=(
            f"Need at least **{required_days}** days with joins or leaves. "
            f"Current data days: **{observed_days}**."
        ),
        inline=False,
    )
    embed.add_field(
        name="What Counts",
        value="Member joins and leaves recorded while Legacy Bot is in the server.",
        inline=False,
    )
    return embed


def clamp_score(value: int) -> int:
    return max(0, min(100, int(value)))


def health_label(score: int) -> str:
    if score >= 80:
        return "Strong"
    if score >= 60:
        return "Stable"
    if score >= 40:
        return "Needs Attention"
    return "At Risk"


def build_server_health_embed(guild: discord.Guild) -> discord.Embed:
    rows = get_growth_timeseries(guild.id, days=14)
    observed_days = count_growth_data_days(rows)
    if observed_days < 3:
        return build_not_enough_growth_data_embed("Server Health Score", guild, observed_days)

    summary = summarize_growth_timeseries(rows)
    join_points = min(20, summary["joins"] * 2)
    net_points = max(-25, min(25, summary["net"] * 3))
    consistency_points = (summary["positive_days"] * 4) - (summary["negative_days"] * 3)
    churn_penalty = min(20, summary["leaves"] * 2)
    score = clamp_score(55 + join_points + net_points + consistency_points - churn_penalty)

    embed = build_main_embed(
        "Server Health Score",
        f"Calculated from the last **14 days** of available join and leave data for **{guild.name}**.",
        discord.Color.green() if score >= 60 else discord.Color.orange(),
    )
    embed.add_field(name="Score", value=f"**{score}/100**", inline=True)
    embed.add_field(name="Status", value=health_label(score), inline=True)
    embed.add_field(name="Data Days", value=str(observed_days), inline=True)
    embed.add_field(name="Joins", value=f"+{summary['joins']}", inline=True)
    embed.add_field(name="Leaves", value=f"-{summary['leaves']}", inline=True)
    embed.add_field(name="Net Growth", value=f"{summary['net']:+d}", inline=True)
    return embed


def build_growth_advisor_embed(guild: discord.Guild) -> discord.Embed:
    rows = get_growth_timeseries(guild.id, days=7)
    observed_days = count_growth_data_days(rows)
    if observed_days < 3:
        return build_not_enough_growth_data_embed("Growth Advisor", guild, observed_days)

    summary = summarize_growth_timeseries(rows)
    suggestions = []
    if summary["net"] > 0:
        suggestions.append("Keep posting the content or events that drove recent joins.")
    elif summary["net"] < 0:
        suggestions.append("Run a re-engagement post and ask active members what they want next.")
    else:
        suggestions.append("Create one clear weekly reason for members to return and participate.")

    if summary["leaves"] > summary["joins"]:
        suggestions.append("Review recent announcements, rules, or inactive channels that may be causing churn.")
    if summary["positive_days"] <= 1:
        suggestions.append("Schedule a simple invite push or community event on your highest-traffic day.")
    if summary["joins"] == 0:
        suggestions.append("Refresh your invite message and place it where new members can see the server value fast.")
    if len(suggestions) < 3:
        suggestions.append("Use today's growth and the leaderboard to spot which days create the best momentum.")

    embed = build_main_embed(
        "Growth Advisor",
        f"Rule-based advice from the last **7 days** for **{guild.name}**.",
        discord.Color.blurple(),
    )
    embed.add_field(
        name="Recent Growth",
        value=f"+{summary['joins']} joins / -{summary['leaves']} leaves / **{summary['net']:+d} net**",
        inline=False,
    )
    embed.add_field(
        name="Suggestions",
        value="\n".join(f"- {item}" for item in suggestions[:4]),
        inline=False,
    )
    return embed


def build_growth_prediction_embed(guild: discord.Guild) -> discord.Embed:
    rows = get_growth_timeseries(guild.id, days=14)
    observed_days = count_growth_data_days(rows)
    if observed_days < 3:
        return build_not_enough_growth_data_embed("Growth Prediction", guild, observed_days)

    active_rows = [row for row in rows if int(row["joins"]) > 0 or int(row["leaves"]) > 0]
    avg_daily_net = sum(row["net"] for row in active_rows) / len(active_rows)
    projected_7_day_net = round(avg_daily_net * 7)
    projected_members = max(0, (guild.member_count or 0) + projected_7_day_net)

    embed = build_main_embed(
        "Growth Prediction",
        f"Simple projection from recent average net growth for **{guild.name}**.",
        discord.Color.gold(),
    )
    embed.add_field(name="Observed Data Days", value=str(observed_days), inline=True)
    embed.add_field(name="Avg Net / Data Day", value=f"{avg_daily_net:+.2f}", inline=True)
    embed.add_field(name="Projected 7-Day Net", value=f"{projected_7_day_net:+d}", inline=True)
    embed.add_field(name="Current Members", value=str(guild.member_count or 0), inline=True)
    embed.add_field(name="Projected Members", value=str(projected_members), inline=True)
    embed.add_field(
        name="Model",
        value="Uses recent joins and leaves only. No paid API calls or external AI dependency.",
        inline=False,
    )
    return embed


def describe_growth_trend(summary: dict) -> str:
    delta = summary["second_half_net"] - summary["first_half_net"]
    avg = summary["avg_daily_net"]

    if summary["net"] == 0 and delta == 0:
        return "➖ Stable"
    if avg > 0 and delta > 0:
        return "🚀 Accelerating"
    if avg > 0:
        return "📈 Upward"
    if avg < 0 and delta < 0:
        return "📉 Slipping"
    if avg < 0:
        return "↘️ Recovering"
    return "➖ Stable"


def format_percent_change(current_value: int, previous_value: int) -> str:
    if previous_value == 0:
        if current_value == 0:
            return "0%"
        return "New activity"

    pct = ((current_value - previous_value) / abs(previous_value)) * 100
    return f"{pct:+.0f}%"


def build_dashboard_color(summary: dict) -> discord.Color:
    if summary["net"] > 0:
        return discord.Color.green()
    if summary["net"] < 0:
        return discord.Color.orange()
    return discord.Color.gold()


def generate_growth_dashboard_chart(guild: discord.Guild, days: int = 7) -> io.BytesIO:
    rows = get_growth_timeseries(guild.id, days=days)
    labels = [row["label"] for row in rows]
    daily_net = [row["net"] for row in rows]
    cumulative = [row["cumulative_net"] for row in rows]
    joins = [row["joins"] for row in rows]
    leaves = [row["leaves"] for row in rows]
    x_positions = list(range(len(labels)))

    fig, ax = plt.subplots(figsize=(11.2, 5.6), facecolor="#0f111a")
    try:
        ax.set_facecolor("#151826")

        bar_colors = ["#43b581" if value >= 0 else "#f04747" for value in daily_net]
        ax.bar(x_positions, daily_net, color=bar_colors, alpha=0.55, width=0.62, label="Daily Net")
        ax.plot(x_positions, cumulative, color="#ffd166", linewidth=2.8, marker="o", markersize=5, label="Cumulative Net")
        ax.fill_between(x_positions, cumulative, 0, color="#ffd166", alpha=0.08)

        if any(joins) or any(leaves):
            ax.plot(x_positions, joins, color="#4ea8de", linewidth=1.6, linestyle="--", alpha=0.9, label="Joins")
            ax.plot(x_positions, leaves, color="#ff7b72", linewidth=1.6, linestyle=":", alpha=0.9, label="Leaves")

        ax.axhline(0, color="#9aa4b2", linewidth=1, alpha=0.45)
        ax.set_title(f"{guild.name} • Elite Growth Dashboard", color="white", fontsize=15, pad=14)
        ax.set_ylabel("Members", color="#d0d7de")
        ax.set_xticks(x_positions)
        ax.set_xticklabels(labels, rotation=35, ha="right", color="#c9d1d9")
        ax.tick_params(axis="y", colors="#c9d1d9")

        for spine in ax.spines.values():
            spine.set_color("#30363d")

        ax.grid(True, axis="y", linestyle="--", linewidth=0.6, alpha=0.22, color="#8b949e")
        legend = ax.legend(facecolor="#151826", edgecolor="#30363d", labelcolor="#e6edf3")
        for text_obj in legend.get_texts():
            text_obj.set_color("#e6edf3")

        if not any(joins) and not any(leaves):
            ax.text(
                0.5,
                0.5,
                "Not enough growth activity yet",
                transform=ax.transAxes,
                ha="center",
                va="center",
                fontsize=16,
                color="#e6edf3",
                bbox={"boxstyle": "round,pad=0.5", "facecolor": "#21262d", "edgecolor": "#30363d", "alpha": 0.95},
            )

        final_cumulative = cumulative[-1] if cumulative else 0
        final_daily = daily_net[-1] if daily_net else 0
        badge_text = f"Window Net {final_cumulative:+d} • Latest Day {final_daily:+d}"
        ax.text(
            0.99,
            1.04,
            badge_text,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=10,
            color="#e6edf3",
            bbox={"boxstyle": "round,pad=0.35", "facecolor": "#21262d", "edgecolor": "#30363d", "alpha": 0.95},
        )

        plt.tight_layout()

        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=170, bbox_inches="tight", facecolor=fig.get_facecolor())
        buffer.seek(0)
        return buffer
    finally:
        plt.close(fig)


def build_growth_dashboard_embed(guild: discord.Guild, days: int = 7) -> discord.Embed:
    days = max(3, min(int(days), 30))
    rows = get_growth_timeseries(guild.id, days=days)
    summary = summarize_growth_timeseries(rows)
    today_stats = db.get_growth_for_date(guild.id, current_utc_day_str())
    week_summary = summarize_growth_timeseries(get_growth_timeseries(guild.id, days=7))
    prev_week_summary = summarize_growth_timeseries(get_growth_timeseries(guild.id, days=14)[:7])
    top_days = db.get_top_growth_days(guild.id, limit=3)

    trend_text = describe_growth_trend(summary)
    week_delta_text = format_percent_change(week_summary['net'], prev_week_summary['net'])

    best_day = summary.get('best_day')
    if best_day and int(best_day.get('net', 0)) > 0:
        best_day_text = (
            f"**{best_day['date']}** • Net **{best_day['net']:+d}**\n"
            f"+{best_day['joins']} joins • -{best_day['leaves']} leaves"
        )
    else:
        best_day_text = 'No positive growth day yet.'

    worst_day = summary.get('worst_day')
    if worst_day and int(worst_day.get('net', 0)) < 0:
        worst_day_text = (
            f"**{worst_day['date']}** • Net **{worst_day['net']:+d}**\n"
            f"+{worst_day['joins']} joins • -{worst_day['leaves']} leaves"
        )
    else:
        worst_day_text = 'No negative growth day yet.'

    recent_lines = [
        f"`{row['label']}` **{row['net']:+d}**  (+{row['joins']} / -{row['leaves']})"
        for row in rows[-7:]
    ]

    champion_lines = [
        f"{medal_for_rank(idx)} **{row['date']}** • **{int(row['net']):+d}** net"
        for idx, row in enumerate(top_days, start=1)
    ]

    observed_days = count_growth_data_days(rows)
    description = (
        f"Premium analytics for **{guild.name}** across the last **{days}** days.\n"
        f"Trend: **{trend_text}** • Weekly momentum: **{week_delta_text}** • Data days: **{observed_days}**"
    )
    embed = build_main_embed(
        'Premium Growth Dashboard',
        description,
        build_dashboard_color(summary),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name='Members', value=str(guild.member_count or 0), inline=True)
    embed.add_field(name='Window Net', value=f"{summary['net']:+d}", inline=True)
    embed.add_field(name='Avg / Day', value=f"{summary['avg_daily_net']:+.2f}", inline=True)

    embed.add_field(
        name='Today',
        value=f"+{today_stats['joins']} / -{today_stats['leaves']} • **{today_stats['net']:+d}**"
        ,inline=True,
    )
    embed.add_field(name='7-Day Net', value=f"{week_summary['net']:+d}", inline=True)
    embed.add_field(name='Trend', value=trend_text, inline=True)

    embed.add_field(
        name=f"{days}-Day Pulse" ,
        value=(
            f"**Joins:** +{summary['joins']}\n"
            f"**Leaves:** -{summary['leaves']}\n"
            f"**Positive Days:** {summary['positive_days']}\n"
            f"**Negative Days:** {summary['negative_days']}\n"
            f"**Flat Days:** {summary['flat_days']}"
        ),
        inline=True,
    )
    embed.add_field(name='Best Day', value=best_day_text, inline=True)
    embed.add_field(name='Toughest Day', value=worst_day_text, inline=True)

    embed.add_field(
        name='Top Growth Days',
        value='\n'.join(champion_lines) if champion_lines else 'No growth data yet.',
        inline=False,
    )
    embed.add_field(
        name='Last 7 Days Snapshot',
        value='\n'.join(recent_lines) if recent_lines else 'No recent growth data yet.',
        inline=False,
    )
    embed.set_image(url='attachment://growth_dashboard.png')
    embed.set_footer(text=f"Elite analytics • Requested window: {days} days")
    return embed
SPANISH_KEYWORDS = {
    "ayuda",
    "comandos",
    "configuracion",
    "configurar",
    "empezar",
    "es",
    "espanol",
    "español",
    "inicio",
    "premium",
    "servidor",
}


def wants_spanish(*values: Optional[str]) -> bool:
    for value in values:
        if not value:
            continue
        normalized = str(value).strip().lower()
        if normalized in SPANISH_KEYWORDS:
            return True
        if any(keyword in normalized.split() for keyword in SPANISH_KEYWORDS):
            return True
    return False


def build_help_embed(include_owner: bool = False, language: Optional[str] = None) -> discord.Embed:
    spanish = wants_spanish(language)
    embed = build_main_embed(
        f"{BOT_NAME} Help" if not spanish else f"Ayuda de {BOT_NAME}",
        (
            "Here are the available commands. Start with `/start` or `/setup` if this is a new server."
            if not spanish else
            "Aqui estan los comandos disponibles. Usa `/start` o `/setup` si este es un servidor nuevo."
        ),
    )

    embed.add_field(
        name="Start Here" if not spanish else "Primeros Pasos",
        value=(
            f"`{DEFAULT_PREFIX}ping` - Check bot latency\n"
            f"`{DEFAULT_PREFIX}help` - Show this help menu\n"
            f"`{DEFAULT_PREFIX}help es` - Ver ayuda en espanol\n"
            f"`{DEFAULT_PREFIX}setup` - New server setup guide\n"
            f"`{DEFAULT_PREFIX}about` - About the bot\n"
            f"`{DEFAULT_PREFIX}invite` - Bot invite link\n"
            f"`{DEFAULT_PREFIX}stats` - Global bot stats\n"
            f"`{DEFAULT_PREFIX}serverstatus` - Current server info"
        ),
        inline=False,
    )

    embed.add_field(
        name="Free Commands" if not spanish else "Comandos Gratis",
        value=(
            f"`{DEFAULT_PREFIX}growthtoday` - Today's joins, leaves, and net growth\n"
            f"`{DEFAULT_PREFIX}analytics` - Free 7-day growth snapshot\n"
            f"`{DEFAULT_PREFIX}bestday` - Best growth day record\n"
            f"`{DEFAULT_PREFIX}growthleaderboard` - Top server growth days\n"
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
        name="Premium Growth Tools" if not spanish else "Herramientas Premium",
        value=(
            f"`{DEFAULT_PREFIX}setreport #channel` - Set daily report channel\n"
            f"`{DEFAULT_PREFIX}reportchannel` - Show report channel\n"
            f"`{DEFAULT_PREFIX}growthweek` - Weekly growth analytics (Premium)\n"
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
            "`/start` - New server quick-start guide\n"
            "`/setup` - Setup guide for server admins\n"
            "`/analytics` - Free 7-day growth snapshot\n"
            "`/growthtoday` - Free growth stats for today\n"
            "`/growthleaderboard` - Show top growth days\n"
            "`/healthscore` - Server health score from growth data\n"
            "`/advisor` - Rule-based growth suggestions\n"
            "`/growthpredict` - Simple recent-average growth projection\n"
            "`/dashboard` - Premium analytics dashboard\n"
            "`/premium` - Free vs premium overview\n"
            "`/vote` - Get Top.gg vote link\n"
            "`/votestatus` - Check your vote rewards\n"
            "`/buypremium` - Get a premium checkout link\n"
            "`/premiumstatus` - View premium billing status"
        ),
        inline=False,
    )

    embed.add_field(
        name="Free vs Premium" if not spanish else "Gratis vs Premium",
        value=(
            "Free servers can track daily growth, view leaderboards, configure milestones, and use Top.gg vote rewards.\n"
            "Premium adds the dashboard, weekly growth analytics, live alerts, custom alert thresholds, and daily report automation."
            if not spanish else
            "Los servidores gratis pueden ver crecimiento diario, rankings, hitos y recompensas de Top.gg.\n"
            "Premium agrega dashboard, analiticas semanales, alertas, limites personalizados y reportes diarios."
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


def build_setup_embed(guild: discord.Guild, language: Optional[str] = None) -> discord.Embed:
    spanish = wants_spanish(language)
    db.ensure_guild(guild.id)
    settings = db.get_guild_settings(guild.id)

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
        f"{BOT_NAME} Setup" if not spanish else f"Configuracion de {BOT_NAME}",
        (
            "Quick setup guide for server owners and admins. Free servers can start tracking growth immediately."
            if not spanish else
            "Guia rapida para duenos y administradores. Los servidores gratis pueden empezar a medir crecimiento de inmediato."
        ),
    )

    embed.add_field(
        name="1. Confirm the bot works" if not spanish else "1. Confirma que el bot funciona",
        value=(
            f"Run `{DEFAULT_PREFIX}ping` or `/ping`.\n"
            f"Run `{DEFAULT_PREFIX}growthtoday` or `/growthtoday` to see today's free growth stats."
            if not spanish else
            f"Usa `{DEFAULT_PREFIX}ping` o `/ping`.\n"
            f"Usa `{DEFAULT_PREFIX}growthtoday` o `/growthtoday` para ver el crecimiento gratis de hoy."
        ),
        inline=False,
    )

    embed.add_field(
        name="2. Set milestone roles" if not spanish else "2. Configura roles por metas",
        value=(
            f"`{DEFAULT_PREFIX}setmilestone <member_count> @role` assigns a role when the server reaches a member count.\n"
            f"`{DEFAULT_PREFIX}milestones` shows saved milestone roles."
            if not spanish else
            f"`{DEFAULT_PREFIX}setmilestone <cantidad> @rol` asigna un rol cuando el servidor llega a una meta.\n"
            f"`{DEFAULT_PREFIX}milestones` muestra los roles guardados."
        ),
        inline=False,
    )

    embed.add_field(
        name="3. Configure reports and vote rewards" if not spanish else "3. Configura reportes y recompensas",
        value=(
            f"`{DEFAULT_PREFIX}setreport #channel` chooses where growth reports go. Current: {report_channel_text}\n"
            f"`{DEFAULT_PREFIX}setvoterole @role` gives active Top.gg voters a temporary role. Current: {vote_role_text}"
            if not spanish else
            f"`{DEFAULT_PREFIX}setreport #canal` elige donde enviar reportes. Actual: {report_channel_text}\n"
            f"`{DEFAULT_PREFIX}setvoterole @rol` da un rol temporal a votantes activos de Top.gg. Actual: {vote_role_text}"
        ),
        inline=False,
    )

    embed.add_field(
        name="4. Understand free vs premium" if not spanish else "4. Entiende gratis vs premium",
        value=(
            f"Free: `{DEFAULT_PREFIX}growthtoday`, `{DEFAULT_PREFIX}analytics`, `{DEFAULT_PREFIX}growthleaderboard`, milestones, vote rewards.\n"
            f"Premium: `{DEFAULT_PREFIX}dashboard`, `{DEFAULT_PREFIX}growthweek`, alerts, custom thresholds, daily report automation.\n"
            f"Use `{DEFAULT_PREFIX}premium` or `/premium` for details."
            if not spanish else
            f"Gratis: `{DEFAULT_PREFIX}growthtoday`, `{DEFAULT_PREFIX}analytics`, `{DEFAULT_PREFIX}growthleaderboard`, metas y recompensas por votos.\n"
            f"Premium: `{DEFAULT_PREFIX}dashboard`, `{DEFAULT_PREFIX}growthweek`, alertas, limites personalizados y reportes diarios.\n"
            f"Usa `{DEFAULT_PREFIX}premium` o `/premium` para mas detalles."
        ),
        inline=False,
    )

    embed.add_field(
        name="Need help?" if not spanish else "Necesitas ayuda?",
        value=(
            f"Use `{DEFAULT_PREFIX}help`, `/help`, or join support: {SUPPORT_SERVER_URL}"
            if not spanish else
            f"Usa `{DEFAULT_PREFIX}help es`, `/help language: es`, o entra al soporte: {SUPPORT_SERVER_URL}"
        ),
        inline=False,
    )

    return embed


def build_growth_today_embed(guild: discord.Guild) -> discord.Embed:
    stats = db.get_growth_for_date(guild.id, current_utc_day_str())
    embed = build_main_embed(
        "Today's Growth",
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
    embed.add_field(
        name="Free Command",
        value="This growth snapshot is available on free servers. Use `/dashboard` for premium analytics.",
        inline=False,
    )
    return embed


def build_free_analytics_embed(guild: discord.Guild) -> discord.Embed:
    end_date_obj = datetime.now(UTC).date()
    start_date_obj = end_date_obj - timedelta(days=6)
    stats = db.get_growth_range(
        guild.id,
        start_date_obj.isoformat(),
        end_date_obj.isoformat(),
    )
    today = db.get_growth_for_date(guild.id, current_utc_day_str())
    top_days = db.get_top_growth_days(guild.id, limit=3)

    embed = build_main_embed(
        "Free Growth Analytics",
        f"7-day snapshot for **{guild.name}**. Premium dashboard remains available with `/dashboard`.",
        discord.Color.green()
        if stats["net"] > 0
        else discord.Color.orange()
        if stats["net"] < 0
        else discord.Color.blurple(),
    )
    embed.add_field(name="7-Day Joins", value=f"+{stats['joins']}", inline=True)
    embed.add_field(name="7-Day Leaves", value=f"-{stats['leaves']}", inline=True)
    embed.add_field(name="7-Day Net", value=f"{stats['net']:+d}", inline=True)
    embed.add_field(name="Today", value=f"{today['net']:+d} net (+{today['joins']} / -{today['leaves']})", inline=False)

    if top_days:
        lines = [
            f"**{row['date']}** - Net {int(row['net']):+d} (+{int(row['joins'])} / -{int(row['leaves'])})"
            for row in top_days
        ]
        embed.add_field(name="Top Growth Days", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Top Growth Days", value="No growth data recorded yet.", inline=False)

    embed.add_field(
        name="Premium Upgrade",
        value="Premium adds charts, weekly analytics, live alerts, custom thresholds, and report automation.",
        inline=False,
    )
    return embed


def build_premium_overview_embed(guild: discord.Guild, user) -> discord.Embed:
    settings = db.get_guild_settings(guild.id)
    billing = db.get_guild_billing(guild.id)

    embed = build_main_embed(
        "Premium Overview",
        f"Server premium is currently **{'Enabled' if settings['premium'] else 'Disabled'}** for **{guild.name}**.",
        discord.Color.gold() if settings["premium"] else discord.Color.blurple(),
    )

    embed.add_field(
        name="Free Plan",
        value=(
            f"`{DEFAULT_PREFIX}growthtoday` / `/growthtoday` - today's joins, leaves, and net growth\n"
            f"`{DEFAULT_PREFIX}analytics` / `/analytics` - free 7-day growth snapshot\n"
            f"`{DEFAULT_PREFIX}growthleaderboard` - top growth days\n"
            "Milestone roles and Top.gg vote rewards"
        ),
        inline=False,
    )

    embed.add_field(
        name="Premium Plan",
        value=(
            f"`{DEFAULT_PREFIX}dashboard [days]` / `/dashboard` - charted analytics dashboard\n"
            f"`{DEFAULT_PREFIX}growthweek` - weekly growth analytics\n"
            f"`{DEFAULT_PREFIX}alerts on/off` - live growth/drop alerts\n"
            f"`{DEFAULT_PREFIX}setalertthreshold <number>` - custom alert threshold\n"
            "Daily growth report automation"
        ),
        inline=False,
    )

    embed.add_field(
        name="Your Vote Premium",
        value=(
            f"Active - {get_vote_premium_remaining_text(user.id)}"
            if is_vote_premium_active(user.id)
            else "Inactive - vote on Top.gg to unlock temporary personal perks and reward role access"
        ),
        inline=False,
    )

    embed.add_field(
        name="Billing",
        value=(
            (billing.get("status_formatted") or billing.get("status") or "Linked")
            if billing else
            f"Not linked yet - use `{DEFAULT_PREFIX}buypremium` or `/buypremium` to purchase."
        ),
        inline=False,
    )

    embed.add_field(
        name="How to upgrade",
        value="Server admins can open secure Lemon Squeezy checkout with `/buypremium`.",
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


def find_welcome_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    candidates = []
    if isinstance(guild.system_channel, discord.TextChannel):
        candidates.append(guild.system_channel)
    candidates.extend(
        channel
        for channel in guild.text_channels
        if channel not in candidates
    )

    me = guild.me
    if me is None:
        return None

    for channel in candidates:
        perms = channel.permissions_for(me)
        if perms.send_messages and perms.embed_links:
            return channel
    return None


async def send_join_welcome(guild: discord.Guild):
    embed = build_main_embed(
        f"Thanks for adding {BOT_NAME}",
        "I am ready to track growth, milestones, Top.gg vote rewards, and premium analytics for this server.",
        discord.Color.green(),
    )
    embed.add_field(
        name="Start here",
        value=(
            "Run `/start` or `/setup` for the server-owner setup guide.\n"
            "Español: usa `/start language: es` o `/setup language: es` para la guia en español."
        ),
        inline=False,
    )
    embed.add_field(
        name="Free value",
        value="Use `/growthtoday`, `/analytics`, `/growthleaderboard`, `/vote`, and `/votestatus` without premium.",
        inline=False,
    )
    embed.add_field(
        name="Premium",
        value="Use `/premium` to compare free vs premium, or `/buypremium` if you are ready to upgrade.",
        inline=False,
    )

    channel = find_welcome_channel(guild)
    if channel is not None:
        try:
            await channel.send(embed=embed)
            return
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("Could not send join welcome in guild %s channel %s: %s", guild.id, channel.id, e)

    if guild.owner is not None:
        try:
            await guild.owner.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("Could not DM join welcome to guild owner for guild %s: %s", guild.id, e)


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
    raw_body = await request.read()
    signature_header = request.headers.get("x-topgg-signature", "")
    is_v1_webhook = bool(signature_header)

    if is_v1_webhook:
        if not TOPGG_WEBHOOK_SECRET:
            log.error("Top.gg v1 webhook received but TOPGG_WEBHOOK_SECRET is missing.")
            return web.json_response({"ok": False, "error": "Webhook secret not configured"}, status=503)
        if not verify_topgg_signature(raw_body, signature_header):
            log.warning(
                "Rejected Top.gg webhook with invalid signature; trace=%s",
                request.headers.get("x-topgg-trace", "unknown"),
            )
            return web.json_response({"ok": False, "error": "Invalid signature"}, status=401)
    else:
        if not TOPGG_WEBHOOK_AUTH:
            log.error("Legacy Top.gg webhook received but TOPGG_WEBHOOK_AUTH is missing.")
            return web.json_response({"ok": False, "error": "Webhook authorization not configured"}, status=503)
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, TOPGG_WEBHOOK_AUTH):
            return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    if is_v1_webhook:
        event_type = str(payload.get("type") or "").lower()
        if event_type not in {"vote.create", "webhook.test"}:
            return web.json_response({"ok": False, "error": "Unsupported event type"}, status=400)

        event_data = payload.get("data") or {}
        user_data = event_data.get("user") or {}
        raw_user = user_data.get("platform_id")
        payload["username"] = user_data.get("name")
        payload["isWeekend"] = int(event_data.get("weight") or 1) > 1
        vote_type = "test" if event_type == "webhook.test" else "upvote"
    else:
        raw_user = payload.get("user") or payload.get("id")
        vote_type = str(payload.get("type", "upvote")).lower()

    if raw_user is None:
        return web.json_response({"ok": False, "error": "Missing user"}, status=400)

    try:
        user_id = int(raw_user)
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid user"}, status=400)

    if vote_type not in {"upvote", "test"}:
        return web.json_response({"ok": False, "error": "Unsupported vote type"}, status=400)

    try:
        result = await process_topgg_vote(user_id, payload, source=f"topgg_{vote_type}")
        return web.json_response({"ok": True, "result": result})
    except Exception as e:
        log.exception("Top.gg vote processing failed: %s", e)
        return web.json_response({"ok": False, "error": "Internal error"}, status=500)


async def lemonsqueezy_webhook_handler(request: web.Request):
    raw_body = await request.read()
    signature = request.headers.get("X-Signature", "")

    if not LEMONSQUEEZY_WEBHOOK_SECRET:
        log.error("Lemon Squeezy webhook received but LEMONSQUEEZY_WEBHOOK_SECRET is missing.")
        return web.json_response({"ok": False, "error": "Webhook secret not configured"}, status=503)

    if not verify_lemonsqueezy_signature(raw_body, signature):
        log.warning("Rejected Lemon Squeezy webhook with an invalid signature.")
        return web.json_response({"ok": False, "error": "Invalid signature"}, status=401)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    try:
        result = await process_lemonsqueezy_webhook(payload)
        log.info(
            "Processed Lemon Squeezy event=%s guild_id=%s subscription_id=%s status=%s test_mode=%s",
            result["event_name"] or "unknown",
            result["guild_id"] or "unlinked",
            result["subscription_id"] or "unknown",
            result["status"] or "unknown",
            result["test_mode"],
        )
        return web.json_response({"ok": True, "result": result})
    except Exception as e:
        log.exception("Lemon Squeezy webhook processing failed: %s", e)
        return web.json_response({"ok": False, "error": "Internal error"}, status=500)


async def start_web_server():
    global web_app, web_runner, web_site

    if web_runner is not None:
        return
    if TOPGG_WEBHOOK_ROUTE == LEMONSQUEEZY_WEBHOOK_ROUTE:
        raise RuntimeError(
            "TOPGG_WEBHOOK_ROUTE and LEMONSQUEEZY_WEBHOOK_ROUTE must be different."
        )

    app = web.Application()
    app.router.add_get("/", healthcheck_handler)
    app.router.add_get("/health", healthcheck_handler)
    app.router.add_post(TOPGG_WEBHOOK_ROUTE, topgg_vote_handler)

    if TOPGG_WEBHOOK_ROUTE != "/topgg":
        app.router.add_post("/topgg", topgg_vote_handler)
    app.router.add_post(LEMONSQUEEZY_WEBHOOK_ROUTE, lemonsqueezy_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, TOPGG_WEB_HOST, TOPGG_WEB_PORT)
    await site.start()

    web_app = app
    web_runner = runner
    web_site = site
    log.info(
        "Webhook server started on %s:%s topgg=%s lemonsqueezy=%s",
        TOPGG_WEB_HOST,
        TOPGG_WEB_PORT,
        TOPGG_WEBHOOK_ROUTE,
        LEMONSQUEEZY_WEBHOOK_ROUTE,
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
            if not settings.get("premium"):
                continue
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
        reconcile_guild_premium(guild.id)

    apply_auto_premium_for_known_guilds()

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
    reconcile_guild_premium(guild.id)

    db.increment_stat("join_count", 1)
    db.record_install_event(
        guild_id=guild.id,
        guild_name=guild.name,
        event_type="join",
        member_count=guild.member_count or 0,
    )
    await send_join_welcome(guild)
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
async def help_command(ctx: commands.Context, language: Optional[str] = None):
    embed = build_help_embed(
        include_owner=is_owner_user(ctx.author.id),
        language=language,
    )
    await ctx.send(embed=embed)


@bot.command(name="setup")
@admin_or_manage_guild()
async def setup_command(ctx: commands.Context, language: Optional[str] = None):
    await ctx.send(embed=build_setup_embed(ctx.guild, language=language))


@bot.command(name="start")
async def start_command(ctx: commands.Context, language: Optional[str] = None):
    if ctx.guild is None:
        return await ctx.send("This command can only be used in a server.")
    await ctx.send(embed=build_setup_embed(ctx.guild, language=language))


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
    if not await require_guild_context(ctx):
        return

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
    if not await require_guild_context(ctx):
        return

    await ctx.send(embed=build_premium_overview_embed(ctx.guild, ctx.author))


@bot.command(name="buypremium")
@admin_or_manage_guild()
async def buypremium_command(ctx: commands.Context):
    checkout_url = build_lemonsqueezy_checkout_url(ctx.guild, ctx.author)
    if not checkout_url:
        return await ctx.send(
            embed=build_main_embed(
                "Checkout Not Configured",
                "Set `LEMONSQUEEZY_CHECKOUT_URL` in your environment first.",
                discord.Color.red(),
            )
        )

    embed = build_main_embed(
        "💳 Buy Premium",
        f"Use the secure checkout link below to purchase premium for **{ctx.guild.name}**.",
        discord.Color.gold(),
    )
    embed.add_field(name="Checkout Link", value=f"[Open Checkout]({checkout_url})", inline=False)
    embed.add_field(name="Server", value=f"{ctx.guild.name} (`{ctx.guild.id}`)", inline=False)
    embed.add_field(name="What happens next", value="After payment, Lemon Squeezy will call your webhook and premium will unlock automatically.", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="premiumstatus")
async def premiumstatus_command(ctx: commands.Context):
    if ctx.guild is None:
        return await ctx.send("This command can only be used in a server.")
    await ctx.send(embed=build_billing_status_embed(ctx.guild))


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
    if not await require_guild_context(ctx):
        return

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
    if not await require_guild_context(ctx):
        return

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
    if not await require_guild_context(ctx):
        return

    await ctx.send(embed=build_growth_today_embed(ctx.guild))


@bot.command(name="analytics")
async def analytics_command(ctx: commands.Context):
    if ctx.guild is None:
        return await ctx.send(
            embed=build_main_embed(
                "Server Only",
                "This command can only be used in a server.",
                discord.Color.red(),
            )
        )
    await ctx.send(embed=build_free_analytics_embed(ctx.guild))


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
    if not await require_guild_context(ctx):
        return

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
    if not await require_guild_context(ctx):
        return

    embed = build_growth_leaderboard_embed(ctx.guild)
    await ctx.send(embed=embed)




@bot.command(name="dashboard")
async def dashboard_command(ctx: commands.Context, days: Optional[int] = 7):
    if ctx.guild is None:
        return await ctx.send(
            embed=build_main_embed(
                "Server Only",
                "This command can only be used in a server.",
                discord.Color.red(),
            )
        )

    settings = db.get_guild_settings(ctx.guild.id)
    if not settings["premium"]:
        return await ctx.send(
            embed=build_main_embed(
                "Premium Required",
                "This dashboard is available only for premium servers.",
                discord.Color.red(),
            )
        )

    days = max(3, min(int(days or 7), 30))
    try:
        chart_buffer = generate_growth_dashboard_chart(ctx.guild, days=days)
    except Exception:
        log.exception(
            "Dashboard chart generation failed for guild %s over %s days.",
            ctx.guild.id,
            days,
        )
        return await ctx.send(
            embed=build_main_embed(
                "Dashboard Unavailable",
                "The dashboard chart could not be generated right now. Please try again shortly.",
                discord.Color.red(),
            )
        )
    dashboard_file = discord.File(chart_buffer, filename="growth_dashboard.png")
    embed = build_growth_dashboard_embed(ctx.guild, days=days)
    await ctx.send(embed=embed, file=dashboard_file)

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
@premium_required()
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
@app_commands.describe(language="Optional: use 'es' for a Spanish-friendly help menu")
async def help_slash(interaction: discord.Interaction, language: Optional[str] = None):
    embed = build_help_embed(include_owner=False, language=language)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="start", description="Quick-start guide for new server owners")
@app_commands.describe(language="Optional: use 'es' for Spanish onboarding")
async def start_slash(interaction: discord.Interaction, language: Optional[str] = None):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    embed = build_setup_embed(interaction.guild, language=language)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="setup", description="Setup guide for server admins")
@app_commands.describe(language="Optional: use 'es' for Spanish onboarding")
async def setup_slash(interaction: discord.Interaction, language: Optional[str] = None):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    perms = interaction.user.guild_permissions if isinstance(interaction.user, discord.Member) else None
    if perms is None or not (perms.administrator or perms.manage_guild):
        return await interaction.response.send_message(
            "You need Administrator or Manage Server permissions to use this command.",
            ephemeral=True,
        )

    embed = build_setup_embed(interaction.guild, language=language)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="analytics", description="Free 7-day growth snapshot for this server")
async def analytics_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        embed=build_free_analytics_embed(interaction.guild),
        ephemeral=True,
    )


@bot.tree.command(name="growthtoday", description="Free growth stats for today")
async def growthtoday_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        embed=build_growth_today_embed(interaction.guild),
        ephemeral=True,
    )


@bot.tree.command(name="growthleaderboard", description="Show this server's top growth days")
async def growthleaderboard_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    embed = build_growth_leaderboard_embed(interaction.guild)
    await interaction.response.send_message(embed=embed)




@bot.tree.command(name="healthscore", description="Calculate a safe server health score from growth data")
async def healthscore_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        embed=build_server_health_embed(interaction.guild),
        ephemeral=True,
    )


@bot.tree.command(name="advisor", description="Get rule-based growth advice from recent server growth")
async def advisor_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        embed=build_growth_advisor_embed(interaction.guild),
        ephemeral=True,
    )


@bot.tree.command(name="growthpredict", description="Project near-term growth from recent averages")
async def growthpredict_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        embed=build_growth_prediction_embed(interaction.guild),
        ephemeral=True,
    )


@bot.tree.command(name="dashboard", description="Premium analytics dashboard for this server")
@app_commands.describe(days="How many days to analyze (3-30)")
async def dashboard_slash(
    interaction: discord.Interaction,
    days: app_commands.Range[int, 3, 30] = 7,
):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    settings = db.get_guild_settings(interaction.guild.id)
    if not settings["premium"]:
        return await interaction.response.send_message(
            "🚫 This dashboard is available only for premium servers.",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)
    try:
        chart_buffer = generate_growth_dashboard_chart(interaction.guild, days=int(days))
    except Exception:
        log.exception(
            "Slash dashboard chart generation failed for guild %s over %s days.",
            interaction.guild.id,
            int(days),
        )
        return await interaction.followup.send(
            "The dashboard chart could not be generated right now. Please try again shortly.",
            ephemeral=True,
        )
    dashboard_file = discord.File(chart_buffer, filename="growth_dashboard.png")
    embed = build_growth_dashboard_embed(interaction.guild, days=int(days))
    await interaction.followup.send(embed=embed, file=dashboard_file, ephemeral=True)


@bot.tree.command(name="premium", description="Compare free vs premium and check server status")
async def premium_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        embed=build_premium_overview_embed(interaction.guild, interaction.user),
        ephemeral=True,
    )


@bot.tree.command(name="buypremium", description="Get a Lemon Squeezy checkout link for this server")
async def buypremium_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    perms = interaction.user.guild_permissions if isinstance(interaction.user, discord.Member) else None
    if perms is None or not (perms.administrator or perms.manage_guild):
        return await interaction.response.send_message(
            "You need Administrator or Manage Server permissions to use this command.",
            ephemeral=True,
        )

    checkout_url = build_lemonsqueezy_checkout_url(interaction.guild, interaction.user)
    if not checkout_url:
        return await interaction.response.send_message(
            "Checkout is not configured yet. Set `LEMONSQUEEZY_CHECKOUT_URL` in the bot environment.",
            ephemeral=True,
        )

    embed = build_main_embed(
        "💳 Buy Premium",
        f"Use the secure checkout link below to purchase premium for **{interaction.guild.name}**.",
        discord.Color.gold(),
    )
    embed.add_field(name="Checkout Link", value=f"[Open Checkout]({checkout_url})", inline=False)
    embed.add_field(name="Server", value=f"{interaction.guild.name} (`{interaction.guild.id}`)", inline=False)
    embed.add_field(name="After payment", value="Premium unlocks automatically after the webhook is received.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="premiumstatus", description="View premium billing status for this server")
async def premiumstatus_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.send_message(embed=build_billing_status_embed(interaction.guild), ephemeral=True)


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
@owner_only()
async def amowner_command(ctx: commands.Context):
    await ctx.send("Owner access confirmed.")


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
