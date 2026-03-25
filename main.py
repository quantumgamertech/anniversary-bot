import os
from datetime import datetime, timezone

import discord
from discord.ext import tasks
import psycopg2

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

OWNER_ID = 207279875902537731

DEFAULT_MESSAGE = """{user} has completed {years} year(s) in the AO.

🕒 Time Served: {years} year(s)
🎮 Arena Breakout: Infinite

Loyalty recognized. Respect earned."""

EMBED_COLOR = 0x8A2BE2

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client = discord.Client(intents=intents)


class QGTSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Setup",
        style=discord.ButtonStyle.success,
        custom_id="qgt_setup_button"
    )
    async def setup_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        embed = discord.Embed(
            title="⚙️ QGT Competitive Setup Guide",
            description="Optimized settings for max performance, low latency, and overall stability.",
            color=discord.Color.green()
        )

        embed.add_field(
            name="🔥 NVIDIA Settings",
            value=(
                "• Low Latency Mode: Ultra\n"
                "• Power Management Mode: Prefer Maximum Performance\n"
                "• V-Sync: OFF\n"
                "• Max Frame Rate: OFF in NVIDIA\n"
                "• Texture Filtering - Quality: Performance\n"
                "• Threaded Optimization: ON"
            ),
            inline=False
        )

        embed.add_field(
            name="🧠 Windows Settings",
            value=(
                "• Game Mode: ON\n"
                "• Hardware-Accelerated GPU Scheduling: ON\n"
                "• Restart after major driver / chipset changes"
            ),
            inline=False
        )

        embed.add_field(
            name="🌐 Network",
            value=(
                "• Use Ethernet if possible\n"
                "• Close background downloads/apps\n"
                "• DNS: 1.1.1.1 / 8.8.8.8"
            ),
            inline=False
        )

        embed.set_footer(text="QGT System • Built for Performance")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Audio",
        style=discord.ButtonStyle.primary,
        custom_id="qgt_audio_button"
    )
    async def audio_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        embed = discord.Embed(
            title="🎧 QGT Audio Optimization",
            description="Footstep-focused setup for competitive advantage.",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="🔊 Windows Audio",
            value=(
                "• Spatial Sound: OFF\n"
                "• Enhancements: OFF\n"
                "• Format: 24-bit / 48000 Hz\n"
                "• Stereo: ON"
            ),
            inline=False
        )

        embed.add_field(
            name="🎯 EQ Focus",
            value=(
                "• Cut bass mud\n"
                "• Boost 2kHz–4kHz for footsteps\n"
                "• Control harsh highs"
            ),
            inline=False
        )

        embed.add_field(
            name="🔥 Result",
            value=(
                "• Clearer footsteps\n"
                "• Better direction tracking\n"
                "• Less listening fatigue"
            ),
            inline=False
        )

        embed.set_footer(text="QGT Audio System")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="FPS",
        style=discord.ButtonStyle.danger,
        custom_id="qgt_fps_button"
    )
    async def fps_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        embed = discord.Embed(
            title="🎮 QGT FPS Optimization",
            description="Competitive settings for Arena Breakout: Infinite and similar shooters.",
            color=discord.Color.red()
        )

        embed.add_field(
            name="⚡ Core Settings",
            value=(
                "• V-Sync: OFF\n"
                "• Reflex: ON + BOOST\n"
                "• DLSS: Balanced or Performance\n"
                "• Frame Generation: Optional\n"
                "• Max FPS: 237 for 240Hz monitors"
            ),
            inline=False
        )

        embed.add_field(
            name="👀 Visual Priority",
            value=(
                "• Prioritize visibility over eye candy\n"
                "• Keep contrast strong\n"
                "• Avoid over-brightness\n"
                "• Reduce clutter when needed"
            ),
            inline=False
        )

        embed.set_footer(text="QGT Performance Mode")
        await interaction.response.send_message(embed=embed, ephemeral=True)


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


def build_message(template, member, years):
    return template.format(
        user=member.mention,
        years=years
    )


def build_anniversary_embed(member, years, custom_message):
    description = build_message(
        custom_message if custom_message else DEFAULT_MESSAGE,
        member,
        years
    )

    embed = discord.Embed(
        title="🎉 Anniversary Celebration",
        description=description,
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Time Served", value=f"{years} year(s)", inline=True)

    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.set_footer(text="Celebrating your time in the community.")
    return embed


def build_test_embed(member, custom_message):
    description = build_message(
        custom_message if custom_message else DEFAULT_MESSAGE,
        member,
        "Preview"
    )

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

    embed.set_footer(text="Preview mode • Not a real anniversary")
    return embed


@client.event
async def on_ready():
    setup_database()
    client.add_view(QGTSetupView())
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

                    embed = build_anniversary_embed(member, years, custom_message)
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

        if is_premium(message.guild.id) and settings and settings.get("custom_message"):
            custom_message = settings["custom_message"]

        embed = build_test_embed(message.author, custom_message)
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
• Role rewards (coming soon)
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
• Role rewards (coming soon)
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

    elif message.content == "!messagehelp":
        await message.channel.send(
            """📝 Custom message help

Use:
!setmessage your message here

Available placeholders:
{user}   = mentions the user
{years}  = anniversary years

Example:
!setmessage 🎉 {user} just hit {years} year(s)!

💎 Custom messages are a premium feature.
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

    elif message.content == "!setup":
        embed = discord.Embed(
            title="⚙️ QGT Competitive Setup Guide",
            description="Optimized settings for max performance, low latency, and overall stability.",
            color=discord.Color.green()
        )

        embed.add_field(
            name="🔥 NVIDIA Settings",
            value=(
                "• Low Latency Mode: Ultra\n"
                "• Power Management Mode: Prefer Maximum Performance\n"
                "• V-Sync: OFF\n"
                "• Max Frame Rate: OFF in NVIDIA\n"
                "• Texture Filtering - Quality: Performance\n"
                "• Threaded Optimization: ON"
            ),
            inline=False
        )

        embed.add_field(
            name="🧠 Windows Settings",
            value=(
                "• Game Mode: ON\n"
                "• Hardware-Accelerated GPU Scheduling: ON\n"
                "• Restart after major driver / chipset changes"
            ),
            inline=False
        )

        embed.add_field(
            name="🌐 Network",
            value=(
                "• Use Ethernet if possible\n"
                "• Close background downloads/apps\n"
                "• DNS: 1.1.1.1 / 8.8.8.8"
            ),
            inline=False
        )

        embed.set_footer(text="QGT System • Built for Performance")
        await message.channel.send(embed=embed)

    elif message.content == "!audio":
        embed = discord.Embed(
            title="🎧 QGT Audio Optimization",
            description="Footstep-focused setup for competitive advantage.",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="🔊 Windows Audio",
            value=(
                "• Spatial Sound: OFF\n"
                "• Enhancements: OFF\n"
                "• Format: 24-bit / 48000 Hz\n"
                "• Stereo: ON"
            ),
            inline=False
        )

        embed.add_field(
            name="🎯 EQ Focus",
            value=(
                "• Cut bass mud\n"
                "• Boost 2kHz–4kHz for footsteps\n"
                "• Control harsh highs"
            ),
            inline=False
        )

        embed.add_field(
            name="🔥 Result",
            value=(
                "• Clearer footsteps\n"
                "• Better direction tracking\n"
                "• Less listening fatigue"
            ),
            inline=False
        )

        embed.set_footer(text="QGT Audio System")
        await message.channel.send(embed=embed)

    elif message.content == "!fps":
        embed = discord.Embed(
            title="🎮 QGT FPS Optimization",
            description="Competitive settings for Arena Breakout: Infinite and similar shooters.",
            color=discord.Color.red()
        )

        embed.add_field(
            name="⚡ Core Settings",
            value=(
                "• V-Sync: OFF\n"
                "• Reflex: ON + BOOST\n"
                "• DLSS: Balanced or Performance\n"
                "• Frame Generation: Optional\n"
                "• Max FPS: 237 for 240Hz monitors"
            ),
            inline=False
        )

        embed.add_field(
            name="👀 Visual Priority",
            value=(
                "• Prioritize visibility over eye candy\n"
                "• Keep contrast strong\n"
                "• Avoid over-brightness\n"
                "• Reduce clutter when needed"
            ),
            inline=False
        )

        embed.set_footer(text="QGT Performance Mode")
        await message.channel.send(embed=embed)

    elif message.content == "!panel":
        embed = discord.Embed(
            title="🚀 QGT Optimization Panel",
            description="Click a button below to get optimized settings.",
            color=discord.Color.gold()
        )

        embed.add_field(
            name="Available Guides",
            value="⚙️ Setup\n🎧 Audio\n🎮 FPS",
            inline=False
        )

        embed.set_footer(text="QGT Control Center")
        await message.channel.send(embed=embed, view=QGTSetupView())


client.run(TOKEN)