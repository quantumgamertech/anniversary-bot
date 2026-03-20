import os
import json
from datetime import datetime, timezone

import discord
from discord.ext import tasks

TOKEN = os.getenv("TOKEN")
DATA_FILE = "server_settings.json"

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client = discord.Client(intents=intents)


def load_settings():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_settings(settings):
    with open(DATA_FILE, "w") as f:
        json.dump(settings, f, indent=4)


settings = load_settings()


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    if not check_anniversaries.is_running():
        check_anniversaries.start()


@tasks.loop(hours=24)
async def check_anniversaries():
    now = datetime.now(timezone.utc)

    for guild in client.guilds:
        guild_id = str(guild.id)
        channel = None

        if guild_id in settings and "channel_id" in settings[guild_id]:
            channel = client.get_channel(settings[guild_id]["channel_id"])

        if channel is None:
            print(f"No configured channel for {guild.name}")
            continue

        for member in guild.members:
            if member.bot or member.joined_at is None:
                continue

            joined = member.joined_at.astimezone(timezone.utc)

            if joined.month == now.month and joined.day == now.day:
                years = now.year - joined.year

                if years >= 1:
                    await channel.send(
                        f"""🎖 SYSTEM EVENT — SERVICE MILESTONE

Operator {member.mention} has completed {years} year(s) in the AO.

🕒 Time Served: {years} year(s)
🎮 Arena Breakout: Infinite

Loyalty recognized. Respect earned."""
                    )


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

        guild_id = str(message.guild.id)
        settings[guild_id] = {"channel_id": message.channel.id}
        save_settings(settings)

        await message.channel.send(f"✅ Anniversary channel set to {message.channel.mention}")