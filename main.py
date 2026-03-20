import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks
import psycopg2

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

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
            guild_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS anniversary_log (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            anniversary_date TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, anniversary_date)
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_today_key():
    now = datetime.now(timezone.utc)
    return f"{now.year}-{now.month:02d}-{now.day:02d}"


def get_channel_id_for_guild(guild_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT channel_id FROM guild_settings WHERE guild_id = %s",
        (str(guild_id),)
    )
    result = cur.fetchone()

    cur.close()
    conn.close()

    return int(result[0]) if result else None


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


def already_sent_today(guild_id, user_id, today_key):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT 1 FROM anniversary_log
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
        channel_id = get_channel_id_for_guild(guild.id)

        if not channel_id:
            print(f"No configured channel for {guild.name}")
            continue

        channel = client.get_channel(channel_id)
        if channel is None:
            print(f"Configured channel not found for {guild.name}")
            continue

        for member in guild.members:
            if member.bot or member.joined_at is None:
                continue

            joined = member.joined_at.astimezone(timezone.utc)

            if joined.month == now.month and joined.day == now.day:
                years = now.year - joined.year

                if years >= 1:
                    if already_sent_today(guild.id, member.id, today_key):
                        continue

                    await channel.send(
                        f"""🎖 SYSTEM EVENT — SERVICE MILESTONE

Operator {member.mention} has completed {years} year(s) in the AO.

🕒 Time Served: {years} year(s)
🎮 Arena Breakout: Infinite

Loyalty recognized. Respect earned."""
                    )

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
        await message.channel.send(
            f"""🎖 SYSTEM EVENT — SERVICE MILESTONE

Operator {message.author.mention} has completed another year in the AO.

🕒 Time Served: TEST MODE
🎮 Arena Breakout: Infinite

Loyalty recognized. Respect earned."""
        )

    elif message.content == "!setchannel":
        if not message.author.guild_permissions.administrator:
            await message.channel.send("❌ You must be an admin to set the anniversary channel.")
            return

        set_channel_for_guild(message.guild.id, message.channel.id)
        await message.channel.send(f"✅ Anniversary channel set to {message.channel.mention}")


client.run(TOKEN)