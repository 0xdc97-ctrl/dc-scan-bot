import re
import asyncio
import os
import json
import discord
from discord import app_commands
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_BOT_TOKEN      = os.getenv("DISCORD_BOT_TOKEN")
# Your private TG group that feeds CAs — leave blank to accept from any group
TELEGRAM_SOURCE_CHAT   = os.getenv("TELEGRAM_SOURCE_CHAT_ID", "")

CHANNELS_FILE = "channels.json"

EVM_RE    = re.compile(r'\b(0x[0-9a-fA-F]{40})\b')
SOLANA_RE = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{43,44})\b')

# ── Channel registry (persisted to disk) ─────────────────────────────────────

def load_channels() -> set[int]:
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE) as f:
            return set(json.load(f).get("channels", []))
    return set()

def save_channels(channels: set[int]) -> None:
    with open(CHANNELS_FILE, "w") as f:
        json.dump({"channels": list(channels)}, f)

def add_channel(cid: int) -> None:
    ch = load_channels(); ch.add(cid); save_channels(ch)

def remove_channel(cid: int) -> None:
    ch = load_channels(); ch.discard(cid); save_channels(ch)

# ── Discord setup ─────────────────────────────────────────────────────────────

intents        = discord.Intents.default()
discord_client = discord.Client(intents=intents)
tree           = app_commands.CommandTree(discord_client)


@discord_client.event
async def on_ready():
    await tree.sync()   # global sync — commands appear in all servers within ~1 hr
    print(f"Discord ready: {discord_client.user}  |  slash commands synced")


@tree.command(name="setup", description="Show setup guide for the CA feed bot")
async def cmd_setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="CA Feed — Setup Guide",
        description="Follow these 3 steps to start receiving contract addresses automatically.",
        color=0x5865F2
    )
    embed.add_field(
        name="1️⃣  Create a dedicated channel",
        value="Make a channel in your server for CA alerts (e.g. `#ca-feed`).",
        inline=False
    )
    embed.add_field(
        name="2️⃣  Grant the bot permissions in that channel",
        value="Channel Settings → Permissions → add this bot with:\n"
              "✅ **View Channel**\n"
              "✅ **Send Messages**",
        inline=False
    )
    embed.add_field(
        name="3️⃣  Run /start in that channel",
        value="The bot will verify permissions and register the channel.\n"
              "CAs will be forwarded there automatically from that point on.",
        inline=False
    )
    embed.set_footer(text="Run /stop at any time to disable forwarding in a channel.")
    await interaction.response.send_message(embed=embed)


@tree.command(name="start", description="Activate CA feed in this channel")
async def cmd_start(interaction: discord.Interaction):
    channel   = interaction.channel
    bot_perms = channel.permissions_for(interaction.guild.me)

    missing = []
    if not bot_perms.view_channel:   missing.append("View Channel")
    if not bot_perms.send_messages:  missing.append("Send Messages")

    if missing:
        await interaction.response.send_message(
            f"❌ Missing permissions: **{', '.join(missing)}**\n"
            "Grant them in channel settings, then run `/start` again.\n"
            "Need help? Run `/setup` for a step-by-step guide.",
            ephemeral=True
        )
        return

    if channel.id in load_channels():
        await interaction.response.send_message(
            "✅ This channel is already receiving CA feeds.", ephemeral=True
        )
        return

    add_channel(channel.id)
    await interaction.response.send_message(
        "✅ **CA feed activated!**\n"
        "Contract addresses will be posted here automatically.\n"
        "Run `/stop` to disable at any time."
    )


@tree.command(name="stop", description="Deactivate CA feed in this channel")
async def cmd_stop(interaction: discord.Interaction):
    if interaction.channel.id not in load_channels():
        await interaction.response.send_message(
            "This channel isn't receiving CA feeds.", ephemeral=True
        )
        return
    remove_channel(interaction.channel.id)
    await interaction.response.send_message("⛔ CA feed stopped for this channel.")


# ── CA broadcasting ───────────────────────────────────────────────────────────

def extract_cas(text: str) -> list[str]:
    found = [m.group(1) for m in EVM_RE.finditer(text)]
    found += [m.group(1) for m in SOLANA_RE.finditer(text)]
    seen, unique = set(), []
    for ca in found:
        if ca.lower() not in seen:
            seen.add(ca.lower()); unique.append(ca)
    return unique


async def broadcast(ca: str) -> None:
    await discord_client.wait_until_ready()
    channels = load_channels()
    dead: set[int] = set()

    for cid in channels:
        try:
            ch = discord_client.get_channel(cid) or await discord_client.fetch_channel(cid)
            await ch.send(ca)
        except Exception:
            dead.add(cid)   # bot removed or channel deleted — clean it up

    if dead:
        save_channels(load_channels() - dead)


# ── Telegram handler ──────────────────────────────────────────────────────────

async def on_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return
    if TELEGRAM_SOURCE_CHAT and str(msg.chat_id) != TELEGRAM_SOURCE_CHAT:
        return
    for ca in extract_cas(msg.text):
        await broadcast(ca)


# ── Keep-alive server (for UptimeRobot ping) ─────────────────────────────────

async def run_keepalive():
    async def health(request):
        return web.Response(text="OK")
    server = web.Application()
    server.router.add_get("/", health)
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    print("Keep-alive server running on port 8080")


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_telegram_bot():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_telegram_message))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message", "channel_post"])
    print("Telegram bot polling...")
    return app


async def main():
    missing_cfg = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "DISCORD_BOT_TOKEN":  DISCORD_BOT_TOKEN,
    }.items() if not v]
    if missing_cfg:
        raise RuntimeError(f"Missing in .env: {', '.join(missing_cfg)}")

    await run_keepalive()
    tg_app = await run_telegram_bot()
    try:
        await discord_client.start(DISCORD_BOT_TOKEN)
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
