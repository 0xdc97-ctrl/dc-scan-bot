import re
import asyncio
import os
import json
import discord
from discord import app_commands
import aiohttp
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_BOT_TOKEN    = os.getenv("DISCORD_BOT_TOKEN")
TELEGRAM_SOURCE_CHAT    = os.getenv("TELEGRAM_SOURCE_CHAT_ID", "")
OWNER_DISCORD_ID        = int(os.getenv("OWNER_DISCORD_ID", "0"))
TELEGRAM_OWNER_USER_ID  = int(os.getenv("TELEGRAM_OWNER_USER_ID", "0"))

CHANNELS_FILE  = "channels.json"
APPROVED_FILE  = "approved_servers.json"
PENDING_FILE   = "pending_requests.json"

EVM_RE    = re.compile(r'\b(0x[0-9a-fA-F]{40})\b')
SOLANA_RE = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{43,44})\b')

# ── Persistence helpers ───────────────────────────────────────────────────────

def _load(path: str, key: str, cast=list) -> set:
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f).get(key, cast()))
    return set()

def _save(path: str, key: str, data: set) -> None:
    with open(path, "w") as f:
        json.dump({key: list(data)}, f)

# channels.json stores {channel_id_str: webhook_url}
def load_channels() -> dict:
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE) as f:
            return json.load(f).get("channels", {})
    return {}

def save_channels(data: dict) -> None:
    with open(CHANNELS_FILE, "w") as f:
        json.dump({"channels": data}, f)

def add_channel(cid: int, webhook_url: str) -> None:
    ch = load_channels(); ch[str(cid)] = webhook_url; save_channels(ch)

def remove_channel(cid: int) -> None:
    ch = load_channels(); ch.pop(str(cid), None); save_channels(ch)

def load_approved()  -> set[int]: return _load(APPROVED_FILE, "servers")
def save_approved(s) -> None:     _save(APPROVED_FILE, "servers", s)
def approve_server(gid: int) -> None: s = load_approved(); s.add(gid);     save_approved(s)
def revoke_server(gid: int)  -> None: s = load_approved(); s.discard(gid); save_approved(s)

def load_pending() -> dict:
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            return json.load(f).get("requests", {})
    return {}

def save_pending(data: dict) -> None:
    with open(PENDING_FILE, "w") as f:
        json.dump({"requests": data}, f)

def add_pending(guild_id: int, channel_id: int, guild_name: str, requester: str) -> None:
    p = load_pending()
    p[str(guild_id)] = {"channel_id": channel_id, "guild_name": guild_name, "requester": requester}
    save_pending(p)

def pop_pending(guild_id: int) -> dict | None:
    p = load_pending()
    entry = p.pop(str(guild_id), None)
    save_pending(p)
    return entry

# ── Discord client ────────────────────────────────────────────────────────────

intents        = discord.Intents.default()
discord_client = discord.Client(intents=intents)
tree           = app_commands.CommandTree(discord_client)


@discord_client.event
async def on_ready():
    await tree.sync()
    print(f"Discord ready: {discord_client.user}  |  slash commands synced")


# ── Approval buttons ──────────────────────────────────────────────────────────

class ApprovalView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = pop_pending(self.guild_id)
        approve_server(self.guild_id)

        await interaction.response.edit_message(
            content=f"✅ **Approved** — `{entry['guild_name'] if entry else self.guild_id}`",
            embed=None, view=None
        )

        if entry:
            try:
                ch = discord_client.get_channel(entry["channel_id"]) or \
                     await discord_client.fetch_channel(entry["channel_id"])
                webhook = await ch.create_webhook(name="CA Feed")
                add_channel(ch.id, webhook.url)
                await ch.send(
                    "✅ **Access granted!** CA feeds will now be posted in this channel automatically."
                )
            except Exception as e:
                print(f"Failed to set up webhook on approval: {e}")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = pop_pending(self.guild_id)

        await interaction.response.edit_message(
            content=f"❌ **Denied** — `{entry['guild_name'] if entry else self.guild_id}`",
            embed=None, view=None
        )

        if entry:
            try:
                ch = discord_client.get_channel(entry["channel_id"]) or \
                     await discord_client.fetch_channel(entry["channel_id"])
                await ch.send(
                    "❌ **Access denied.** This server was not approved for the CA feed.\n"
                    "Contact the bot owner if you think this is a mistake."
                )
            except Exception:
                pass


async def notify_owner(guild_id: int, guild_name: str, channel_name: str, requester: str) -> None:
    await discord_client.wait_until_ready()
    try:
        owner = await discord_client.fetch_user(OWNER_DISCORD_ID)
        embed = discord.Embed(
            title="New Access Request",
            color=0xF0A500
        )
        embed.add_field(name="Server",    value=guild_name,   inline=True)
        embed.add_field(name="Channel",   value=f"#{channel_name}", inline=True)
        embed.add_field(name="Requested by", value=requester, inline=True)
        embed.add_field(name="Server ID", value=str(guild_id), inline=False)
        await owner.send(embed=embed, view=ApprovalView(guild_id))
    except Exception as e:
        print(f"Failed to DM owner: {e}")


# ── Slash commands ────────────────────────────────────────────────────────────

@tree.command(name="setup", description="Show setup guide for the CA feed bot")
async def cmd_setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="CA Feed — Setup Guide",
        description="Follow these steps to request access to the CA feed.",
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
              "✅ **View Channel**\n✅ **Send Messages**",
        inline=False
    )
    embed.add_field(
        name="3️⃣  Run /start in that channel",
        value="This sends an access request to the bot owner.\n"
              "Once approved, CAs will be forwarded here automatically.",
        inline=False
    )
    embed.set_footer(text="Run /stop at any time to disable forwarding.")
    await interaction.response.send_message(embed=embed)


@tree.command(name="start", description="Request CA feed access for this channel")
async def cmd_start(interaction: discord.Interaction):
    channel   = interaction.channel
    guild     = interaction.guild
    bot_perms = channel.permissions_for(guild.me)

    missing = []
    if not bot_perms.view_channel:    missing.append("View Channel")
    if not bot_perms.send_messages:   missing.append("Send Messages")
    if not bot_perms.manage_webhooks: missing.append("Manage Webhooks")

    if missing:
        await interaction.response.send_message(
            f"❌ Missing permissions: **{', '.join(missing)}**\n"
            "Grant them in channel settings, then run `/start` again.\n"
            "Need help? Run `/setup`.",
            ephemeral=True
        )
        return

    # Already approved and active
    if guild.id in load_approved() and str(channel.id) in load_channels():
        await interaction.response.send_message(
            "✅ This channel is already receiving CA feeds.", ephemeral=True
        )
        return

    # Approved server, just register the channel
    if guild.id in load_approved():
        webhook = await channel.create_webhook(name="CA Feed")
        add_channel(channel.id, webhook.url)
        await interaction.response.send_message(
            "✅ **CA feed activated!** Contract addresses will be posted here automatically.\n"
            "Run `/stop` to disable at any time."
        )
        return

    # Not approved — send request to owner
    pending = load_pending()
    if str(guild.id) in pending:
        await interaction.response.send_message(
            "⏳ An access request for this server is already pending.\n"
            "Please wait for the owner to approve it.",
            ephemeral=True
        )
        return

    requester = f"{interaction.user.name} ({interaction.user.id})"
    add_pending(guild.id, channel.id, guild.name, requester)
    await notify_owner(guild.id, guild.name, channel.name, requester)

    await interaction.response.send_message(
        "📨 **Access request sent!**\n"
        "The bot owner has been notified. You'll receive a confirmation here once approved.",
        ephemeral=False
    )


@tree.command(name="stop", description="Deactivate CA feed in this channel")
async def cmd_stop(interaction: discord.Interaction):
    if str(interaction.channel.id) not in load_channels():
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
    dead = []
    for cid_str in channels:
        try:
            ch = discord_client.get_channel(int(cid_str)) or \
                 await discord_client.fetch_channel(int(cid_str))
            await ch.send(ca)
        except Exception:
            dead.append(cid_str)
    if dead:
        ch = load_channels()
        for cid_str in dead:
            ch.pop(cid_str, None)
        save_channels(ch)


# ── Telegram handler ──────────────────────────────────────────────────────────

async def on_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return
    if TELEGRAM_SOURCE_CHAT and str(msg.chat_id) != TELEGRAM_SOURCE_CHAT:
        return
    if TELEGRAM_OWNER_USER_ID and msg.from_user and msg.from_user.id != TELEGRAM_OWNER_USER_ID:
        return
    for ca in extract_cas(msg.text):
        await broadcast(ca)


# ── Keep-alive server ─────────────────────────────────────────────────────────

async def run_keepalive():
    async def health(request):
        return web.Response(text="OK")
    server = web.Application()
    server.router.add_get("/", health)
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    print("Keep-alive server on port 8080")


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
    missing = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "DISCORD_BOT_TOKEN":  DISCORD_BOT_TOKEN,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing in .env: {', '.join(missing)}")

    if os.getenv("REPLIT"):
        await run_keepalive()
    tg_app = await run_telegram_bot()
    try:
        await discord_client.start(DISCORD_BOT_TOKEN)
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
