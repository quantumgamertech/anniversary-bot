import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = 1466458582437335363

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
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found. Check CHANNEL_ID.")
        return

    now = datetime.now(timezone.utc)
    today_month = now.month
    today_day = now.day

    for guild in client.guilds:
        for member in guild.members:
            if member.bot or member.joined_at is None:
                continue

            joined = member.joined_at.astimezone(timezone.utc)
            years = now.year - joined.year

            if years >= 1 and joined.month == today_month and joined.day == today_day:
                await channel.send(
                    f"""🎖 SYSTEM EVENT — SERVICE MILESTONE

Operator {member.mention} has completed another year in the AO.

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

    if message.content == "!testanniversary":
        await message.channel.send(
            f"""🎖 SYSTEM EVENT — SERVICE MILESTONE

Operator {message.author.mention} has completed another year in the AO.

🕒 Time Served: TEST MODE
🎮 Arena Breakout: Infinite

Loyalty recognized. Respect earned."""
        )
        
client.run(TOKEN)