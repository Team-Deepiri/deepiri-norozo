import hashlib
import hmac
import json
import os
import re
from urllib.parse import urlparse
from typing import Optional

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from github import invite_user
from meetings import setup_meeting_features
from onboarding import ApprovalView
from plaky import create_task, get_tasks


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_PAT = os.getenv("GITHUB_PAT")
GITHUB_ORG = os.getenv("GITHUB_ORG")
PLAKY_API_KEY = os.getenv("PLAKY_API_KEY")
PLAKY_WEBHOOK_SECRET = os.getenv("PLAKY_WEBHOOK_SECRET", "")


def _int_env(name: str) -> Optional[int]:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


STAFF_CHANNEL_ID = _int_env("STAFF_CHANNEL_ID")
PR_CHANNEL_ID = _int_env("PR_CHANNEL_ID")
QA_CHANNEL_ID = _int_env("QA_CHANNEL_ID")
SERVER_COM_CHANNEL_ID = _int_env("SERVER_COM_CHANNEL_ID")
DEV_TEAM_ROLE_ID = _int_env("DEV_TEAM_ROLE_ID")

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
PR_URL_RE = re.compile(r"https?://(?:www\.)?github\.com/[^\s]+/[^\s]+/pull/(\d+)", re.IGNORECASE)
PLAKY_URL_RE = re.compile(r"https?://(?:www\.)?app\.plaky\.com/\S+", re.IGNORECASE)
GITHUB_USERNAME_RE = re.compile(r"^[A-Za-z\d](?:[A-Za-z\d-]{0,37}[A-Za-z\d])?$")
GITHUB_RESERVED_PATHS = {
    "about",
    "account",
    "blog",
    "collections",
    "contact",
    "customer-stories",
    "dashboard",
    "enterprise",
    "events",
    "explore",
    "features",
    "gist",
    "github",
    "issues",
    "login",
    "marketplace",
    "new",
    "notifications",
    "orgs",
    "pricing",
    "pulls",
    "search",
    "security",
    "settings",
    "site",
    "sponsors",
    "teams",
    "topics",
    "trending",
}


class DeepiriBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.guilds = True

        super().__init__(command_prefix="!", intents=intents)
        self.webhook_runner: Optional[web.AppRunner] = None

    async def setup_hook(self) -> None:
        if DEV_TEAM_ROLE_ID is not None:
            self.add_view(ApprovalView(dev_team_role_id=DEV_TEAM_ROLE_ID))
        await self.tree.sync()


bot = DeepiriBot()
meeting_service = setup_meeting_features(bot)


def _extract_github_profile_username(message_content: str) -> Optional[str]:
    for match in URL_RE.finditer(message_content):
        raw_url = match.group(0).rstrip(".,!?:;)\"'>]")
        if "github.com/" not in raw_url.lower():
            continue

        parsed = urlparse(raw_url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host != "github.com":
            continue

        path = parsed.path.strip("/")
        if not path:
            continue

        segments = [segment for segment in path.split("/") if segment]
        if len(segments) != 1:
            continue

        username = segments[0]
        if username.lower() in GITHUB_RESERVED_PATHS:
            continue

        if not GITHUB_USERNAME_RE.match(username):
            continue

        return username

    return None


def _is_valid_plaky_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.strip()
    if provided.startswith("sha256="):
        provided = provided.split("=", 1)[1]

    return hmac.compare_digest(provided, expected)


async def _channel_from_id(channel_id: Optional[int]) -> Optional[discord.TextChannel]:
    if not channel_id:
        return None

    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel

    try:
        fetched = await bot.fetch_channel(channel_id)
        if isinstance(fetched, discord.TextChannel):
            return fetched
    except discord.NotFound:
        return None

    return None


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (id={bot.user.id if bot.user else 'unknown'})")

    meeting_service.start_loop()

    if bot.webhook_runner is None:
        await start_webhook_server()


@bot.event
async def on_member_join(member: discord.Member) -> None:
    welcome_channel = await _channel_from_id(SERVER_COM_CHANNEL_ID)
    if welcome_channel:
        await welcome_channel.send(
            f"Welcome {member.mention}! Please sign the IPCA first, then run /ipca-signed to request DEV team access."
        )

    try:
        await member.send(
            "Welcome to Deepiri. Before joining the DEV team, please sign the IPCA. "
            "After signing, run /ipca-signed in the server so staff can approve your role."
        )
    except discord.Forbidden:
        pass


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    content = message.content or ""

    if PR_CHANNEL_ID and message.channel.id == PR_CHANNEL_ID:
        pr_match = PR_URL_RE.search(content)
        plaky_match = PLAKY_URL_RE.search(content)

        if pr_match and plaky_match:
            pr_number = pr_match.group(1)
            pr_url = pr_match.group(0)
            plaky_url = plaky_match.group(0)
            embed = discord.Embed(
                title=f"PR #{pr_number} linked to Plaky task",
                description=f"[Pull Request]({pr_url})\\n[Plaky Task]({plaky_url})",
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f"Linked by {message.author.display_name}")
            await message.channel.send(embed=embed)
        elif pr_match and not plaky_match:
            await message.channel.send(
                f"{message.author.mention} please include the Plaky task URL (app.plaky.com/...) with your PR link."
            )

    github_username = _extract_github_profile_username(content)
    if github_username:
        result = invite_user(
            username=github_username,
            github_org=GITHUB_ORG or "",
            github_pat=GITHUB_PAT or "",
        )

        if result.get("ok"):
            await message.reply(result.get("message", "GitHub invite sent."))
        else:
            await message.reply(result.get("message", "GitHub invite could not be sent."))

    await bot.process_commands(message)


@bot.tree.command(name="ipca-signed", description="Request DEV team access after signing IPCA")
async def ipca_signed(interaction: discord.Interaction) -> None:
    if STAFF_CHANNEL_ID is None:
        await interaction.response.send_message("STAFF_CHANNEL_ID is not configured.", ephemeral=True)
        return

    if DEV_TEAM_ROLE_ID is None:
        await interaction.response.send_message("DEV_TEAM_ROLE_ID is not configured.", ephemeral=True)
        return

    staff_channel = await _channel_from_id(STAFF_CHANNEL_ID)
    if not staff_channel:
        await interaction.response.send_message("Could not find the configured staff channel.", ephemeral=True)
        return

    view = ApprovalView(dev_team_role_id=DEV_TEAM_ROLE_ID)
    embed = discord.Embed(
        title="IPCA Approval Request",
        description=f"User {interaction.user.mention} says they signed IPCA. Click Approve to grant DEV team role.",
        color=discord.Color.green(),
    )
    await staff_channel.send(embed=embed, view=view)

    await interaction.response.send_message("Your approval request was sent to staff.", ephemeral=True)


@bot.tree.command(name="plaky-request", description="Create a Plaky task")
@app_commands.describe(title="Task title", description="Task description", priority="Task priority")
@app_commands.choices(
    priority=[
        app_commands.Choice(name="low", value="low"),
        app_commands.Choice(name="medium", value="medium"),
        app_commands.Choice(name="high", value="high"),
    ]
)
async def plaky_request(
    interaction: discord.Interaction,
    title: str,
    description: str,
    priority: app_commands.Choice[str],
) -> None:
    result = create_task(
        title=title,
        description=description,
        priority=priority.value,
        api_key=PLAKY_API_KEY or "",
    )

    if result.get("ok"):
        task_url = result.get("task_url") or "(no URL returned)"
        await interaction.response.send_message(f"Plaky task created: {task_url}")
        return

    await interaction.response.send_message(result.get("message", "Failed to create Plaky task."), ephemeral=True)


@bot.tree.command(name="plaky-status", description="Post open Plaky tasks summary to QA channel")
async def plaky_status(interaction: discord.Interaction) -> None:
    if QA_CHANNEL_ID is None:
        await interaction.response.send_message("QA_CHANNEL_ID is not configured.", ephemeral=True)
        return

    qa_channel = await _channel_from_id(QA_CHANNEL_ID)
    if not qa_channel:
        await interaction.response.send_message("Could not find the configured QA channel.", ephemeral=True)
        return

    result = get_tasks(api_key=PLAKY_API_KEY or "", status="open")
    if not result.get("ok"):
        await interaction.response.send_message(result.get("message", "Failed to fetch tasks."), ephemeral=True)
        return

    tasks = result.get("tasks", [])
    if not tasks:
        await qa_channel.send("No open Plaky tasks found.")
        await interaction.response.send_message("Posted status to QA channel.", ephemeral=True)
        return

    lines = ["Open Plaky tasks:"]
    for task in tasks[:20]:
        task_title = task.get("title", "Untitled")
        task_status = task.get("status", "unknown")
        task_url = task.get("url") or task.get("taskUrl") or ""
        if task_url:
            lines.append(f"- [{task_title}]({task_url}) - status: {task_status}")
        else:
            lines.append(f"- {task_title} - status: {task_status}")

    await qa_channel.send("\n".join(lines))
    await interaction.response.send_message("Posted status to QA channel.", ephemeral=True)


async def plaky_webhook_handler(request: web.Request) -> web.Response:
    raw_body = await request.read()

    if PLAKY_WEBHOOK_SECRET:
        signature_header = (
            request.headers.get("X-Plaky-Signature")
            or request.headers.get("x-plaky-signature")
            or request.headers.get("X-Signature")
        )
        if not signature_header:
            return web.json_response({"ok": False, "message": "Missing signature header"}, status=401)

        if not _is_valid_plaky_signature(raw_body, signature_header, PLAKY_WEBHOOK_SECRET):
            return web.json_response({"ok": False, "message": "Invalid signature"}, status=401)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return web.json_response({"ok": False, "message": "Invalid JSON"}, status=400)

    status = str(payload.get("status", "")).strip().lower()
    priority = str(payload.get("priority", "")).strip().lower()

    should_alert = status == "blocked" or priority in {"high", "high priority"}
    if should_alert and QA_CHANNEL_ID:
        channel = await _channel_from_id(QA_CHANNEL_ID)
        if channel:
            title = payload.get("title", "Plaky task")
            task_url = payload.get("url") or payload.get("taskUrl") or ""
            description = f"Status update for **{title}**\\nStatus: **{status or 'unknown'}**\\nPriority: **{priority or 'unknown'}**"
            if task_url:
                description += f"\\n{task_url}"
            await channel.send(f":warning: {description}")

    return web.json_response({"ok": True})


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "deepiri-discord-bot"})


async def start_webhook_server() -> None:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/plaky/webhook", plaky_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host=WEBHOOK_HOST, port=WEBHOOK_PORT)
    await site.start()

    bot.webhook_runner = runner
    print(f"Plaky webhook server listening on http://{WEBHOOK_HOST}:{WEBHOOK_PORT}/plaky/webhook")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is required in .env")

    bot.run(DISCORD_TOKEN)
