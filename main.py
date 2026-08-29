import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import discord
import httpx
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bot import format_discussion_body, format_discussion_title
from github import (
    add_user_to_team,
    invite_user,
    remove_user_from_org,
    remove_user_from_team,
)
from github_discussion import GitHubDiscussionError, create_github_discussion
from meetings import setup_meeting_features
from onboarding import ApprovalView
from plaky import create_task, get_tasks

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("deepiri.main")

DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or "").strip() or None
GITHUB_PAT = os.getenv("GITHUB_PAT")
GITHUB_ORG = os.getenv("GITHUB_ORG")
GITHUB_SUPPORT_TEAM_SLUG = os.getenv("GITHUB_SUPPORT_TEAM_SLUG", "support-team")
GITHUB_IT_TEAM_SLUG = os.getenv("GITHUB_IT_TEAM_SLUG", "it-management-team")
PLAKY_API_KEY = os.getenv("PLAKY_API_KEY")
PLAKY_WEBHOOK_SECRET = os.getenv("PLAKY_WEBHOOK_SECRET", "")


def _int_env(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


STAFF_CHANNEL_ID = _int_env("STAFF_CHANNEL_ID")  # #it-notifications 1438671182025982043
PR_CHANNEL_ID = _int_env("PR_CHANNEL_ID")
QA_CHANNEL_ID = _int_env("QA_CHANNEL_ID")
SERVER_COM_CHANNEL_ID = _int_env("SERVER_COM_CHANNEL_ID")
DEV_TEAM_ROLE_ID = _int_env("DEV_TEAM_ROLE_ID")
AVAILABLE_ROLE_ID = _int_env("AVAILABLE_ROLE_ID")
STAFF_ROLE_ID = _int_env("STAFF_ROLE_ID")
SUPPORT_SESSIONS_CHANNEL_ID = _int_env("SUPPORT_SESSIONS_CHANNEL_ID")  # #support-tickets 1435722355723993088
GITHUB_PROFILES_CHANNEL_ID = _int_env("GITHUB_PROFILES_CHANNEL_ID")  # #github-profiles 1435086187822845982
IT_OPERATIONS_SUPPORT_ROLE_ID = _int_env("IT_OPERATIONS_SUPPORT_ROLE_ID") or _int_env("SUPPORT_TEAM_ROLE_ID")
QA_ROLE_ID = _int_env("QA_ROLE_ID")
ANNOUNCEMENTS_CHANNEL_ID = _int_env("DISCORD_CHANNEL_ID") or _int_env("ANNOUNCEMENTS_CHANNEL_ID")  # #announcements 1436509524818395156
ANNOUNCEMENTS_CHANNEL_NAME = os.getenv("DISCORD_CHANNEL_NAME", "announcements")

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT", "8080"))

PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL = (
    os.getenv("PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL")
    or os.getenv("PLATFORM_WEBHOOK_URL")
    or os.getenv("PLATFORM_API_URL")
    or ""
).strip()
PLATFORM_ANNOUNCEMENTS_SECRET = (
    os.getenv("PLATFORM_ANNOUNCEMENTS_WEBHOOK_SECRET")
    or os.getenv("PLATFORM_WEBHOOK_SECRET")
    or os.getenv("ANNOUNCEMENTS_WEBHOOK_SECRET")
    or ""
).strip()
ANNOUNCEMENTS_INBOUND_SECRET = (
    os.getenv("ANNOUNCEMENTS_INBOUND_SECRET") or PLATFORM_ANNOUNCEMENTS_SECRET or ""
).strip()

GITHUB_USERNAME_MAP_PATH = Path(os.getenv("GITHUB_USERNAME_MAP_FILE", "github_username_map.json"))
ANNOUNCEMENT_DEDUP_PATH = Path("announcement_webhook_events.json")
ANNOUNCEMENT_DEDUP_TTL_SECONDS = 7 * 24 * 60 * 60
ANNOUNCEMENT_DEDUP_MAX_EVENTS = 1000
_announcement_dedup_lock = asyncio.Lock()

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
        self.webhook_runner: web.AppRunner | None = None
        self.meeting_service = None  # Will be set by factory

    async def setup_hook(self) -> None:
        if DEV_TEAM_ROLE_ID is not None and AVAILABLE_ROLE_ID is not None:
            self.add_view(ApprovalView(dev_team_role_id=DEV_TEAM_ROLE_ID, available_role_id=AVAILABLE_ROLE_ID))
        await self.tree.sync()


# Global reference to current bot; will be replaced on each retry.
bot: DeepiriBot | None = None
meeting_service = None


def _create_and_register_bot() -> DeepiriBot:
    """
    Factory function that creates a fresh bot instance and registers ALL handlers.
    
    This ensures each retry gets a fully configured bot with:
    - Event handlers (on_ready, on_member_join, on_message)
    - Slash commands (github-invite-request, ipca-signed, offboard-user, etc.)
    - Meeting service
    - Approval view for IPCA flow
    
    Returns:
        A fully configured DeepiriBot instance ready to start()
    """
    new_bot = DeepiriBot()
    
    # Register event handlers
    @new_bot.event
    async def on_ready() -> None:
        print(f"Logged in as {new_bot.user} (id={new_bot.user.id if new_bot.user else 'unknown'})")
        if new_bot.meeting_service:
            new_bot.meeting_service.start_loop()

    @new_bot.event
    async def on_member_join(member: discord.Member) -> None:
        welcome_channel = await _channel_from_id(SERVER_COM_CHANNEL_ID)
        if welcome_channel:
            await welcome_channel.send(
                f"Welcome {member.mention}! Please sign the IPCA first, then run /github-invite-request in the support tickets channel to request a GitHub invite."
            )
        try:
            await member.send(
                "Welcome to Deepiri. Before joining the DEV team, please sign the IPCA. "
                "After signing, run /github-invite-request in the support tickets channel so IT/staff can approve your GitHub invite."
            )
        except discord.Forbidden:
            pass

    @new_bot.event
    async def on_member_update(before: discord.Member, after: discord.Member) -> None:
        if not GITHUB_ORG or not GITHUB_PAT:
            return

        before_roles = {role.id for role in before.roles}
        after_roles = {role.id for role in after.roles}
        added = after_roles - before_roles
        if not added:
            return

        github_username = _get_github_username_for_member(after)
        if not github_username:
            logger.info(
                "Member %s gained roles %s but no GitHub username mapping found, skipping team sync",
                after.id,
                added,
            )
            return

        added_names = {role.name.strip().lower() for role in after.roles if role.id in added}
        qa_triggered = QA_ROLE_ID in added if QA_ROLE_ID is not None else bool(
            {"qa", "quality assurance"} & added_names
        )
        it_candidates = {
            "it operations support",
            "support operations",
            "it",
            "it-management",
            "security it",
            "it operations",
            "support operations and security it",
        }
        it_triggered = (
            IT_OPERATIONS_SUPPORT_ROLE_ID in added
            if IT_OPERATIONS_SUPPORT_ROLE_ID is not None
            else bool(it_candidates & added_names)
        )

        for triggered, team_slug, role_label in (
            (qa_triggered, GITHUB_SUPPORT_TEAM_SLUG, "QA"),
            (it_triggered, GITHUB_IT_TEAM_SLUG, "IT"),
        ):
            if not triggered:
                continue
            logger.info("Syncing %s (%s) to GitHub team %s for %s role", after, github_username, team_slug, role_label)
            try:
                result = await asyncio.to_thread(
                    add_user_to_team,
                    username=github_username,
                    github_org=GITHUB_ORG,
                    github_pat=GITHUB_PAT,
                    team_slug=team_slug,
                )
                if not result.get("ok"):
                    logger.warning("Failed to add %s to %s team: %s", github_username, role_label, result.get("message"))
            except Exception:
                logger.exception("Exception syncing %s role to GitHub team", role_label)

    @new_bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        content = message.content or ""
        await notify_support_team_for_message(message)
        if _is_announcements_channel(message.channel):
            title = format_discussion_title(message.content)
            body = format_discussion_body(message)
            try:
                await create_github_discussion(title, body)
            except GitHubDiscussionError as exc:
                logger.error("Discussion bridge failed for message %s: %s", message.id, exc)
            try:
                await _forward_announcement_to_platform(message)
            except Exception:
                logger.exception("Platform forward failed for message %s", message.id)
        if PR_CHANNEL_ID and message.channel.id == PR_CHANNEL_ID:
            pr_match = PR_URL_RE.search(content)
            plaky_match = PLAKY_URL_RE.search(content)

            if pr_match and plaky_match:
                pr_number = pr_match.group(1)
                pr_url = pr_match.group(0)
                plaky_url = plaky_match.group(0)
                embed = discord.Embed(
                    title=f"PR #{pr_number} linked to Plaky task",
                    description=f"[Pull Request]({pr_url})\n[Plaky Task]({plaky_url})",
                    color=discord.Color.blue(),
                )
                embed.set_footer(text=f"Linked by {message.author.display_name}")
                await message.channel.send(embed=embed)
            elif pr_match:
                await message.channel.send(
                    f"{message.author.mention} please include the Plaky task URL (app.plaky.com/...) with your PR link."
                )

        await new_bot.process_commands(message)

    # Register slash commands
    @new_bot.tree.command(name="github-invite-request", description="Request a GitHub invite after signing ICPA")
    @app_commands.describe(github_username="Your GitHub profile username", team="Optional team to add the user to (support or it)")
    @app_commands.choices(
        team=[
            app_commands.Choice(name="support", value="support"),
            app_commands.Choice(name="it", value="it"),
        ]
    )
    async def github_invite_request(interaction: discord.Interaction, github_username: str, team: app_commands.Choice[str] | None = None) -> None:
        await handle_github_invite_request(interaction, github_username, team=team.value if team else None)

    @new_bot.tree.command(name="ipca-signed", description="Request DEV team and Available roles after signing ICPA")
    @app_commands.describe(github_username="Your GitHub profile username")
    async def ipca_signed(interaction: discord.Interaction, github_username: str) -> None:
        await handle_ipca_signed(interaction, github_username)

    @new_bot.tree.command(name="offboard-user", description="Offboard a user from Discord roles and GitHub membership")
    @app_commands.describe(member="The Discord member to offboard", github_username="Their GitHub profile username", team="Optional team to remove them from (support or it)")
    @app_commands.choices(
        team=[
            app_commands.Choice(name="support", value="support"),
            app_commands.Choice(name="it", value="it"),
        ]
    )
    async def offboard_user(
        interaction: discord.Interaction,
        member: discord.Member,
        github_username: str,
        team: app_commands.Choice[str] | None = None,
    ) -> None:
        team_value = team.value if hasattr(team, "value") else team
        await handle_offboard_user(interaction, member, github_username, team=team_value)

    @new_bot.tree.command(name="plaky-request", description="Create a Plaky task")
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

    @new_bot.tree.command(name="plaky-status", description="Post open Plaky tasks summary to QA channel")
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

    @new_bot.tree.command(name="poll", description="Create a poll (staff only)")
    @app_commands.describe(question="The poll question", options="Comma-separated options (e.g., Yes, No, Maybe)")
    async def poll(interaction: discord.Interaction, question: str, options: str) -> None:
        if not interaction.guild or not interaction.user:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Could not verify your permissions.", ephemeral=True)
            return
        if not _is_staff(interaction.user):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return
        option_list = [opt.strip() for opt in options.split(",") if opt.strip()]
        if len(option_list) < 2:
            await interaction.response.send_message("Please provide at least 2 options separated by commas.", ephemeral=True)
            return
        if len(option_list) > 9:
            await interaction.response.send_message("Maximum 9 options allowed.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"📊 {question}",
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Poll created by {interaction.user.display_name}")
        for i, option in enumerate(option_list):
            embed.add_field(name=f"{_poll_option_emoji(i)} {option}", value="\u200b", inline=True)
        channel = interaction.channel
        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This command can only be used in a text channel.", ephemeral=True)
            return
        await interaction.response.send_message("Poll created!", ephemeral=True)
        poll_message = await channel.send(embed=embed)
        for i in range(len(option_list)):
            await poll_message.add_reaction(_poll_option_emoji(i))

    # Set up meeting service
    new_bot.meeting_service = setup_meeting_features(new_bot)
    
    return new_bot


def _extract_github_profile_username(message_content: str) -> str | None:
    content = (message_content or "").strip()
    if not content:
        return None

    if " " not in content:
        candidate = content.lstrip("@").rstrip(".,!?:;)\"'>]")
        if candidate and GITHUB_USERNAME_RE.match(candidate) and candidate.lower() not in GITHUB_RESERVED_PATHS:
            return candidate.lower()

    for match in URL_RE.finditer(message_content):
        raw_url = match.group(0).rstrip(".,!?:;)\"'>]")
        if "github.com/" not in raw_url.lower():
            continue

        parsed = urlparse(raw_url)
        host = parsed.netloc.lower()
        host = host.removeprefix("www.")
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

        return username.lower()

    return None


def _is_announcements_channel(channel: object) -> bool:
    if ANNOUNCEMENTS_CHANNEL_ID is not None:
        return getattr(channel, "id", None) == ANNOUNCEMENTS_CHANNEL_ID
    return getattr(channel, "name", None) == ANNOUNCEMENTS_CHANNEL_NAME


def _is_support_sessions_channel(channel: object) -> bool:
    # Support-tickets and github-profiles are both considered support entry points
    valid_ids = {cid for cid in [SUPPORT_SESSIONS_CHANNEL_ID, GITHUB_PROFILES_CHANNEL_ID] if cid is not None}
    if not valid_ids:
        return False
    channel_id = getattr(channel, "id", None)
    parent_channel_id = getattr(channel, "parent_id", None)
    return channel_id in valid_ids or parent_channel_id in valid_ids


def _validate_hmac_signature(
    raw_body: bytes,
    signature_header: str,
    secret: str,
    expected_prefix: Optional[str] = None,
) -> bool:
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.strip()
    if expected_prefix and provided.startswith(expected_prefix):
        provided = provided[len(expected_prefix) :]

    return hmac.compare_digest(provided, expected)


def _is_valid_plaky_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    return _validate_hmac_signature(raw_body, signature_header, secret, expected_prefix="sha256=")


def _is_valid_announcement_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    if not secret:
        return False
    return _validate_hmac_signature(raw_body, signature_header, secret, expected_prefix="sha256=")


def _load_github_username_map() -> dict:
    try:
        if not GITHUB_USERNAME_MAP_PATH.exists():
            return {}
        raw = GITHUB_USERNAME_MAP_PATH.read_text(encoding="utf-8").strip() or "{}"
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v).lower() for k, v in data.items() if isinstance(v, str)}
        return {}
    except Exception:
        logger.exception("Failed to load GitHub username map from %s", GITHUB_USERNAME_MAP_PATH)
        return {}


def _save_github_username_map(mapping: dict) -> None:
    try:
        GITHUB_USERNAME_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = GITHUB_USERNAME_MAP_PATH.with_suffix(f"{GITHUB_USERNAME_MAP_PATH.suffix}.tmp")
        temporary_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        temporary_path.replace(GITHUB_USERNAME_MAP_PATH)
    except Exception:
        logger.exception("Failed to persist github username map")


def _remember_github_username(discord_id: int, github_username: str) -> None:
    if not discord_id or not github_username:
        return
    mapping = _load_github_username_map()
    mapping[str(discord_id)] = github_username.lower()
    _save_github_username_map(mapping)


def _get_github_username_for_member(member: discord.Member) -> Optional[str]:
    """Return an explicitly mapped username, or a best-effort name-based guess.

    The name fallback is not authoritative. Critical operations should first collect
    an explicit mapping through ``/github-invite-request``.
    """
    mapping = _load_github_username_map()
    gh = mapping.get(str(member.id))
    if gh:
        return gh
    # Fallback: try display name or global name if it looks like a github username
    for candidate in [getattr(member, "global_name", None), getattr(member, "display_name", None), str(member.name) if hasattr(member, "name") else None]:
        if candidate and GITHUB_USERNAME_RE.match(candidate.strip()) and candidate.strip().lower() not in GITHUB_RESERVED_PATHS:
            # Only use if single word
            if " " not in candidate.strip():
                return candidate.strip().lower()
    return None


async def _forward_announcement_to_platform(message: discord.Message) -> None:
    if not PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL:
        return
    if not PLATFORM_ANNOUNCEMENTS_SECRET:
        logger.error("Announcement forward disabled: PLATFORM_ANNOUNCEMENTS_WEBHOOK_SECRET is not configured")
        return
    title = format_discussion_title(message.content)
    body = format_discussion_body(message)
    payload = {
        "source": "discord",
        "discord_message_id": str(message.id),
        "discord_channel_id": str(getattr(message.channel, "id", "")),
        "author": str(message.author),
        "author_id": str(getattr(message.author, "id", "")),
        "title": title,
        "body": body,
        "content": message.content or "",
        "timestamp": message.created_at.isoformat() if hasattr(message, "created_at") else "",
        "jump_url": getattr(message, "jump_url", ""),
    }
    raw = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    sig = hmac.new(PLATFORM_ANNOUNCEMENTS_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    headers["X-Norozo-Signature"] = f"sha256={sig}"
    headers["X-Platform-Signature"] = f"sha256={sig}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL,
                content=raw,
                headers=headers,
            )
            response.raise_for_status()
        logger.info("Forwarded Discord announcement %s to platform", message.id)
    except httpx.HTTPError:
        logger.exception("Failed to forward announcement %s to platform", message.id)


async def _channel_from_id(channel_id: int | None) -> discord.TextChannel | None:
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


def _is_staff(member: discord.Member) -> bool:
    if STAFF_ROLE_ID is None:
        return member.guild_permissions.administrator
    return member.get_role(STAFF_ROLE_ID) is not None or member.guild_permissions.administrator


def _poll_option_emoji(index: int) -> str:
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
    return emojis[index] if index < len(emojis) else str(index + 1)


async def notify_support_team_for_message(message: discord.Message) -> None:
    if SUPPORT_SESSIONS_CHANNEL_ID is None or IT_OPERATIONS_SUPPORT_ROLE_ID is None:
        return

    if not _is_support_sessions_channel(message.channel):
        return

    if not message.guild:
        return

    support_role = message.guild.get_role(IT_OPERATIONS_SUPPORT_ROLE_ID)
    if support_role is None:
        logger.warning(
            "Support notification skipped: role %s not found in guild %s",
            IT_OPERATIONS_SUPPORT_ROLE_ID,
            message.guild.id,
        )
        return

    support_members = [member for member in support_role.members if not member.bot and member.id != message.author.id]
    if not support_members:
        return

    preview = (message.content or "").strip()
    if len(preview) > 300:
        preview = preview[:297].rstrip() + "..."

    message_link = getattr(message, "jump_url", "")
    body_lines = [
        "New message in support sessions.",
        f"From: {message.author}",
        f"Channel: #{getattr(message.channel, 'name', 'support-sessions')}",
    ]
    if preview:
        body_lines.append(f"Message: {preview}")
    if message_link:
        body_lines.append(f"Link: {message_link}")

    dm_text = "\n".join(body_lines)
    send_tasks = [member.send(dm_text) for member in support_members]
    results = await asyncio.gather(*send_tasks, return_exceptions=True)

    failures = sum(1 for result in results if isinstance(result, Exception))
    if failures:
        logger.warning("Support DM sent with %s failures out of %s recipients", failures, len(support_members))



# Event handlers and slash commands are now registered dynamically in _create_and_register_bot()
# to support bot factory pattern for retry and lifecycle management.

# Initialize bot on module load (for backwards compatibility with decorators at module level)
bot = _create_and_register_bot()
meeting_service = bot.meeting_service


async def handle_github_invite_request(interaction: discord.Interaction, github_username: str, team: str | None = None) -> None:
    if not interaction.channel or not _is_support_sessions_channel(interaction.channel):
        await interaction.response.send_message(
            "Please run /github-invite-request in the support tickets channel.",
            ephemeral=True,
        )
        return

    normalized_username = _extract_github_profile_username(github_username)
    if not normalized_username:
        await interaction.response.send_message(
            "Please provide a valid GitHub profile username.",
            ephemeral=True,
        )
        return

    if not GITHUB_ORG or not GITHUB_PAT:
        await interaction.response.send_message(
            "GitHub configuration is missing (GITHUB_ORG or GITHUB_PAT).",
            ephemeral=True,
        )
        return

    # Remember mapping for future role->team sync
    try:
        _remember_github_username(interaction.user.id, normalized_username)
    except Exception:
        logger.exception(
            "Failed to remember GitHub username %s for Discord user %s",
            normalized_username,
            interaction.user.id,
        )

    await interaction.response.defer(ephemeral=True)

    logger.info("Sending GitHub invite for %s to org %s", normalized_username, GITHUB_ORG)
    result = await asyncio.to_thread(
        invite_user,
        username=normalized_username,
        github_org=GITHUB_ORG,
        github_pat=GITHUB_PAT,
    )

    if not result.get("ok"):
        logger.error("GitHub invite failed for %s: %s", normalized_username, result.get("message"))
        await interaction.edit_original_response(
            content=result.get("message", "GitHub invite could not be sent.")
        )
        return

    team_slug = None
    if team:
        normalized_team = team.strip().lower()
        if normalized_team == "support":
            team_slug = GITHUB_SUPPORT_TEAM_SLUG
        elif normalized_team == "it":
            team_slug = GITHUB_IT_TEAM_SLUG

    team_result = None
    if team_slug:
        logger.info("Adding GitHub user %s to team %s", normalized_username, team_slug)
        team_result = add_user_to_team(
            username=normalized_username,
            github_org=GITHUB_ORG,
            github_pat=GITHUB_PAT,
            team_slug=team_slug,
        )
        if not team_result.get("ok"):
            logger.warning("GitHub team assignment failed for %s: %s", normalized_username, team_result.get("message"))

    logger.info("GitHub invite sent successfully for %s", normalized_username)

    org_name = GITHUB_ORG.strip("/").split("/")[-1]
    invite_url = f"https://github.com/orgs/{org_name}/invitation"
    dm_message = (
        f"Your GitHub org invite has been sent!\n\n"
        f"Click here to accept your invite: {invite_url}\n\n"
        f"**Important:** You need to have **Two-Factor Authentication (2FA)** enabled on your GitHub account to join the org. "
        f"You can set that up at https://github.com/settings/security before accepting."
    )
    try:
        await interaction.user.send(dm_message, suppress_embeds=True)
    except discord.Forbidden:
        logger.warning("Could not DM %s — they likely have DMs disabled", interaction.user)

    if STAFF_CHANNEL_ID is not None:
        staff_channel = await _channel_from_id(STAFF_CHANNEL_ID)
        if staff_channel:
            try:
                await staff_channel.send(
                    f"GitHub invite auto-sent for `{normalized_username}` requested by {interaction.user.mention}."
                )
            except discord.DiscordException:
                logger.warning("Could not post GitHub invite log to staff channel %s", STAFF_CHANNEL_ID)

    team_display_name = "team"
    if team_slug:
        team_display_name = "support team" if team_slug == GITHUB_SUPPORT_TEAM_SLUG else "IT team" if team_slug == GITHUB_IT_TEAM_SLUG else "team"

    if team_slug:
        if team_result and team_result.get("ok"):
            await interaction.edit_original_response(
                content=f"Your GitHub invite has been sent and you were added to the {team_display_name}."
            )
        else:
            await interaction.edit_original_response(
                content=f"Your GitHub invite has been sent, but there was an issue adding you to the {team_display_name}: {team_result.get('message', 'Unknown error')}."
            )
        return

    await interaction.edit_original_response(
        content="Your GitHub invite has been sent! Check your DMs for the link."
    )


async def handle_offboard_user(interaction: discord.Interaction, member: discord.Member, github_username: str, *, team: str | None = None) -> None:
    # Permission check: only Staff or Administrator (when user is a real discord.Member)
    if isinstance(interaction.user, discord.Member) and not _is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to offboard users. Staff or Administrator required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    normalized_username = (github_username or "").strip().lower()
    if not normalized_username:
        # Try to resolve from mapping / member
        mapped = _get_github_username_for_member(member) if isinstance(member, discord.Member) else None
        if mapped:
            normalized_username = mapped
        else:
            await interaction.edit_original_response(content="Could not identify the GitHub username to offboard.")
            return

    if member is not None and hasattr(member, "guild") and hasattr(member, "remove_roles"):
        guild = getattr(member, "guild", None)
        if guild is not None and hasattr(guild, "get_role"):
            dev_role = guild.get_role(DEV_TEAM_ROLE_ID) if DEV_TEAM_ROLE_ID else None
            available_role = guild.get_role(AVAILABLE_ROLE_ID) if AVAILABLE_ROLE_ID else None
            roles_to_remove = [role for role in (dev_role, available_role) if role is not None]
            if roles_to_remove:
                try:
                    await member.remove_roles(*roles_to_remove, reason="Offboarding")
                except discord.Forbidden:
                    logger.warning("Could not remove roles from %s during offboarding", member)

    team_slug = None
    if team:
        normalized_team = team.strip().lower()
        if normalized_team == "support":
            team_slug = GITHUB_SUPPORT_TEAM_SLUG
        elif normalized_team == "it":
            team_slug = GITHUB_IT_TEAM_SLUG

    org_result = remove_user_from_org(
        username=normalized_username,
        github_org=GITHUB_ORG,
        github_pat=GITHUB_PAT,
    )
    if not org_result.get("ok"):
        logger.warning("GitHub org removal failed for %s: %s", normalized_username, org_result.get("message"))

    team_result = None
    if team_slug:
        team_result = remove_user_from_team(
            username=normalized_username,
            github_org=GITHUB_ORG,
            github_pat=GITHUB_PAT,
            team_slug=team_slug,
        )
        if not team_result.get("ok"):
            logger.warning("GitHub team removal failed for %s: %s", normalized_username, team_result.get("message"))

    await interaction.edit_original_response(content=f"Offboarding completed for {getattr(member, 'mention', normalized_username)}.")


async def handle_ipca_signed(interaction: discord.Interaction, github_username: str) -> None:
    if STAFF_CHANNEL_ID is None:
        await interaction.response.send_message("STAFF_CHANNEL_ID is not configured.", ephemeral=True)
        return

    if DEV_TEAM_ROLE_ID is None:
        await interaction.response.send_message("DEV_TEAM_ROLE_ID is not configured.", ephemeral=True)
        return

    if AVAILABLE_ROLE_ID is None:
        await interaction.response.send_message("AVAILABLE_ROLE_ID is not configured.", ephemeral=True)
        return

    if not interaction.user:
        await interaction.response.send_message("Could not identify the requesting user.", ephemeral=True)
        return

    # Remember github mapping if provided
    normalized = _extract_github_profile_username(github_username) if github_username else None
    if normalized:
        try:
            _remember_github_username(interaction.user.id, normalized)
        except Exception:
            logger.exception(
                "Failed to remember GitHub username %s for Discord user %s",
                normalized,
                interaction.user.id,
            )

    approval_channel = await _channel_from_id(STAFF_CHANNEL_ID)
    if not approval_channel:
        await interaction.response.send_message("Could not find the configured staff channel.", ephemeral=True)
        return

    view = ApprovalView(dev_team_role_id=DEV_TEAM_ROLE_ID, available_role_id=AVAILABLE_ROLE_ID)
    embed = discord.Embed(
        title="IPCA Approval Request",
        description=(
            f"User {interaction.user.mention} says they signed IPCA. "
            "Click Approve to grant Available and DEV team roles."
        ),
        color=discord.Color.green(),
    )
    await interaction.response.defer(ephemeral=True)

    try:
        await approval_channel.send(embed=embed, view=view)
    except Exception:
        logger.exception("Failed to post IPCA approval request to channel %s", STAFF_CHANNEL_ID)
        await interaction.edit_original_response(
            content="I could not send your approval request to the staff channel."
        )
        return

    await interaction.edit_original_response(content="Your approval request was sent to staff for review.")



# Slash commands are registered in the bot factory _create_and_register_bot()


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
    except (UnicodeDecodeError, json.JSONDecodeError):
        return web.json_response({"ok": False, "message": "Invalid JSON"}, status=400)

    status = str(payload.get("status", "")).strip().lower()
    priority = str(payload.get("priority", "")).strip().lower()

    should_alert = status == "blocked" or priority in {"high", "high priority"}
    if should_alert and QA_CHANNEL_ID:
        channel = await _channel_from_id(QA_CHANNEL_ID)
        if channel:
            title = payload.get("title", "Plaky task")
            task_url = payload.get("url") or payload.get("taskUrl") or ""
            description = f"Status update for **{title}**\nStatus: **{status or 'unknown'}**\nPriority: **{priority or 'unknown'}**"
            if task_url:
                description += f"\n{task_url}"
            await channel.send(f":warning: {description}")

    return web.json_response({"ok": True})


def _announcement_event_key(request: web.Request, payload: dict, raw_body: bytes) -> str:
    explicit_key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    if explicit_key:
        return f"header:{explicit_key.strip()}"

    for field in ("event_id", "eventId", "announcement_id", "announcementId", "id"):
        value = payload.get(field)
        if value is not None and str(value).strip():
            return f"payload:{field}:{str(value).strip()}"

    return f"body:{hashlib.sha256(raw_body).hexdigest()}"


def _load_announcement_events(now: float) -> dict[str, float]:
    try:
        if not ANNOUNCEMENT_DEDUP_PATH.exists():
            return {}
        data = json.loads(ANNOUNCEMENT_DEDUP_PATH.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            return {}
        cutoff = now - ANNOUNCEMENT_DEDUP_TTL_SECONDS
        return {
            str(key): float(timestamp)
            for key, timestamp in data.items()
            if isinstance(timestamp, (int, float)) and float(timestamp) >= cutoff
        }
    except Exception:
        logger.exception("Failed to load announcement idempotency state from %s", ANNOUNCEMENT_DEDUP_PATH)
        return {}


def _save_announcement_events(events: dict[str, float]) -> None:
    ANNOUNCEMENT_DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    newest_events = dict(sorted(events.items(), key=lambda item: item[1], reverse=True)[:ANNOUNCEMENT_DEDUP_MAX_EVENTS])
    temporary_path = ANNOUNCEMENT_DEDUP_PATH.with_suffix(f"{ANNOUNCEMENT_DEDUP_PATH.suffix}.tmp")
    temporary_path.write_text(json.dumps(newest_events, indent=2), encoding="utf-8")
    temporary_path.replace(ANNOUNCEMENT_DEDUP_PATH)


async def _reserve_announcement_event(event_key: str) -> bool:
    async with _announcement_dedup_lock:
        now = time.time()
        events = _load_announcement_events(now)
        if event_key in events:
            return False
        events[event_key] = now
        _save_announcement_events(events)
        return True


async def _release_announcement_event(event_key: str) -> None:
    async with _announcement_dedup_lock:
        events = _load_announcement_events(time.time())
        if events.pop(event_key, None) is not None:
            _save_announcement_events(events)


async def platform_announcement_handler(request: web.Request) -> web.Response:
    """Inbound webhook for platform.deepiri.com -> Discord announcements.
    Expects JSON with {title, body, content, author} and optional signature header.
    """
    raw_body = await request.read()

    if not ANNOUNCEMENTS_INBOUND_SECRET:
        logger.error("Platform announcement webhook disabled: ANNOUNCEMENTS_INBOUND_SECRET is not configured")
        return web.json_response({"ok": False, "message": "Webhook authentication is not configured"}, status=503)

    sig_header = (
        request.headers.get("X-Norozo-Signature")
        or request.headers.get("X-Platform-Signature")
        or request.headers.get("X-Signature")
        or request.headers.get("X-Webhook-Signature")
        or ""
    )
    if not sig_header or not _is_valid_announcement_signature(raw_body, sig_header, ANNOUNCEMENTS_INBOUND_SECRET):
        return web.json_response({"ok": False, "message": "Missing or invalid signature"}, status=401)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return web.json_response({"ok": False, "message": "Invalid JSON"}, status=400)

    title = str(payload.get("title") or payload.get("announcement_title") or "").strip()
    body = str(payload.get("body") or payload.get("announcement_body") or payload.get("content") or "").strip()
    author = str(payload.get("author") or payload.get("created_by") or "Platform").strip()
    url = str(payload.get("url") or payload.get("link") or "").strip()

    if not body and not title:
        return web.json_response({"ok": False, "message": "Missing title/body"}, status=400)

    if ANNOUNCEMENTS_CHANNEL_ID is None:
        return web.json_response({"ok": False, "message": "ANNOUNCEMENTS_CHANNEL_ID not configured"}, status=500)

    channel = await _channel_from_id(ANNOUNCEMENTS_CHANNEL_ID)
    if not channel:
        return web.json_response({"ok": False, "message": "Announcements channel not found"}, status=500)

    event_key = _announcement_event_key(request, payload, raw_body)
    try:
        reserved = await _reserve_announcement_event(event_key)
    except OSError:
        logger.exception("Failed to persist announcement idempotency key %s", event_key)
        return web.json_response({"ok": False, "message": "Could not persist webhook state"}, status=503)
    if not reserved:
        logger.info("Ignoring duplicate platform announcement %s", event_key)
        return web.json_response({"ok": True, "duplicate": True})

    # Build embed for platform announcement
    embed = discord.Embed(
        title=title or "Platform Announcement",
        description=body[:4000] if body else "New announcement from platform.deepiri.com",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"From platform.deepiri.com • {author}")
    if url:
        embed.add_field(name="Link", value=url, inline=False)

    content = body if body else title
    # Prevent loop: mark source as platform, but discord forward will only forward discord->platform, not platform->platform
    try:
        await channel.send(content=content[:1900] if content else None, embed=embed)
    except Exception:
        try:
            await _release_announcement_event(event_key)
        except OSError:
            logger.exception("Failed to release announcement idempotency key %s", event_key)
        logger.exception("Failed to post platform announcement to Discord")
        return web.json_response({"ok": False, "message": "Failed to post to Discord"}, status=500)

    return web.json_response({"ok": True})


async def health_handler(_: web.Request) -> web.Response:
    announcement_webhook_ready = bool(ANNOUNCEMENTS_INBOUND_SECRET)
    return web.json_response(
        {
            "ok": True,
            "service": "deepiri-discord-bot",
            "announcement_webhook_ready": announcement_webhook_ready,
        }
    )


async def start_webhook_server() -> None:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/plaky/webhook", plaky_webhook_handler)
    app.router.add_post("/announcements/webhook", platform_announcement_handler)
    app.router.add_post("/platform/announcements", platform_announcement_handler)
    app.router.add_post("/webhooks/platform-announcements", platform_announcement_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host=WEBHOOK_HOST, port=WEBHOOK_PORT)
    await site.start()

    if bot:
        bot.webhook_runner = runner
    print(f"Plaky webhook server listening on http://{WEBHOOK_HOST}:{WEBHOOK_PORT}/plaky/webhook")
    print(f"Announcements webhook listening on http://{WEBHOOK_HOST}:{WEBHOOK_PORT}/announcements/webhook")


def _is_discord_rate_limit_error(error: Exception) -> bool:
    """Check if an error is a Discord 429 or Cloudflare 1015 rate limit error."""
    error_str = str(error).lower()
    # Check for common rate limit indicators
    rate_limit_indicators = ["429", "1015", "too many requests", "cloudflare", "rate limit"]
    return any(indicator in error_str for indicator in rate_limit_indicators)


def _extract_retry_after(error: Exception) -> int | None:
    """
    Extract Retry-After header value (in seconds) from error.
    
    If available, respects the server's rate-limit retry instruction.
    """
    # Check for ClientError with response info
    error_str = str(error)
    # Look for Retry-After value in error message
    if "Retry-After" in error_str:
        try:
            # Simple extraction; may need refinement based on actual error format
            parts = error_str.split("Retry-After")
            if len(parts) > 1:
                value_str = parts[1].split()[0].strip("=:,[]")
                return int(value_str)
        except (ValueError, IndexError):
            pass
    return None


async def _connect_discord_with_retry(token: str, max_backoff: int = 300) -> None:
    """
    Connect to Discord with indefinite exponential backoff retry on 429/1015 rate limits.
    
    This function NEVER gives up:
    - Retries indefinitely with exponential backoff capped at max_backoff seconds
    - Respects Retry-After header when available
    - Creates a fresh bot instance on each retry via factory
    - Keeps the webhook server and process alive during retries
    - Does NOT re-raise rate-limit errors
    
    Args:
        token: Discord bot token
        max_backoff: Maximum backoff delay in seconds (default 300 / 5 minutes)
    """
    global bot

    attempt = 0
    consecutive_failures = 0  # Track consecutive failures for backoff calculation
    
    while True:  # INDEFINITE RETRY LOOP
        attempt += 1
        consecutive_failures += 1
        
        # Create a fresh bot instance for this attempt
        bot = _create_and_register_bot()
        
        try:
            logger.info(f"Discord startup attempt {attempt} (consecutive failures: {consecutive_failures})")
            await bot.start(token)
            # If we reach here, bot connected successfully; reset failure counter and exit
            logger.info("Discord bot started successfully!")
            return
        except asyncio.CancelledError:
            # Handle graceful shutdown
            logger.info("Discord startup cancelled")
            if bot and not bot.is_closed():
                await bot.close()
            raise
        except Exception as err:
            logger.exception("Discord startup failed (attempt %s)", attempt)

            # Close the failed bot instance to release resources
            if bot and not bot.is_closed():
                try:
                    await bot.close()
                except discord.DiscordException as close_err:
                    logger.warning("Error closing failed bot instance: %s", close_err)
            
            # Check for rate-limit errors and Retry-After header
            is_rate_limit = _is_discord_rate_limit_error(err)
            retry_after = _extract_retry_after(err)
            
            if is_rate_limit:
                logger.warning(
                    f"Discord rate limit (429/1015) detected on attempt {attempt}. "
                    f"This typically indicates IP-level rate limiting or Cloudflare blocking."
                )
            
            # Determine wait time
            if retry_after:
                # Respect server's Retry-After instruction
                wait_seconds = retry_after
                logger.info(f"Respecting Retry-After header: {wait_seconds} seconds")
            else:
                # Use exponential backoff with jitter
                # 2^(consecutive_failures-1) capped at max_backoff
                base_backoff = min(2 ** (consecutive_failures - 1), max_backoff)
                jitter = random.uniform(0, base_backoff * 0.1)  # Up to 10% jitter
                wait_seconds = base_backoff + jitter
            
            logger.info(
                f"Retrying Discord startup in {wait_seconds:.1f} seconds... "
                f"(backoff strategy: exponential with max {max_backoff}s cap)"
            )
            await asyncio.sleep(wait_seconds)


async def main() -> None:
    await start_webhook_server()
    await _connect_discord_with_retry(DISCORD_TOKEN)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is required in .env")

    asyncio.run(main())
