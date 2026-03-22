import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks
import psycopg2

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

DEFAULT_MESSAGE = """Operator {user} has completed {years} year(s) in the AO.

🕒 Time Served: {years} year(s)
🎮 Arena Breakout: Infinite

Loyalty recognized. Respect earned."""

EMBED_COLOR = 0x8A2BE2  # purple-ish

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client = discord.Client(intents=intents)


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
        ALTER TABLE guild_settings
        ADD COLUMN IF NOT EXISTS role_name TEXT
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS anniversary_log (
            guild_id TEXT,
            user_id TEXT,
            anniversary_date TEXT,
            PRIMARY KEY (guild_id, user_id, anniversary_date)
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
        SELECT channel_id, custom_message, role_name
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
        "custom_message": result[1],
        "role_name": result[2]
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


def build_message(template, member, years, guild_name):
    return template.format(
        user=member.mention,
        years=years,
        server=guild_name
    )


def build_anniversary_embed(member, years, guild_name, custom_message):
    description = build_message(custom_message or DEFAULT_MESSAGE, member, years, guild_name)

    embed = discord.Embed(
        title="🎖 SYSTEM EVENT — SERVICE MILESTONE",
        description=description,
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_author(
        name="Anniversary Bot",
        icon_url=client.user.display_avatar.url if client.user else discord.Embed.Empty
    )

    embed.add_field(name="Operator", value=member.mention, inline=True)
    embed.add_field(name="Time Served", value=f"{years} year(s)", inline=True)
    embed.add_field(name="Server", value=guild_name, inline=True)

    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(text="Loyalty recognized. Respect earned.")
    return embed


def build_test_embed(member, guild_name, custom_message):
    description = build_message(custom_message or DEFAULT_MESSAGE, member, "TEST MODE", guild_name)

    embed = discord.Embed(
        title="🎖 SYSTEM EVENT — SERVICE MILESTONE",
        description=description,
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_author(
        name="Anniversary Bot",
        icon_url=client.user.display_avatar.url if client.user else discord.Embed.Empty
    )

    embed.add_field(name="Operator", value=member.mention, inline=True)
    embed.add_field(name="Time Served", value="TEST MODE", inline=True)
    embed.add_field(name="Server", value=guild_name, inline=True)

    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(text="Preview mode.")
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

        custom_message = settings.get("custom_message") or DEFAULT_MESSAGE

        for member in guild.members:
            if member.bot or member.joined_at is None:
                continue

            joined = member.joined_at.astimezone(timezone.utc)

            if joined.month == now.month and joined.day == now.day:
                years = now.year - joined.year

                if years >= 1:
                    if already_sent_today(guild.id, member.id, today_key):
                        continue

                    embed = build_anniversary_embed(member, years, guild.name, custom_message)
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

    elif message.content == "!testanniversary":
        settings = get_guild_settings(message.guild.id)
        custom_message = DEFAULT_MESSAGE

        if settings and settings.get("custom_message"):
            custom_message = settings["custom_message"]

        embed = build_test_embed(message.author, message.guild.name, custom_message)
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

        try:
            reset_message_for_guild(message.guild.id)
            await message.channel.send("✅ Anniversary message reset to default.")
        except Exception as e:
            await message.channel.send(f"❌ Reset failed: {e}")

    elif message.content == "!messagehelp":
        await message.channel.send(
            """📝 Custom message help

Use:
!setmessage your message here

Available placeholders:
{user}   = mentions the user
{years}  = anniversary years
{server} = server name

Example:
!setmessage 🎉 {user} just hit {years} year(s) in {server}!
"""
        )


client.run(TOKEN)