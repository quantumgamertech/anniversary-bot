import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks
import psycopg2

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

OWNER_ID = 207279875902537731

DEFAULT_MESSAGE = """{user} has completed {years} in the AO.

🕒 Time Served: {years}
🎮 Arena Breakout: Infinite

Loyalty recognized. Respect earned."""

EMBED_COLOR = 0x8A2BE2

ROLE_MILESTONES = {
    1: "OG Gooper",
    2: "Elite Gooper",
    3: "Legend Gooper",
}

ALL_MILESTONE_ROLE_NAMES = list(ROLE_MILESTONES.values())

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client = discord.Client(intents=intents)


def format_years(years: int) -> str:
    return f"{years} year" if years == 1 else f"{years} years"


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def setup_database():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id TEXT PRIMARY KEY
        )
    """)

    cur.execute("""
        ALTER TABLE guild_settings
        ADD COLUMN IF NOT EXISTS channel_id TEXT
    """)

    cur.execute("""
        ALTER TABLE guild_settings
        ADD COLUMN IF NOT EXISTS custom_message TEXT
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS anniversary_log (
            guild_id TEXT,
            user_id TEXT,
            anniversary_date TEXT,
            PRIMARY KEY (guild_id, user_id, anniversary_date)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS premium_guilds (
            guild_id TEXT PRIMARY KEY
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_today_key():
    now = datetime.now(timezone.utc)
    return f"{now.year}-{now.month:02d}-{now.day:02d}"


def get_guild_settings(guild_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT channel_id, custom_message
        FROM guild_settings
        WHERE guild_id = %s
    """, (str(guild_id),))
    result = cur.fetchone()

    cur.close()
    conn.close()

    if not result:
        return None

    return {
        "channel_id": int(result[0]) if result[0] else None,
        "custom_message": result[1]
    }


def set_channel_for_guild(guild_id, channel_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO guild_settings (guild_id, channel_id)
        VALUES (%s, %s)
        ON CONFLICT (guild_id)
        DO UPDATE SET channel_id = EXCLUDED.channel_id
    """, (str(guild_id), str(channel_id)))

    conn.commit()
    cur.close()
    conn.close()


def set_message_for_guild(guild_id, custom_message):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO guild_settings (guild_id, custom_message)
        VALUES (%s, %s)
        ON CONFLICT (guild_id)
        DO UPDATE SET custom_message = EXCLUDED.custom_message
    """, (str(guild_id), custom_message))

    conn.commit()
    cur.close()
    conn.close()


def reset_message_for_guild(guild_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        UPDATE guild_settings
        SET custom_message = NULL
        WHERE guild_id = %s
    """, (str(guild_id),))

    conn.commit()
    cur.close()
    conn.close()


def already_sent_today(guild_id, user_id, today_key):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT 1
        FROM anniversary_log
        WHERE guild_id = %s AND user_id = %s AND anniversary_date = %s
    """, (str(guild_id), str(user_id), today_key))

    result = cur.fetchone()

    cur.close()
    conn.close()

    return result is not None


def mark_sent_today(guild_id, user_id, today_key):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO anniversary_log (guild_id, user_id, anniversary_date)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (str(guild_id), str(user_id), today_key))

    conn.commit()
    cur.close()
    conn.close()


def is_premium(guild_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT 1
        FROM premium_guilds
        WHERE guild_id = %s
    """, (str(guild_id),))

    result = cur.fetchone()

    cur.close()
    conn.close()

    return result is not None


def add_premium(guild_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO premium_guilds (guild_id)
        VALUES (%s)
        ON CONFLICT DO NOTHING
    """, (str(guild_id),))

    conn.commit()
    cur.close()
    conn.close()


def remove_premium(guild_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM premium_guilds
        WHERE guild_id = %s
    """, (str(guild_id),))

    conn.commit()
    cur.close()
    conn.close()


def build_message(template, member, years_text):
    return template.format(
        user=member.mention,
        years=years_text
    )


def get_milestone_role_name(years):
    eligible_years = [milestone for milestone in ROLE_MILESTONES if years >= milestone]
    if not eligible_years:
        return None
    highest = max(eligible_years)
    return ROLE_MILESTONES[highest]


def get_next_milestone(years):
    future_milestones = sorted([milestone for milestone in ROLE_MILESTONES if milestone > years])
    if not future_milestones:
        return None
    next_year = future_milestones[0]
    return next_year, ROLE_MILESTONES[next_year]


def get_missing_milestone_roles(guild):
    missing = []
    for role_name in ALL_MILESTONE_ROLE_NAMES:
        if discord.utils.get(guild.roles, name=role_name) is None:
            missing.append(role_name)
    return missing


def get_manageability_issues(guild):
    issues = []

    me = guild.me
    if me is None:
        issues.append("Bot member object not available in this server.")
        return issues

    if not me.guild_permissions.manage_roles:
        issues.append("Bot is missing the **Manage Roles** permission.")

    bot_top_role = me.top_role
    for role_name in ALL_MILESTONE_ROLE_NAMES:
        role_obj = discord.utils.get(guild.roles, name=role_name)
        if role_obj and role_obj >= bot_top_role:
            issues.append(f"Move the bot role above **{role_name}**.")

    return issues


async def assign_highest_milestone_role(member, guild, years):
    target_role_name = get_milestone_role_name(years)
    if not target_role_name:
        return None

    target_role = discord.utils.get(guild.roles, name=target_role_name)
    if not target_role:
        return None

    milestone_roles_to_remove = []
    for role_name in ALL_MILESTONE_ROLE_NAMES:
        role_obj = discord.utils.get(guild.roles, name=role_name)
        if role_obj and role_obj in member.roles and role_obj.name != target_role_name:
            milestone_roles_to_remove.append(role_obj)

    try:
        if milestone_roles_to_remove:
            await member.remove_roles(
                *milestone_roles_to_remove,
                reason="Replacing old anniversary milestone role"
            )

        if target_role not in member.roles:
            await member.add_roles(target_role, reason="Anniversary milestone reward")

        return target_role.name
    except Exception as e:
        print(f"Role assignment failed for {member.name}: {e}")
        return None


def build_anniversary_embed(member, years, custom_message, role_name=None):
    years_text = format_years(years)
    description = build_message(
        custom_message if custom_message else DEFAULT_MESSAGE,
        member,
        years_text
    )

    if role_name:
        description += f"\n\n🎖️ Role Granted: **{role_name}**"

    embed = discord.Embed(
        title="🎉 Anniversary Celebration",
        description=description,
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Time Served", value=years_text, inline=True)

    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(text="QGT • Anniversary System")
    return embed


def build_test_embed(member, custom_message, role_name=None):
    description = build_message(
        custom_message if custom_message else DEFAULT_MESSAGE,
        member,
        "Preview"
    )

    if role_name:
        description += f"\n\n🎖️ Role Reward Preview: **{role_name}**"

    embed = discord.Embed(
        title="🎉 Anniversary Celebration",
        description=description,
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Time Served", value="Preview", inline=True)

    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(text="QGT • Preview Mode")
    return embed


def build_stats_embed(member, years, current_role_name, next_milestone):
    joined_text = "Unknown"
    if member.joined_at is not None:
        joined_text = member.joined_at.astimezone(timezone.utc).strftime("%B %d, %Y")

    embed = discord.Embed(
        title="📊 Your Anniversary Stats",
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Joined", value=joined_text, inline=True)
    embed.add_field(name="Years", value=str(years), inline=True)

    if current_role_name:
        embed.add_field(name="Current Milestone", value=current_role_name, inline=True)
    else:
        embed.add_field(name="Current Milestone", value="No milestone yet", inline=True)

    if next_milestone:
        next_year, next_role = next_milestone
        years_remaining = max(1, next_year - years)
        remaining_text = format_years(years_remaining)
        next_text = format_years(next_year)
        embed.add_field(
            name="Next Milestone",
            value=f"{next_text} → {next_role}\n{remaining_text} to go",
            inline=False
        )
    else:
        embed.add_field(
            name="Next Milestone",
            value="🏆 You’ve reached the highest configured milestone.",
            inline=False
        )

    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(text="QGT • Progress in the AO")
    return embed


def build_help_embed(is_server_premium):
    embed = discord.Embed(
        title="🛠️ Anniversary Bot Help",
        description="Celebrate server anniversaries with milestone roles, custom messages, and member progression.\n\u200b",
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="🆓 Free Features",
        value=(
            "\n`!setchannel` — Set the anniversary channel\n"
            "`!testanniversary` — Preview the anniversary embed\n"
            "`!mystats` — Show your milestone progress\n"
            "`!milestones` — View milestone role ladder\n"
            "`!serverstatus` — Check server setup status\n"
            "`!premiumstatus` — Check current plan\n"
            "`!help` — Show this help menu\n"
            "`!setup` — Show quick setup steps"
        ),
        inline=False
    )

    embed.add_field(
        name="💎 Premium Features",
        value=(
            "\n`!setmessage` — Set a custom anniversary message\n"
            "`!resetmessage` — Reset custom message\n"
            "`!testrole` — Preview milestone role rewards\n"
            "✨ Automatic milestone role rewards"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Setup Steps",
        value=(
            "\n1. Use `!setchannel` in the channel you want announcements in\n"
            "2. Make sure milestone roles exist in the server\n"
            "3. Move the bot role above milestone roles\n"
            "4. Upgrade to premium for custom messages and role rewards"
        ),
        inline=False
    )

    embed.add_field(
        name="🏆 Milestone Roles",
        value=(
            f"\n1 year → **{ROLE_MILESTONES[1]}**\n"
            f"2 years → **{ROLE_MILESTONES[2]}**\n"
            f"3 years → **{ROLE_MILESTONES[3]}**"
        ),
        inline=False
    )

    plan_text = "💎 PREMIUM" if is_server_premium else "🆓 FREE"
    embed.add_field(name="📊 Current Server Plan", value=f"\n{plan_text}", inline=False)

    embed.set_footer(text="QGT • Anniversary System")
    return embed


def build_setup_embed(guild, is_server_premium):
    missing_roles = get_missing_milestone_roles(guild)
    manageability_issues = get_manageability_issues(guild)

    embed = discord.Embed(
        title="⚙️ Anniversary Bot Setup",
        description="Quick setup guide to get the bot fully running in your server.\n\u200b",
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="Step 1",
        value="Run `!setchannel` in the channel where you want anniversary posts.",
        inline=False
    )

    embed.add_field(
        name="Step 2",
        value=(
            "Create these milestone roles:\n"
            f"• {ROLE_MILESTONES[1]}\n"
            f"• {ROLE_MILESTONES[2]}\n"
            f"• {ROLE_MILESTONES[3]}"
        ),
        inline=False
    )

    embed.add_field(
        name="Step 3",
        value="Move the **Anniversary Bot** role above those milestone roles in Server Settings → Roles.",
        inline=False
    )

    embed.add_field(
        name="Step 4",
        value=(
            "Optional premium commands:\n"
            "`!setmessage` — set a custom message\n"
            "`!resetmessage` — reset custom message\n"
            "`!testrole` — preview role rewards"
        ),
        inline=False
    )

    if missing_roles:
        embed.add_field(
            name="⚠️ Missing Roles Detected",
            value="\n".join([f"• {role_name}" for role_name in missing_roles]),
            inline=False
        )

    if manageability_issues:
        embed.add_field(
            name="⚠️ Bot Permission / Role Issues",
            value="\n".join([f"• {issue}" for issue in manageability_issues]),
            inline=False
        )

    if not missing_roles and not manageability_issues:
        embed.add_field(
            name="✅ Setup Check",
            value="Everything looks good. Your bot is fully configured and ready to go.",
            inline=False
        )

    plan_text = "💎 PREMIUM" if is_server_premium else "🆓 FREE"
    embed.add_field(name="Current Plan", value=plan_text, inline=False)

    embed.set_footer(text="QGT • Use !help for commands")
    return embed


def build_milestones_embed():
    embed = discord.Embed(
        title="🏆 Anniversary Milestones",
        description="Here’s the full milestone ladder for server progression.\n\u200b",
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    milestone_lines = []
    for years, role_name in sorted(ROLE_MILESTONES.items()):
        year_text = format_years(years)
        milestone_lines.append(f"**{year_text}** → {role_name}")

    embed.add_field(
        name="Milestone Ladder",
        value="\n".join(milestone_lines),
        inline=False
    )

    embed.set_footer(text="QGT • Milestones")
    return embed


def build_serverstatus_embed(guild, is_server_premium_flag, settings):
    missing_roles = get_missing_milestone_roles(guild)
    manageability_issues = get_manageability_issues(guild)

    channel_value = "Not configured"
    if settings and settings.get("channel_id"):
        channel = guild.get_channel(settings["channel_id"])
        if channel:
            channel_value = channel.mention
        else:
            channel_value = "Configured channel not found"

    custom_message_value = "Default"
    if is_server_premium_flag and settings and settings.get("custom_message"):
        custom_message_value = "Custom message enabled"
    elif not is_server_premium_flag:
        custom_message_value = "Locked (premium only)"

    embed = discord.Embed(
        title="📋 Server Status",
        description="Current anniversary system status for this server.\n\u200b",
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Plan", value="💎 PREMIUM" if is_server_premium_flag else "🆓 FREE", inline=True)
    embed.add_field(name="Announcement Channel", value=channel_value, inline=True)
    embed.add_field(name="Message Mode", value=custom_message_value, inline=True)

    if missing_roles:
        embed.add_field(
            name="Missing Roles",
            value="\n".join([f"• {role_name}" for role_name in missing_roles]),
            inline=False
        )
    else:
        embed.add_field(
            name="Missing Roles",
            value="✅ All milestone roles configured",
            inline=False
        )

    if manageability_issues:
        embed.add_field(
            name="Bot Role / Permission Issues",
            value="\n".join([f"• {issue}" for issue in manageability_issues]),
            inline=False
        )
    else:
        embed.add_field(
            name="Bot Role / Permission Issues",
            value="✅ Permissions & role hierarchy good",
            inline=False
        )

    embed.set_footer(text="QGT • Server Diagnostics")
    return embed


@client.event
async def on_ready():
    setup_database()
    print(f"Logged in as {client.user}")
    if not check_anniversaries.is_running():
        check_anniversaries.start()


@tasks.loop(hours=24)
async def check_anniversaries():
    now = datetime.now(timezone.utc)
    today_key = get_today_key()

    for guild in client.guilds:
        settings = get_guild_settings(guild.id)

        if not settings or not settings.get("channel_id"):
            print(f"No configured channel for {guild.name}")
            continue

        channel = client.get_channel(settings["channel_id"])
        if channel is None:
            print(f"Configured channel not found for {guild.name}")
            continue

        custom_message = DEFAULT_MESSAGE
        if is_premium(guild.id) and settings.get("custom_message"):
            custom_message = settings["custom_message"]

        for member in guild.members:
            if member.bot or member.joined_at is None:
                continue

            joined = member.joined_at.astimezone(timezone.utc)

            if joined.month == now.month and joined.day == now.day:
                years = now.year - joined.year

                if years >= 1:
                    if already_sent_today(guild.id, member.id, today_key):
                        continue

                    granted_role_name = None
                    if is_premium(guild.id):
                        granted_role_name = await assign_highest_milestone_role(member, guild, years)

                    embed = build_anniversary_embed(
                        member,
                        years,
                        custom_message,
                        granted_role_name
                    )
                    await channel.send(embed=embed)
                    mark_sent_today(guild.id, member.id, today_key)


@check_anniversaries.before_loop
async def before_check_anniversaries():
    await client.wait_until_ready()


@client.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    if message.content == "!test":
        await message.channel.send("ANV Bot is working 🔥")

    elif message.content == "!help":
        embed = build_help_embed(is_premium(message.guild.id))
        await message.channel.send(embed=embed)

    elif message.content == "!setup":
        embed = build_setup_embed(message.guild, is_premium(message.guild.id))
        await message.channel.send(embed=embed)

    elif message.content == "!milestones":
        embed = build_milestones_embed()
        await message.channel.send(embed=embed)

    elif message.content == "!serverstatus":
        settings = get_guild_settings(message.guild.id)
        embed = build_serverstatus_embed(
            message.guild,
            is_premium(message.guild.id),
            settings
        )
        await message.channel.send(embed=embed)

    elif message.content == "!testanniversary":
        settings = get_guild_settings(message.guild.id)
        custom_message = DEFAULT_MESSAGE
        role_name = None

        if is_premium(message.guild.id) and settings and settings.get("custom_message"):
            custom_message = settings["custom_message"]

        if is_premium(message.guild.id):
            role_name = get_milestone_role_name(3)

        embed = build_test_embed(message.author, custom_message, role_name)
        await message.channel.send(embed=embed)

    elif message.content == "!mystats":
        if message.author.joined_at is None:
            await message.channel.send("❌ Could not read your join date.")
            return

        now = datetime.now(timezone.utc)
        joined = message.author.joined_at.astimezone(timezone.utc)
        years = now.year - joined.year

        if (now.month, now.day) < (joined.month, joined.day):
            years -= 1

        if years < 0:
            years = 0

        current_role_name = get_milestone_role_name(years)
        next_milestone = get_next_milestone(years)

        embed = build_stats_embed(
            message.author,
            years,
            current_role_name,
            next_milestone
        )
        await message.channel.send(embed=embed)

    elif message.content == "!setchannel":
        if not message.author.guild_permissions.administrator:
            await message.channel.send("❌ You must be an admin.")
            return

        try:
            set_channel_for_guild(message.guild.id, message.channel.id)
            await message.channel.send(f"✅ Channel set to {message.channel.mention}")
        except Exception as e:
            await message.channel.send(f"❌ Failed to save channel: {e}")

    elif message.content.startswith("!setmessage "):
        if not message.author.guild_permissions.administrator:
            await message.channel.send("❌ You must be an admin.")
            return

        if not is_premium(message.guild.id):
            await message.channel.send(
                """💎 **Premium Feature**

Unlock:
• Custom anniversary messages
• Multi-role rewards
• Advanced features

🚀 Upgrade your server today!
Contact the server owner to get access."""
            )
            return

        custom_message = message.content.replace("!setmessage ", "", 1).strip()

        if not custom_message:
            await message.channel.send("❌ Usage: !setmessage Your custom message here")
            return

        try:
            set_message_for_guild(message.guild.id, custom_message)
            await message.channel.send("✅ Custom anniversary message saved.")
        except Exception as e:
            await message.channel.send(f"❌ Save failed: {e}")

    elif message.content == "!resetmessage":
        if not message.author.guild_permissions.administrator:
            await message.channel.send("❌ You must be an admin.")
            return

        if not is_premium(message.guild.id):
            await message.channel.send(
                """💎 **Premium Feature**

Unlock:
• Custom anniversary messages
• Multi-role rewards
• Advanced features

🚀 Upgrade your server today!
Contact the server owner to get access."""
            )
            return

        try:
            reset_message_for_guild(message.guild.id)
            await message.channel.send("✅ Anniversary message reset to default.")
        except Exception as e:
            await message.channel.send(f"❌ Reset failed: {e}")

    elif message.content == "!testrole":
        if not is_premium(message.guild.id):
            await message.channel.send(
                """💎 **Premium Feature**

Unlock:
• Custom anniversary messages
• Multi-role rewards
• Advanced features

🚀 Upgrade your server today!
Contact the server owner to get access."""
            )
            return

        missing_roles = get_missing_milestone_roles(message.guild)
        manageability_issues = get_manageability_issues(message.guild)

        if missing_roles:
            await message.channel.send(
                "❌ Cannot preview milestone roles yet.\nMissing roles:\n" +
                "\n".join([f"• {role_name}" for role_name in missing_roles])
            )
            return

        if manageability_issues:
            await message.channel.send(
                "❌ Cannot preview milestone roles yet.\nFix these issues:\n" +
                "\n".join([f"• {issue}" for issue in manageability_issues])
            )
            return

        granted_role_name = await assign_highest_milestone_role(message.author, message.guild, 3)

        if granted_role_name:
            await message.channel.send(f"✅ Highest milestone role granted: **{granted_role_name}**")
        else:
            await message.channel.send("❌ Could not assign a milestone role. Double-check role order and permissions.")

    elif message.content == "!messagehelp":
        await message.channel.send(
            """📝 Custom message help

Use:
!setmessage your message here

Available placeholders:
{user}   = mentions the user
{years}  = anniversary years

Example:
!setmessage 🎉 {user} just hit {years}! Welcome to the elite.

💎 Custom messages and milestone role rewards are premium features.

📊 Extra commands:
!mystats
!milestones
!serverstatus
!help
!setup
"""
        )

    elif message.content == "!premiumstatus":
        if is_premium(message.guild.id):
            await message.channel.send("💎 This server is on PREMIUM.")
        else:
            await message.channel.send("🆓 This server is on the FREE plan.")

    elif message.content == "!premium":
        if message.author.id != OWNER_ID:
            return

        if is_premium(message.guild.id):
            await message.channel.send("💎 This server is already premium.")
            return

        add_premium(message.guild.id)
        await message.channel.send("💎 Premium activated for this server.")

    elif message.content == "!unpremium":
        if message.author.id != OWNER_ID:
            return

        if not is_premium(message.guild.id):
            await message.channel.send("🆓 This server is already on the free plan.")
            return

        remove_premium(message.guild.id)
        await message.channel.send("🆓 Premium removed from this server.")


client.run(TOKEN)