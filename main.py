import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import discord
import httpx
from aiohttp import BasicAuth, web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bot import format_discussion_body, format_discussion_title
from emailer import send_email
from github import add_user_to_team, get_user_email, invite_user, is_org_member, remove_user_from_org, remove_user_from_team
from github_discussion import GitHubDiscussionError, create_github_discussion
from meetings import setup_meeting_features
from onboarding import ApprovalView
from plaky import create_task, find_user_email_by_name, get_tasks
from state_store import load_last_online_at, save_last_online_at


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
DISCORD_PROXY_URL = (os.getenv("DISCORD_PROXY_URL") or "").strip() or None


def _int_env(name: str) -> Optional[int]:
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

# Channels where staff can say "kick out <name>" to remove someone from both
# Discord and the GitHub org in one shot. Env-overridable, defaulting to the IDs
# actually in use so this works without extra Render config.
ADMIN_TERMINAL_CHANNEL_ID = _int_env("ADMIN_TERMINAL_CHANNEL_ID") or 1437210346975924347  # #admin-terminal
IT_KICK_LIST_CHANNEL_ID = _int_env("IT_KICK_LIST_CHANNEL_ID") or 1494803547957760000  # #it-kick-list
KICK_OUT_COMMAND_CHANNEL_IDS = {
    cid for cid in (SUPPORT_SESSIONS_CHANNEL_ID, ADMIN_TERMINAL_CHANNEL_ID, IT_KICK_LIST_CHANNEL_ID) if cid is not None
}
KICK_OUT_COMMAND_RE = re.compile(r"^\s*kick\s*(?:out)?\s+(.+)$", re.IGNORECASE)

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


def _discord_proxy_kwargs() -> dict:
    """Route Discord traffic (REST + gateway) through DISCORD_PROXY_URL if set.

    Render's shared egress IP can pick up a Cloudflare 1015 ban from
    unrelated tenants; routing through deepiri-proxy sidesteps that since
    it isn't fixable from retry/session logic alone. DISCORD_PROXY_URL must
    be an http:// proxy URL (e.g. http://user:pass@vps-ip:8888) — aiohttp's
    proxy=/proxy_auth= support (what discord.py passes this straight into)
    is HTTP-only, not SOCKS5.
    """
    if not DISCORD_PROXY_URL:
        return {}
    parsed = urlparse(DISCORD_PROXY_URL)
    kwargs: dict = {"proxy": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username and parsed.password:
        kwargs["proxy_auth"] = BasicAuth(parsed.username, parsed.password)
    return kwargs


class DeepiriBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.guilds = True

        super().__init__(command_prefix="!", intents=intents, **_discord_proxy_kwargs())
        self.webhook_runner: Optional[web.AppRunner] = None

    async def setup_hook(self) -> None:
        if DEV_TEAM_ROLE_ID is not None and AVAILABLE_ROLE_ID is not None:
            self.add_view(ApprovalView(dev_team_role_id=DEV_TEAM_ROLE_ID, available_role_id=AVAILABLE_ROLE_ID))
        await self.tree.sync()


bot = DeepiriBot()
meeting_service = setup_meeting_features(bot)


def _extract_github_profile_username(message_content: str) -> Optional[str]:
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


def _is_ipca_sign_message(content: str) -> bool:
    text = (content or "").lower()
    if "ipca" not in text:
        return False
    if re.search(r"\bsigned\b", text) or re.search(r"\bsign\b", text):
        return True
    return False


async def _maybe_auto_assign_ipca_roles(message: discord.Message) -> bool:
    """Assign AVAILABLE_ROLE_ID + DEV_TEAM_ROLE_ID if this message signals IPCA
    signed. Shared by the live on_message handler and the startup catch-up sweep
    (_sweep_open_support_threads_for_ipca) so a bot-downtime window doesn't
    silently skip role assignment. Returns True if roles were newly assigned.
    """
    if not (
        _is_support_sessions_channel(message.channel)
        and _is_ipca_sign_message(message.content or "")
        and isinstance(message.author, discord.Member)
    ):
        return False
    if DEV_TEAM_ROLE_ID is None or AVAILABLE_ROLE_ID is None or not message.guild:
        return False
    dev_role = message.guild.get_role(DEV_TEAM_ROLE_ID)
    available_role = message.guild.get_role(AVAILABLE_ROLE_ID)
    if not (dev_role and available_role):
        return False
    if message.author.get_role(DEV_TEAM_ROLE_ID) and message.author.get_role(AVAILABLE_ROLE_ID):
        try:
            target_channel = message.thread or message.channel
            await target_channel.send(f"{message.author.mention} You already have access.")
        except Exception:
            logger.exception("Failed to post IPCA already-has-access reply for %s", message.author.id)
        return False
    try:
        await message.author.add_roles(available_role, dev_role, reason="IPCA signed auto-assign")
    except Exception:
        logger.exception("Failed to auto-assign IPCA roles to %s", message.author.id)
        return False
    try:
        await message.add_reaction("✅")
    except Exception:
        pass
    try:
        # support-tickets uses Discord's auto-thread feature: the triggering
        # message lives in the parent channel and spawns a same-id companion
        # thread. message.reply() posts back into the parent channel, not the
        # thread — so post into message.thread when this message started one.
        target_channel = message.thread or message.channel
        await target_channel.send(f"{message.author.mention} We gave you access to the rest of the Discord.")
    except Exception:
        logger.exception("Failed to post IPCA access confirmation reply for %s", message.author.id)

    ticket_thread = message.thread if message.thread else (message.channel if isinstance(message.channel, discord.Thread) else None)
    if ticket_thread is not None:
        try:
            await ticket_thread.edit(archived=True, locked=False, reason="IPCA signed — ticket resolved")
        except Exception:
            logger.exception("Failed to archive IPCA ticket thread %s", getattr(ticket_thread, "id", "?"))

    return True


DEFAULT_CATCHUP_LOOKBACK_HOURS = 72


async def _sweep_open_support_threads_for_ipca(target_bot: "DeepiriBot") -> None:
    """Catch up on IPCA-signed messages posted while the bot was offline (e.g.
    during a Cloudflare 1015 egress ban) — on_message only fires for live events,
    so a downtime window would otherwise silently skip auto role assignment.
    Runs once per successful login, scanning currently-open threads only.
    """
    if SUPPORT_SESSIONS_CHANNEL_ID is None:
        return
    channel = target_bot.get_channel(SUPPORT_SESSIONS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await target_bot.fetch_channel(SUPPORT_SESSIONS_CHANNEL_ID)
        except Exception:
            logger.exception("IPCA sweep: could not resolve support-sessions channel")
            return
    threads = list(getattr(channel, "threads", []) or [])
    assigned = 0
    for thread in threads:
        try:
            async for msg in thread.history(limit=200, oldest_first=True):
                if await _maybe_auto_assign_ipca_roles(msg):
                    assigned += 1
        except Exception:
            logger.exception("IPCA sweep: failed scanning thread %s", getattr(thread, "id", "?"))
    if assigned:
        logger.info("IPCA sweep: assigned roles to %s member(s) from %s open thread(s)", assigned, len(threads))


async def _sweep_archived_support_threads_for_ipca(target_bot: "DeepiriBot", since) -> None:
    """Companion to _sweep_open_support_threads_for_ipca: a ticket thread that gets
    archived (staff marks it 'Handled') *while the bot is offline* is invisible to
    the open-thread sweep, so an IPCA-signed message sitting in it would silently
    never grant roles (this is exactly what happened to genericpro's ticket during
    the 2026-08-31 downtime — archived at 02:49, bot didn't wake until 04:46).
    Scans threads archived since the last-known-online checkpoint.
    """
    if SUPPORT_SESSIONS_CHANNEL_ID is None:
        return
    channel = target_bot.get_channel(SUPPORT_SESSIONS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await target_bot.fetch_channel(SUPPORT_SESSIONS_CHANNEL_ID)
        except Exception:
            logger.exception("IPCA archived-sweep: could not resolve support-sessions channel")
            return
    assigned = 0
    scanned = 0
    try:
        async for thread in channel.archived_threads(limit=100):
            archived_at = getattr(thread, "archive_timestamp", None)
            if archived_at is not None and archived_at < since:
                # archived_threads() is newest-first, so once we're past `since` nothing older matters
                break
            scanned += 1
            try:
                async for msg in thread.history(limit=200, oldest_first=True):
                    if await _maybe_auto_assign_ipca_roles(msg):
                        assigned += 1
            except Exception:
                logger.exception("IPCA archived-sweep: failed scanning thread %s", getattr(thread, "id", "?"))
    except Exception:
        logger.exception("IPCA archived-sweep: failed listing archived threads")
        return
    if assigned or scanned:
        logger.info(
            "IPCA archived-sweep: assigned roles to %s member(s) from %s archived thread(s) since %s",
            assigned, scanned, since,
        )


async def _catch_up_since_last_online(target_bot: "DeepiriBot") -> None:
    """Runs once per successful login (before the heartbeat starts writing a fresh
    checkpoint). Reads the persisted last-online checkpoint (survives Render
    restarts — the disk itself doesn't) and replays anything missed, open or
    archived, instead of relying on 'currently open' as a proxy for 'not yet
    handled'. Falls back to a fixed lookback window if no checkpoint exists yet.
    """
    since = await load_last_online_at()
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=DEFAULT_CATCHUP_LOOKBACK_HOURS)
        logger.info("No last-online checkpoint found; defaulting catch-up lookback to %sh", DEFAULT_CATCHUP_LOOKBACK_HOURS)
    else:
        logger.info("Catching up on missed activity since %s", since)

    await _sweep_open_support_threads_for_ipca(target_bot)
    await _sweep_archived_support_threads_for_ipca(target_bot, since)

    await save_last_online_at()


async def _heartbeat_last_online(interval_seconds: int = 300) -> None:
    """Keeps the checkpoint fresh while alive, so a hard crash (no graceful
    shutdown) still only loses a few minutes of catch-up window on next boot,
    instead of however long since the last successful login."""
    while True:
        await asyncio.sleep(interval_seconds)
        await save_last_online_at()


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


async def _find_github_username_in_profiles_channel(member: discord.Member) -> Optional[str]:
    """Fallback when there's no explicit mapping and the name-guess heuristic fails:
    scan #github-profiles for a message *authored by this exact member* containing
    their GitHub profile link (that's what the channel is for — no fuzzy name
    matching needed, just match by author.id), then verify the extracted username
    is an actual member of GITHUB_ORG before trusting it — a stale/wrong link
    shouldn't silently pass through into a destructive op like org removal.
    """
    if GITHUB_PROFILES_CHANNEL_ID is None:
        return None
    channel = await _channel_from_id(GITHUB_PROFILES_CHANNEL_ID)
    if channel is None:
        return None
    try:
        async for msg in channel.history(limit=1000):
            if msg.author.id != member.id:
                continue
            candidate = _extract_github_profile_username(msg.content or "")
            if not candidate:
                continue
            if not await asyncio.to_thread(is_org_member, candidate, GITHUB_ORG, GITHUB_PAT):
                logger.warning("Found GitHub link %s for %s in #github-profiles but they're not in %s", candidate, member.id, GITHUB_ORG)
                continue
            _remember_github_username(member.id, candidate)
            return candidate
    except Exception:
        logger.exception("Failed scanning #github-profiles for member %s", member.id)
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


def _is_staff(member: discord.Member) -> bool:
    if STAFF_ROLE_ID is None:
        return member.guild_permissions.administrator
    return member.get_role(STAFF_ROLE_ID) is not None or member.guild_permissions.administrator


def _can_dispatch_ipca_signed(member: discord.Member) -> bool:
    """/ipca-signed grants DEV Team + Available roles on approval — restrict who can
    even open that approval request to admins and Security & Operations Support, so a
    member can't just self-serve the escalation path (the normal path is the automatic
    IPCA-message detection in support tickets, not this command)."""
    if _is_staff(member):
        return True
    return IT_OPERATIONS_SUPPORT_ROLE_ID is not None and member.get_role(IT_OPERATIONS_SUPPORT_ROLE_ID) is not None


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


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (id={bot.user.id if bot.user else 'unknown'})")
    meeting_service.start_loop()


@bot.event
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


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    # Auto-sync GitHub team membership when Discord roles are granted
    if not GITHUB_ORG or not GITHUB_PAT:
        return
    before_roles = {r.id for r in before.roles}
    after_roles = {r.id for r in after.roles}
    added = after_roles - before_roles
    if not added:
        return

    github_username = _get_github_username_for_member(after)
    # If no mapping, we cannot sync; log and skip but still try display_name fallback
    if not github_username:
        # Only attempt if we can infer username; otherwise skip with log
        logger.info("Member %s gained roles %s but no GitHub username mapping found, skipping team sync", after.id, added)
        return

    # Build name fallback maps for when role IDs not configured
    added_roles = [r for r in after.roles if r.id in added]
    added_names_lower = {r.name.strip().lower() for r in added_roles}

    qa_triggered = False
    if QA_ROLE_ID is not None and QA_ROLE_ID in added:
        qa_triggered = True
    elif QA_ROLE_ID is None and ("qa" in added_names_lower or "quality assurance" in added_names_lower):
        qa_triggered = True

    it_triggered = False
    if IT_OPERATIONS_SUPPORT_ROLE_ID is not None and IT_OPERATIONS_SUPPORT_ROLE_ID in added:
        it_triggered = True
    elif IT_OPERATIONS_SUPPORT_ROLE_ID is None:
        # Fallback by name: check for pink IT role variants
        it_candidates = {"it operations support", "support operations", "it", "it-management", "security it", "it operations", "support operations and security it"}
        if added_names_lower & it_candidates:
            it_triggered = True

    # QA -> support-team
    if qa_triggered:
        logger.info("Syncing %s (%s) to GitHub team %s for QA role", after, github_username, GITHUB_SUPPORT_TEAM_SLUG)
        try:
            result = await asyncio.to_thread(
                add_user_to_team,
                username=github_username,
                github_org=GITHUB_ORG,
                github_pat=GITHUB_PAT,
                team_slug=GITHUB_SUPPORT_TEAM_SLUG,
            )
            if not result.get("ok"):
                logger.warning("Failed to add %s to support team: %s", github_username, result.get("message"))
        except Exception:
            logger.exception("Exception syncing QA to GitHub team")

    # IT Operations -> it-management-team
    if it_triggered:
        logger.info("Syncing %s (%s) to GitHub team %s for IT role", after, github_username, GITHUB_IT_TEAM_SLUG)
        try:
            result = await asyncio.to_thread(
                add_user_to_team,
                username=github_username,
                github_org=GITHUB_ORG,
                github_pat=GITHUB_PAT,
                team_slug=GITHUB_IT_TEAM_SLUG,
            )
            if not result.get("ok"):
                logger.warning("Failed to add %s to IT team: %s", github_username, result.get("message"))
        except Exception:
            logger.exception("Exception syncing IT to GitHub team")


@bot.event
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
        # Forward to platform.deepiri.com (bidirectional bridge)
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
        elif pr_match and not plaky_match:
            await message.channel.send(
                f"{message.author.mention} please include the Plaky task URL (app.plaky.com/...) with your PR link."
            )

    await bot.process_commands(message)


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
            except Exception:
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


async def handle_offboard_user(interaction: discord.Interaction, member: discord.Member, github_username: str, *, team: Optional[str] = None) -> None:
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


async def handle_discord_kick(interaction: discord.Interaction, member: discord.Member, reason: str | None = None) -> None:
    if not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
        await interaction.response.send_message("You do not have permission to kick members. Staff or Administrator required.", ephemeral=True)
        return

    if not isinstance(member, discord.Member):
        await interaction.response.send_message("Could not resolve that member.", ephemeral=True)
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message("You cannot kick yourself.", ephemeral=True)
        return

    if member.guild_permissions.administrator or (STAFF_ROLE_ID is not None and member.get_role(STAFF_ROLE_ID) is not None):
        await interaction.response.send_message("Cannot kick an Admin/staff member via this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    kick_reason = (reason or f"Kicked by {interaction.user} via /discord-kick").strip()[:512]
    try:
        await member.kick(reason=kick_reason)
    except discord.Forbidden:
        await interaction.edit_original_response(content="I don't have permission to kick that member (check my role position).")
        return
    except Exception:
        logger.exception("Failed to kick member %s", member.id)
        await interaction.edit_original_response(content=f"Failed to kick {member.mention}.")
        return

    await interaction.edit_original_response(content=f"Kicked {member.mention} from the server.")
    if STAFF_CHANNEL_ID:
        log_channel = await _channel_from_id(STAFF_CHANNEL_ID)
        if log_channel:
            try:
                await log_channel.send(f"{member} ({member.id}) was kicked by {interaction.user.mention}. Reason: {kick_reason}")
            except Exception:
                pass


def _resolve_kick_target(message: discord.Message, raw_target: str) -> Optional[discord.Member]:
    if message.mentions:
        return message.mentions[0]
    guild = message.guild
    if guild is None:
        return None
    needle = raw_target.strip().strip("@").strip('"').strip("'").lower()
    if not needle:
        return None
    for candidate in guild.members:
        names = {
            str(getattr(candidate, "name", "") or "").lower(),
            str(getattr(candidate, "display_name", "") or "").lower(),
            str(getattr(candidate, "global_name", "") or "").lower(),
        }
        if needle in names:
            return candidate
    # Fall back to a substring match if no exact name matched
    for candidate in guild.members:
        names = " ".join(
            str(getattr(candidate, attr, "") or "").lower()
            for attr in ("name", "display_name", "global_name")
        )
        if needle in names:
            return candidate
    return None


def _termination_notice_text(display_name: str) -> str:
    return (
        f"Dear {display_name},\n\n"
        "This email serves as formal notice that your participation in the Deepiri project "
        "is terminated effective immediately pursuant to Section 15 of the Deepiri Contributor "
        "and Intellectual Property Agreement.\n\n"
        "As a result of this termination, the following terms apply:\n"
        "1. Cessation of Representation\n\n"
        "Effective immediately, you may not represent yourself as:\n\n"
        "    A current contributor to Deepiri\n\n"
        "    Acting on behalf of Deepiri\n\n"
        "    Affiliated with Deepiri in any ongoing capacity\n\n"
        "You may update LinkedIn, résumés, and other public profiles to reflect that your "
        "participation has ended. Any description of your past involvement must be accurate "
        "and must not imply ongoing affiliation, authority, or endorsement.\n"
        "2. Access and Assets\n\n"
        "You must immediately cease access to all Deepiri systems, repositories, accounts, "
        "credentials, or internal tools.\n\n"
        "If you possess any materials that were explicitly designated as private and not "
        "publicly released under Apache 2.0, those materials must be deleted or returned in "
        "accordance with Sections 11 and 12 of the Agreement.\n\n"
        "This requirement does not apply to publicly released open-source repositories "
        "governed by Apache 2.0.\n"
        "3. Continuing Obligations\n\n"
        "All confidentiality provisions remain in effect with respect to any non-public "
        "materials previously accessed.\n\n"
        "If you have questions regarding this notice, please submit them in writing.\n\n"
        "Sincerely,\n"
        "Deepiri Management"
    )


async def _send_termination_notice(target: discord.Member, github_username: Optional[str]) -> str:
    """Resolve an email for the kicked member (GitHub public email -> best-effort
    Plaky lookup -> Discord DM as last resort) and send the termination notice.
    Returns a short human-readable outcome string for the kick-out summary.
    """
    body = _termination_notice_text(target.display_name)
    subject = "Notice of Termination — Deepiri Contributor Agreement"

    email = None
    if github_username:
        email = await asyncio.to_thread(get_user_email, github_username, GITHUB_PAT)
    if not email and PLAKY_API_KEY:
        for candidate_name in (target.display_name, str(getattr(target, "global_name", "") or ""), str(target.name)):
            email = await asyncio.to_thread(find_user_email_by_name, candidate_name, PLAKY_API_KEY)
            if email:
                break

    if email:
        sent = await asyncio.to_thread(send_email, email, subject, body)
        if sent:
            return f"emailed to {email}"
        logger.warning("Termination email to %s failed to send; falling back to DM for %s", email, target.id)

    try:
        await target.send(f"**{subject}**\n\n{body}")
        return "no email found — sent via Discord DM" if not email else "email send failed — sent via Discord DM instead"
    except Exception:
        logger.exception("Failed to DM termination notice to %s", target.id)
        return "could not deliver notice via email or DM"


async def _maybe_handle_kick_out_command(message: discord.Message) -> bool:
    """Staff saying 'kick out <name>' (or 'kick <name>') in #support-tickets,
    #admin-terminal, or #it-kick-list removes the member from Discord AND the
    GitHub org in one shot, instead of needing /discord-kick + /offboard-user
    separately. Returns True if this message was handled as a kick command."""
    if message.guild is None or message.channel.id not in KICK_OUT_COMMAND_CHANNEL_IDS:
        return False
    match = KICK_OUT_COMMAND_RE.match(message.content or "")
    if not match:
        return False
    if not isinstance(message.author, discord.Member) or not _is_staff(message.author):
        return False

    target = _resolve_kick_target(message, match.group(1))
    if target is None:
        await message.channel.send(f"{message.author.mention} Couldn't find that member to kick.")
        return True
    if target.id == message.author.id:
        await message.channel.send(f"{message.author.mention} You cannot kick yourself.")
        return True
    if target.guild_permissions.administrator or (STAFF_ROLE_ID is not None and target.get_role(STAFF_ROLE_ID) is not None):
        await message.channel.send(f"{message.author.mention} Cannot kick an Admin/staff member this way.")
        return True

    reason = f"Kicked by {message.author} via kick-out command in #{getattr(message.channel, 'name', message.channel.id)}"[:512]

    # Resolve GitHub username and send the termination notice BEFORE kicking —
    # once someone's kicked, the bot can no longer DM them (no mutual server
    # context), so the DM fallback would always fail if this happened after.
    github_username = _get_github_username_for_member(target)
    if github_username and not await asyncio.to_thread(is_org_member, github_username, GITHUB_ORG, GITHUB_PAT):
        # The mapping/name-guess isn't actually in the org roster — don't trust it for
        # a destructive op, fall through to searching #github-profiles instead.
        logger.warning("Mapped/guessed GitHub username %s for %s is not in %s; falling back to #github-profiles", github_username, target.id, GITHUB_ORG)
        github_username = None
    if not github_username:
        github_username = await _find_github_username_in_profiles_channel(target)

    notice_outcome = await _send_termination_notice(target, github_username)

    discord_ok = True
    try:
        await target.kick(reason=reason)
    except discord.Forbidden:
        discord_ok = False
        await message.channel.send(f"{message.author.mention} I don't have permission to kick {target.mention} (check my role position).")
    except Exception:
        discord_ok = False
        logger.exception("Failed to kick member %s via kick-out command", target.id)
        await message.channel.send(f"{message.author.mention} Failed to kick {target.mention} from Discord.")

    github_ok = False
    github_note = "no mapped GitHub username, skipped"
    if github_username:
        org_result = remove_user_from_org(username=github_username, github_org=GITHUB_ORG, github_pat=GITHUB_PAT)
        github_ok = bool(org_result.get("ok"))
        github_note = github_username if github_ok else f"{github_username} — {org_result.get('message')}"
        if not github_ok:
            logger.warning("GitHub org removal failed for %s during kick-out: %s", github_username, org_result.get("message"))

    summary = (
        f"{'✅' if discord_ok else '⚠️'} Discord kick: {target} ({target.id})\n"
        f"{'✅' if github_ok else '⚠️'} GitHub org removal: {github_note}\n"
        f"📧 Termination notice: {notice_outcome}"
    )
    await message.channel.send(summary)
    if STAFF_CHANNEL_ID:
        log_channel = await _channel_from_id(STAFF_CHANNEL_ID)
        if log_channel:
            try:
                await log_channel.send(f"{message.author.mention} kicked {target} ({target.id}) via kick-out command.\n{summary}")
            except Exception:
                pass
    return True


async def handle_ipca_signed(interaction: discord.Interaction, github_username: str) -> None:
    if not isinstance(interaction.user, discord.Member) or not _can_dispatch_ipca_signed(interaction.user):
        await interaction.response.send_message(
            "Only Admins or Security & Operations Support can run this command. "
            "Roles are normally granted automatically when you sign the IPCA in your support ticket.",
            ephemeral=True,
        )
        return

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


def _register_slash_commands(target_bot: DeepiriBot) -> None:
    @target_bot.tree.command(name="github-invite-request", description="Request a GitHub invite after signing ICPA")
    @app_commands.describe(github_username="Your GitHub profile username", team="Optional team to add the user to (support or it)")
    @app_commands.choices(
        team=[
            app_commands.Choice(name="support", value="support"),
            app_commands.Choice(name="it", value="it"),
        ]
    )
    async def github_invite_request(interaction: discord.Interaction, github_username: str, team: app_commands.Choice[str] | None = None) -> None:
        await handle_github_invite_request(interaction, github_username, team=team.value if team else None)


    @target_bot.tree.command(name="ipca-signed", description="Request DEV team and Available roles after signing ICPA")
    @app_commands.describe(github_username="Your GitHub profile username")
    async def ipca_signed(interaction: discord.Interaction, github_username: str) -> None:
        await handle_ipca_signed(interaction, github_username)


    @target_bot.tree.command(name="offboard-user", description="Offboard a user from Discord roles and GitHub membership")
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

    @target_bot.tree.command(name="discord-kick", description="Kick a member from the Discord server (staff only)")
    @app_commands.describe(member="The Discord member to kick", reason="Optional reason")
    async def discord_kick(interaction: discord.Interaction, member: discord.Member, reason: str | None = None) -> None:
        await handle_discord_kick(interaction, member, reason)


    @target_bot.tree.command(name="plaky-request", description="Create a Plaky task")
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


    @target_bot.tree.command(name="plaky-status", description="Post open Plaky tasks summary to QA channel")
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

    @target_bot.tree.command(name="poll", description="Create a poll (staff only)")
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


_ALERT_SEVERITY_COLORS = {
    "critical": discord.Color.dark_red(),
    "error": discord.Color.red(),
    "warning": discord.Color.orange(),
    "info": discord.Color.blue(),
}

# Fallback "how to handle this" guidance when the sender doesn't provide its own
# `steps`/`runbook` — so #it-notifications alerts are never just a bare "something
# broke" with no next action.
_DEFAULT_ALERT_STEPS = {
    "critical": (
        "1. You were DMed for this one — acknowledge in #it-notifications so others know it's being worked.\n"
        "2. Check the service on the VM: `docker ps` / `docker logs <container>` for the named service.\n"
        "3. If it's Postgres/Redis, check `docker logs deepiri-postgres-platform` / `deepiri-redis` first — most other services depend on them.\n"
        "4. If the container is down, `docker compose up -d --no-deps <service>`; if it's crash-looping, check recent deploys/config changes.\n"
        "5. Once resolved, confirm the 'recovered' alert lands here before standing down."
    ),
    "warning": (
        "1. No page yet — this is a first-failure or a rejected/unauthorized request, not confirmed down.\n"
        "2. If it's a service health check: watch for either a 'recovered' or an escalation to critical.\n"
        "3. If it's a rejected webhook signature: check whether it's expected traffic (e.g. a rotated secret) vs. a probe — repeated rejections from the same source are worth investigating.\n"
        "4. No action needed unless this repeats or escalates."
    ),
    "info": (
        "Informational — no action needed. Health summaries and recoveries land here so the channel stays a complete log."
    ),
}


async def _dm_role_members(role_id: int, embed: discord.Embed) -> int:
    """Critical alerts don't wait for someone to be looking at #it-notifications —
    DM every member holding the given role (Security & Operations Support) directly.
    Best-effort per member: one blocked-DMs member shouldn't stop the rest."""
    guild = None
    for candidate_channel_id in (STAFF_CHANNEL_ID, SUPPORT_SESSIONS_CHANNEL_ID, ANNOUNCEMENTS_CHANNEL_ID):
        channel = await _channel_from_id(candidate_channel_id)
        if channel is not None and getattr(channel, "guild", None) is not None:
            guild = channel.guild
            break
    if guild is None:
        logger.warning("Could not resolve a guild to DM role %s for a critical alert", role_id)
        return 0

    role = guild.get_role(role_id)
    if role is None:
        logger.warning("Role %s not found in guild %s; cannot DM for critical alert", role_id, guild.id)
        return 0

    sent = 0
    for member in role.members:
        if member.bot:
            continue
        try:
            await member.send(embed=embed)
            sent += 1
        except Exception:
            logger.warning("Could not DM %s (%s) for critical alert", member, member.id)
    return sent


async def platform_alert_handler(request: web.Request) -> web.Response:
    """Inbound webhook for platform.deepiri.com system/security notifications
    (auth failures, webhook signature rejections, backend errors, etc.) -> posted
    into #it-notifications (STAFF_CHANNEL_ID). Same signed-webhook scheme as the
    announcements bridge — shares ANNOUNCEMENTS_INBOUND_SECRET since it's the same
    trust boundary (platform.deepiri.com talking to Norozo).
    """
    raw_body = await request.read()

    if not ANNOUNCEMENTS_INBOUND_SECRET:
        logger.error("Platform alert webhook disabled: ANNOUNCEMENTS_INBOUND_SECRET is not configured")
        return web.json_response({"ok": False, "message": "Webhook authentication is not configured"}, status=503)

    sig_header = (
        request.headers.get("X-Norozo-Signature")
        or request.headers.get("X-Platform-Signature")
        or ""
    )
    if not sig_header or not _is_valid_announcement_signature(raw_body, sig_header, ANNOUNCEMENTS_INBOUND_SECRET):
        return web.json_response({"ok": False, "message": "Missing or invalid signature"}, status=401)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return web.json_response({"ok": False, "message": "Invalid JSON"}, status=400)

    title = str(payload.get("title") or "Platform Alert").strip()[:256]
    message_text = str(payload.get("message") or payload.get("body") or "").strip()
    service = str(payload.get("service") or "platform.deepiri.com").strip()
    severity = str(payload.get("severity") or "info").strip().lower()
    steps = str(payload.get("steps") or payload.get("runbook") or "").strip()
    if not message_text:
        return web.json_response({"ok": False, "message": "Missing message/body"}, status=400)

    if STAFF_CHANNEL_ID is None:
        return web.json_response({"ok": False, "message": "STAFF_CHANNEL_ID not configured"}, status=500)
    channel = await _channel_from_id(STAFF_CHANNEL_ID)
    if not channel:
        return web.json_response({"ok": False, "message": "it-notifications channel not found"}, status=500)

    embed = discord.Embed(
        title=title,
        description=message_text[:4000],
        color=_ALERT_SEVERITY_COLORS.get(severity, discord.Color.blue()),
    )
    if not steps:
        steps = _DEFAULT_ALERT_STEPS.get(severity, _DEFAULT_ALERT_STEPS["warning"])
    embed.add_field(name="How to handle", value=steps[:1024], inline=False)
    embed.set_footer(text=f"{service} • {severity.upper()}")

    try:
        await channel.send(embed=embed)
    except Exception:
        logger.exception("Failed to post platform alert to Discord")
        return web.json_response({"ok": False, "message": "Failed to post to Discord"}, status=500)

    dm_count = 0
    if severity == "critical" and IT_OPERATIONS_SUPPORT_ROLE_ID is not None:
        dm_count = await _dm_role_members(IT_OPERATIONS_SUPPORT_ROLE_ID, embed)

    return web.json_response({"ok": True, "dmed": dm_count})


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
    app.router.add_post("/alerts/webhook", platform_alert_handler)
    app.router.add_post("/webhooks/platform-alerts", platform_alert_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host=WEBHOOK_HOST, port=WEBHOOK_PORT)
    await site.start()

    bot.webhook_runner = runner
    print(f"Plaky webhook server listening on http://{WEBHOOK_HOST}:{WEBHOOK_PORT}/plaky/webhook")
    print(f"Announcements webhook listening on http://{WEBHOOK_HOST}:{WEBHOOK_PORT}/announcements/webhook")


def _is_discord_rate_limit_error(error: Exception) -> bool:
    s = str(error).lower()
    return any(x in s for x in ("429", "1015", "too many requests", "cloudflare", "rate limit"))


def _extract_retry_after(error: Exception) -> int | None:
    s = str(error)
    if "Retry-After" in s:
        try:
            parts = s.split("Retry-After")
            if len(parts) > 1:
                return int(parts[1].split()[0].strip("=:,[]"))
        except Exception:
            pass
    return None


def _create_and_register_bot() -> DeepiriBot:
    """Scalable factory: fresh bot per retry with full handler registration.

    Underlying Session is closed happened because aiohttp ClientSession was closed via
    bot.close() after failed login and then reused. Factory avoids reuse by creating
    a new DeepiriBot + meeting_service + all event/command handlers each attempt.
    """
    new_bot = DeepiriBot()
    new_meeting = setup_meeting_features(new_bot)
    # Attach for on_ready
    new_bot.meeting_service = new_meeting  # type: ignore[attr-defined]

    @new_bot.event  # type: ignore[attr-defined]
    async def on_ready() -> None:  # type: ignore[no-redef]
        print(f"Logged in as {new_bot.user} (id={new_bot.user.id if new_bot.user else 'unknown'})")
        try:
            new_bot.meeting_service.start_loop()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Failed to start meeting loop")
        asyncio.create_task(_catch_up_since_last_online(new_bot))
        asyncio.create_task(_heartbeat_last_online())

    @new_bot.event  # type: ignore[attr-defined]
    async def on_member_join(member: discord.Member) -> None:  # type: ignore[no-redef]
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

    @new_bot.event  # type: ignore[attr-defined]
    async def on_member_update(before: discord.Member, after: discord.Member) -> None:  # type: ignore[no-redef]
        if not GITHUB_ORG or not GITHUB_PAT:
            return
        before_roles = {r.id for r in before.roles}
        after_roles = {r.id for r in after.roles}
        added = after_roles - before_roles
        if not added:
            return
        github_username = _get_github_username_for_member(after)
        if not github_username:
            logger.info("Member %s gained roles %s but no GitHub username mapping found, skipping team sync", after.id, added)
            return
        added_roles = [r for r in after.roles if r.id in added]
        added_names_lower = {r.name.strip().lower() for r in added_roles}
        qa_triggered = (QA_ROLE_ID is not None and QA_ROLE_ID in added) or (QA_ROLE_ID is None and ("qa" in added_names_lower or "quality assurance" in added_names_lower))
        it_candidates = {"it operations support", "support operations", "it", "it-management", "security it", "it operations", "support operations and security it"}
        it_triggered = (IT_OPERATIONS_SUPPORT_ROLE_ID is not None and IT_OPERATIONS_SUPPORT_ROLE_ID in added) or (IT_OPERATIONS_SUPPORT_ROLE_ID is None and bool(added_names_lower & it_candidates))
        if qa_triggered:
            try:
                result = await asyncio.to_thread(add_user_to_team, username=github_username, github_org=GITHUB_ORG, github_pat=GITHUB_PAT, team_slug=GITHUB_SUPPORT_TEAM_SLUG)
                if not result.get("ok"):
                    logger.warning("Failed to add %s to support team: %s", github_username, result.get("message"))
            except Exception:
                logger.exception("Exception syncing QA to GitHub team")
        if it_triggered:
            try:
                result = await asyncio.to_thread(add_user_to_team, username=github_username, github_org=GITHUB_ORG, github_pat=GITHUB_PAT, team_slug=GITHUB_IT_TEAM_SLUG)
                if not result.get("ok"):
                    logger.warning("Failed to add %s to IT team: %s", github_username, result.get("message"))
            except Exception:
                logger.exception("Exception syncing IT to GitHub team")

    @new_bot.event  # type: ignore[attr-defined]
    async def on_message(message: discord.Message) -> None:  # type: ignore[no-redef]
        if message.author.bot:
            return
        content = message.content or ""
        await notify_support_team_for_message(message)
        if await _maybe_handle_kick_out_command(message):
            await new_bot.process_commands(message)
            return
        await _maybe_auto_assign_ipca_roles(message)
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
                embed = discord.Embed(title=f"PR #{pr_number} linked to Plaky task", description=f"[Pull Request]({pr_url})\n[Plaky Task]({plaky_url})", color=discord.Color.blue())
                embed.set_footer(text=f"Linked by {message.author.display_name}")
                await message.channel.send(embed=embed)
            elif pr_match and not plaky_match:
                await message.channel.send(f"{message.author.mention} please include the Plaky task URL (app.plaky.com/...) with your PR link.")
        await new_bot.process_commands(message)

    # setup_meeting_features(new_bot) above already registers the meetings commands on
    # new_bot.tree directly. The rest (github-invite-request, ipca-signed, offboard-user,
    # plaky-request, plaky-status, poll) were previously bound to the module-level `bot`'s
    # tree at import time — a tree that's never synced since only new_bot.start() runs.
    # That silently dropped them from `/` every retry. Register them on new_bot too.
    _register_slash_commands(new_bot)

    return new_bot


async def _connect_discord_with_retry(token: str, max_backoff: int = 300) -> None:
    global bot, meeting_service
    attempt = 0
    consecutive = 0
    while True:
        attempt += 1
        consecutive += 1
        # Fresh bot per attempt — fixes Session is closed (aiohttp ClientSession closed then reused)
        bot = _create_and_register_bot()
        meeting_service = bot.meeting_service  # type: ignore[attr-defined]
        # Update _channel_from_id closure to use new global bot
        globals()["bot"] = bot
        globals()["meeting_service"] = meeting_service
        try:
            logger.info("Discord startup attempt %s (consecutive %s)", attempt, consecutive)
            await bot.start(token)  # type: ignore[arg-type]
            logger.info("Discord bot started successfully")
            return
        except asyncio.CancelledError:
            logger.info("Discord startup cancelled")
            if bot and not bot.is_closed():
                try:
                    await bot.close()
                except Exception:
                    pass
            raise
        except Exception as err:
            logger.exception("Discord startup failed (attempt %s)", attempt)
            if bot and not bot.is_closed():
                try:
                    await bot.close()
                except Exception:
                    pass
            is_rate = _is_discord_rate_limit_error(err)
            retry_after = _extract_retry_after(err)
            if is_rate:
                logger.warning("Discord 429/1015 detected — Render IP 74.220.48.29 Cloudflare ban, not token")
            if retry_after:
                wait = retry_after
                logger.info("Respecting Retry-After %ss", wait)
            else:
                base = min(2 ** (consecutive - 1), max_backoff)
                wait = base + random.uniform(0, base * 0.1)
                logger.info("Retrying in %.1fs (exp backoff max %ss)", wait, max_backoff)
            await asyncio.sleep(wait)


async def main() -> None:
    await start_webhook_server()
    await _connect_discord_with_retry(DISCORD_TOKEN)  # type: ignore[arg-type]


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is required in .env (or DISCORD_BOT_TOKEN)")

    asyncio.run(main())
