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
            channel_id TEXT
        )
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

    cur.execute(
        "SELECT channel_id, role_name FROM guild_settings WHERE guild_id = %s",
        (str(guild_id),),
    )
    result = cur.fetchone()

    cur.close()
    conn.close()

    if not result:
        return None

    return {"channel_id": int(result[0]) if result[0] else None, "role_name": result[1]}


def set_channel_for_guild(guild_id, channel_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO guild_settings (guild_id, channel_id)
        VALUES (%s, %s)
        ON CONFLICT (guild_id)
        DO UPDATE SET channel_id = EXCLUDED.channel_id
    """,
        (str(guild_id), str(channel_id)),
    )

    conn.commit()
    cur.close()
    conn.close()


def set_role_for_guild(guild_id, role_name):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO guild_settings (guild_id, role_name)
        VALUES (%s, %s)
        ON CONFLICT (guild_id)
        DO UPDATE SET role_name = EXCLUDED.role_name
    """,
        (str(guild_id), role_name),
    )

    conn.commit()
    cur.close()
    conn.close()


def already_sent_today(guild_id, user_id, today_key):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT 1 FROM anniversary_log
        WHERE guild_id = %s AND user_id = %s AND anniversary_date = %s
    """,
        (str(guild_id), str(user_id), today_key),
    )

    result = cur.fetchone()

    cur.close()
    conn.close()

    return result is not None


def mark_sent_today(guild_id, user_id, today_key):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO anniversary_log (guild_id, user_id, anniversary_date)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """,
        (str(guild_id), str(user_id), today_key),
    )

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
        settings = get_guild_settings(guild.id)

        if not settings or not settings.get("channel_id"):
            print(f"No configured channel for {guild.name}")
            continue

        channel = client.get_channel(settings["channel_id"])
        if channel is None:
            print(f"Configured channel not found for {guild.name}")
            continue

        role_name = settings.get("role_name")

        for member in guild.members:
            if member.bot or member.joined_at is None:
                continue

            joined = member.joined_at.astimezone(timezone.utc)

            if joined.month == now.month and joined.day == now.day:
                years = now.year - joined.year

                if years >= 1:
                    if already_sent_today(guild.id, member.id, today_key):
                        continue

                    if role_name:
                        role = discord.utils.get(guild.roles, name=role_name)
                        if role and role not in member.roles:
                            try:
                                await member.add_roles(
                                    role, reason="Server anniversary reward"
                                )
                            except Exception as e:
                                print(f"Role error for {member.name}: {e}")

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
            await message.channel.send("❌ You must be an admin.")
            return

        set_channel_for_guild(message.guild.id, message.channel.id)
        await message.channel.send(f"✅ Channel set to {message.channel.mention}")

    elif message.content.startswith("!setrole "):
        if not message.author.guild_permissions.administrator:
            await message.channel.send("❌ You must be an admin.")
            return

        role_name = message.content.replace("!setrole ", "", 1).strip()

        if not role_name:
            await message.channel.send("❌ Usage: !setrole OG Gooper")
            return

        set_role_for_guild(message.guild.id, role_name)
        await message.channel.send(f"✅ Role set to **{role_name}**")

    elif message.content == "!testrole":
        if not message.author.guild_permissions.administrator:
            await message.channel.send("❌ You must be an admin.")
            return

        settings = get_guild_settings(message.guild.id)

        if not settings or not settings.get("role_name"):
            await message.channel.send("❌ No role set. Use !setrole first.")
            return

        role = discord.utils.get(message.guild.roles, name=settings["role_name"])

        if not role:
            await message.channel.send("❌ Role not found.")
            return

        try:
            await message.author.add_roles(role, reason="Anniversary role test")
            await message.channel.send(f"✅ You got the **{role.name}** role!")
        except Exception as e:
            await message.channel.send(f"❌ Error: {e}")


client.run(TOKEN)