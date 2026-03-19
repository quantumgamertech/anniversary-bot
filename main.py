import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks

TOKEN = os.getenv("TOKEN")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    if not check_anniversaries.is_running():
        check_anniversaries.start()


@tasks.loop(hours=24)
async def check_anniversaries():
    now = datetime.now(timezone.utc)

    for guild in client.guilds:
        channel = None

        for text_channel in guild.text_channels:
            permissions = text_channel.permissions_for(guild.me)
            if permissions.view_channel and permissions.send_messages:
                channel = text_channel
                break

        if channel is None:
            print(f"No valid channel found in {guild.name}")
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
    if message.author.bot:
        return

    if message.content == "!test":
        await message.channel.send("ANV Bot is working 🔥")

    if message.content == "!testanniversary":
        await message.channel.send(
            f"""🎖 SYSTEM EVENT — SERVICE MILESTONE

Operator {message.author.mention} has completed another year in the AO.

🕒 Time Served: TEST MODE
🎮 Arena Breakout: Infinite

Loyalty recognized. Respect earned."""
        )


client.run(TOKEN)