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
from typing import Dict, Optional
from urllib.parse import urlparse

import discord
import httpx
from aiohttp import BasicAuth, web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bot import format_discussion_body, format_discussion_title, resolve_discord_mentions
from emailer import send_email
from github import add_user_to_team, get_pull_request, get_pull_request_reviews, get_user_profile, invite_user, is_org_member, list_open_prs, list_org_members, remove_user_from_org, remove_user_from_team
from identity_match import AMBIGUITY_MARGIN, best_match
from github_discussion import GitHubDiscussionError, create_github_discussion
from meetings import setup_meeting_features
from onboarding import ApprovalView
from member_email_store import load_member_profile, save_member_email, save_member_real_name
from pr_staleness_store import claim_pr_staleness_1month, find_discord_id_by_email, load_pr_staleness, save_pr_staleness
from plaky import create_task, find_user_email, get_tasks
from plaky_invite import (
    call_plaky_bridge_invite,
    call_plaky_bridge_kick,
    get_user_data,
    is_valid_email,
    persist_member_email,
    remember_user_data,
    resolve_member_email,
)
from security_assessment import overall_status, render_digest_lines, render_indepth_report, run_full_assessment
from state_store import load_last_online_at, load_state, save_last_online_at, save_state


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
PLAKY_BRIDGE_URL = os.getenv("PLAKY_BRIDGE_URL", "http://plaky-bridge:5009")
INTERNAL_SERVICE_SECRET = os.getenv("INTERNAL_SERVICE_SECRET", "")
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
QA_ROLE_ID = _int_env("QA_ROLE_ID")  # "QA" Discord role -- also gates PR-staleness QA reviewer pings; must be set explicitly
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

# "@Someone is retiring" / "@Someone retiring as well" / "@Someone is leaving
# Deepiri" -- voluntary offboarding, triggered by "retiring" anywhere in a
# message, or the exact combined phrase "leaving deepiri" (not just both
# words separately in the same sentence) -- staff-only, since it leads to the
# same Discord kick + GitHub org removal as kick-out. Falls back to the
# current thread's ticket creator when no @mention is present.
RETIRING_TRIGGER_RE = re.compile(r"\bretiring\b|\bleaving\s+deepiri\b", re.IGNORECASE)

# PR staleness escalation: 2 weeks -> #qa-support-team (one-time, includes the
# assigned QA reviewer), 2.5 weeks -> recurring DM to the author AND, separately,
# to any assigned QA reviewer who hasn't reviewed yet (cadence tightens with
# age), 2.5 months -> #announcements (public, one-time -- never repeats no matter
# how much older the PR gets).
# Env-overridable, defaulting to the IDs actually in use.
PR_STALE_QA_CHANNEL_ID = _int_env("PR_STALE_QA_CHANNEL_ID") or 1438705614649032755  # #qa-support-team
# 1-month tier posts to the same #announcements channel used everywhere else --
# no separate env var, just reuse ANNOUNCEMENTS_CHANNEL_ID.
# Repos that never count toward staleness at all, for anyone -- demo/scratch
# repos and the org's .github repo (community-health-file config, not a real
# project repo). Matched case-insensitively against just the repo name (after
# the "org/").
PR_STALE_EXCLUDED_REPOS = {"deepiri-demo", ".github"}
# Narrower exclusion: specific authors excluded only within specific repos --
# no QA-channel post, no DM, no reminder of any kind for these, but other
# authors' PRs in the same repo are still tracked normally.
PR_STALE_EXCLUDED_AUTHORS_PER_REPO = {"diva": {"jrb00013"}}
PR_STALE_2WEEK_DAYS = 14
PR_STALE_2_5WEEK_DAYS = 17.5  # when the recurring author/QA-reviewer DMs start
PR_STALE_3WEEK_DAYS = 21
PR_STALE_DM_DAILY_DAYS = 30  # 1 month+ -> DM cadence tightens to daily
PR_STALE_ANNOUNCE_DAYS = 75  # 2.5 months -> one-time public #announcements post
PR_STALE_SCAN_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours -- PR age changes slowly

# Routine security-posture checks (GitHub access/alerts, Discord admins, Plaky
# access, Deepiri.com, email SPF/DMARC, Deepiri Cloud): a short digest posts to
# #it-notifications twice a week, and every IT_OPERATIONS_SUPPORT_ROLE_ID member
# gets the same digest DMed once a week regardless of whether anyone ran the
# in-depth command -- manual routine checks stay a requirement, this just makes
# sure nobody has to remember to look.
SECURITY_ASSESSMENT_WEBSITE_URL = os.getenv("SECURITY_ASSESSMENT_WEBSITE_URL", "https://deepiri.com")
SECURITY_ASSESSMENT_EMAIL_DOMAIN = os.getenv("SECURITY_ASSESSMENT_EMAIL_DOMAIN", "deepiri.com")
SECURITY_ASSESSMENT_DIGEST_WEEKDAYS = {0, 3}  # Monday and Thursday -- twice a week
SECURITY_ASSESSMENT_DM_WEEKDAY = 0  # Monday -- once a week
SECURITY_ASSESSMENT_RUN_HOUR_UTC = 9
SECURITY_ASSESSMENT_LOOP_INTERVAL_SECONDS = 60 * 60  # check hourly whether it's time to fire


def _pr_stale_dm_cadence_days(age_days: float) -> float:
    """How often to re-nag (author or assigned QA reviewer) once a PR has
    crossed the 2-week mark: weekly at first, every 3 days past 3 weeks,
    daily once it's a month+ old."""
    if age_days >= PR_STALE_DM_DAILY_DAYS:
        return 1.0
    if age_days >= PR_STALE_3WEEK_DAYS:
        return 3.0
    return 7.0

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
EMAIL_SEARCH_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# "Add me / add someone to the Plaky" support-ticket intent (Plaky invite spec):
# a ticket that asks for Plaky access resolves straight into the same invite
# pipeline as IPCA signing, instead of waiting for a human to run /plaky-invite.
PLAKY_ADD_INTENT_RE = re.compile(r"\bpla[ckhy].?\b", re.IGNORECASE)
_POSITIVE_PLAKY_VERBS = re.compile(
    r"\b(?:add(?:ed|ing)?|invite(?:d|ing)?|grant(?:ed|ing)?|join(?:ed|ing)?|"
    r"need(?:ed)?|want|would\s+like|set\s+(?:me|it|us|everyone)?\s*up|"
    r"sign(?:ed)?\s+ipca|request(?:ed|ing)?|access)\b",
    re.IGNORECASE,
)
_STRONG_PLAKY_VERBS = re.compile(
    r"\b(?:add(?:ed|ing)?|need(?:ed)?|want|would\s+like|grant(?:ed|ing)?|join(?:ed|ing)?|"
    r"sign(?:ed)?\s+ipca)\b",
    re.IGNORECASE,
)
_ADD_PLAKY_VERB_RE = re.compile(r"\badd(?:ed|ing)?\b|\binvite(?:d|ing)?\b|\bjoin(?:ed|ing)?\b", re.IGNORECASE)
_NEGATIVE_PLAKY_VERBS = re.compile(
    r"\b(?:remove(?:d)?|offboard|deactivate|delete|kick|leave|drop|revoke|sign\s*out)\b",
    re.IGNORECASE,
)
_PLAKY_REPORT_NOISE_RE = re.compile(
    r"\b(?:worked|sent|done|ok|confirmed|finished|completed|resent|received|accepted|already\s+invited)\b",
    re.IGNORECASE,
)
_ADD_TARGET_RE = re.compile(r"\badd\s+([A-Za-z][A-Za-z.'\- ]*?)\s+(?:to|onto|into)\s+pla[ckhy]", re.IGNORECASE)

# In-thread "we still need your email for the Plaky invite" asks, keyed by thread
# id, so the person's next reply (their own email, also used when they signed the
# IPCA in the same ticket) completes the invite without a second human touch.
PENDING_PLAKY_EMAIL_THREADS: Dict[int, Dict] = {}
_pending_plaky_email_lock = asyncio.Lock()
PLAKY_PENDING_ASK_TTL_SECONDS = 72 * 60 * 60

# New-member onboarding: DM asks for email + role, each reply classified
# independently (no conversation state to track/lose on a restart). IT/Security &
# Operations Support is excluded by name before any fuzzy scoring runs -- it has
# elevated permissions and must never be self-assignable through this flow.
# Matched by fuzzy confidence against the known real name rather than requiring a
# role ID to be configured -- same "look it up live" approach as the categories
# below, and it still catches a renamed/differently-cased role since it's a
# similarity match, not an exact string.
_IT_ROLE_WORD_RE = re.compile(r"\bit\b", re.IGNORECASE)
_ELEVATED_ROLE_REFERENCE_NAME = "Security & Operations Support"
_ELEVATED_ROLE_EXCLUDE_MIN_SCORE = 0.75


def _is_elevated_role(role_name: str) -> bool:
    if _IT_ROLE_WORD_RE.search(role_name):
        return True
    match = best_match(role_name, [_ELEVATED_ROLE_REFERENCE_NAME], min_score=_ELEVATED_ROLE_EXCLUDE_MIN_SCORE)
    return match is not None

_ROLE_CATEGORY_PATTERNS = [
    ("AI Engineer", [re.compile(r"\bai\b", re.IGNORECASE)], False),
    ("ML Engineer", [re.compile(r"\bml\b", re.IGNORECASE), re.compile(r"\bmachine\s+learning\b", re.IGNORECASE)], False),
    ("Data Engineer", [re.compile(r"\bdata\b", re.IGNORECASE)], False),
    ("Cloud/Infra/Security Engineer", [re.compile(r"\b(cloud|infra(structure)?|security)\b", re.IGNORECASE)], True),
    ("Frontend Engineer", [re.compile(r"\bfront[-\s]?end\b", re.IGNORECASE)], False),
    ("Fullstack Engineer", [re.compile(r"\bfull[-\s]?stack\b", re.IGNORECASE)], False),
    ("Backend Engineer", [re.compile(r"\bback[-\s]?end\b", re.IGNORECASE)], False),
]
_ENGINEER_WORD_RE = re.compile(r"\bengineer\b", re.IGNORECASE)
_ROLE_ATTEMPT_HINT_RE = re.compile(
    r"\b(ai|ml|data|cloud|infra|infrastructure|security|frontend|front-end|front end|"
    r"fullstack|full-stack|full stack|backend|back-end|back end|engineer|developer|dev|role|team|it)\b",
    re.IGNORECASE,
)
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

USER_DATA_PATH = Path("user_data.json")
pending_email_requests = {}  # user_id -> True (we only track pending state)


def _load_user_data() -> dict:
    try:
        if not USER_DATA_PATH.exists():
            return {}
        raw = USER_DATA_PATH.read_text(encoding="utf-8").strip() or "{}"
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Failed to load user data")
        return {}


def _save_user_data(data: dict) -> None:
    try:
        USER_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = USER_DATA_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(USER_DATA_PATH)
    except Exception:
        logger.exception("Failed to save user data")


def _remember_user_data(discord_id: int, github_username: str = None, email: str = None) -> None:
    data = _load_user_data()
    key = str(discord_id)
    if key not in data:
        data[key] = {}
    if github_username:
        data[key]["github"] = github_username.lower()
    if email:
        data[key]["email"] = email.lower().strip()
    _save_user_data(data)


def _get_user_data(discord_id: int) -> dict:
    data = _load_user_data()
    return data.get(str(discord_id), {})


async def _resolve_member_email_for_plaky(member: discord.Member, github_username: Optional[str] = None) -> Optional[str]:
    """Thin adapter over plaky_invite.resolve_member_email's ordered trust
    chain (local user_data -> platform cloud DB -> GitHub public profile ->
    Plaky roster fuzzy match), passing the Discord-side name hints that chain
    needs for its last-resort fuzzy match."""
    if not isinstance(member, discord.Member):
        return None
    member_hints = [member.display_name, str(getattr(member, "global_name", "") or ""), str(member.name)]
    return await resolve_member_email(
        member.id,
        github_username=github_username,
        member_hints=[h for h in member_hints if h],
        api_key=PLAKY_API_KEY,
    )


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


def _is_plaky_add_intent(content: str) -> bool:
    """True when a support-ticket message is asking to ADD someone to the Plaky
    (Joe's support-ticket spec). 'plak' + an add/join/need/want/access signal;
    explicitly NOT a remove/offboard/kick intent. Report-style chatter ('the
    plaky invite worked') is filtered so it doesn't re-fire the pipeline."""
    text = (content or "").strip().lower()
    if not PLAKY_ADD_INTENT_RE.search(text):
        return False

    has_invite = re.search(r"\binvite\b", text) is not None

    # "issue"/"help" alongside "invite" signals a problem report or a general
    # assistance ask rather than a plain invite request ("having an issue
    # with my plaky invite", "can someone help with my plaky invite") --
    # authoritative regardless of what the older verb-matching logic below
    # would otherwise decide, since _POSITIVE_PLAKY_VERBS treats "invite" as
    # a positive signal on its own too.
    if has_invite and ("issue" in text or "help" in text):
        return False

    # Report-style chatter ("the plaky invite worked", "plaky invite was sent
    # already") must stay filtered regardless of which path below would
    # otherwise fire on it -- computed once and checked by both.
    is_report_noise = bool(_PLAKY_REPORT_NOISE_RE.search(text) and not _STRONG_PLAKY_VERBS.search(text))

    # Direct rule: "invite" and "plaky" together in the same message is an
    # add-intent on its own (the issue/help/report-noise exclusions above and
    # below already ran).
    if not is_report_noise and has_invite:
        return True

    positive = _POSITIVE_PLAKY_VERBS.search(text)
    if not positive:
        return False
    negative = _NEGATIVE_PLAKY_VERBS.search(text)
    if negative:
        # "i need to be removed from plaky" has 'need' before 'removed' -- the
        # negative verb still wins. Only keep as an add-intent when an explicit
        # add/invite/join verb appears AFTER the negative ("remove X but add me").
        strong_add = _ADD_PLAKY_VERB_RE.search(text)
        if not strong_add or strong_add.start() < negative.start():
            return False
    if is_report_noise:
        return False
    return True


def _email_from_text(text: str) -> Optional[str]:
    match = EMAIL_SEARCH_RE.search(text or "")
    return match.group(0) if match else None


def _member_name_hints(member: Optional[discord.Member]) -> list:
    hints = []
    if member is None:
        return hints
    for attr in ("display_name", "global_name", "name"):
        value = getattr(member, attr, None)
        if value:
            hints.append(str(value))
    return hints


async def _find_member_by_name(guild: Optional[discord.Guild], name: str) -> Optional[discord.Member]:
    """@mention-free target: exact display/global/username match wins, then a
    fuzzy similarity match over every member's name fields (same best_match the
    role-picker uses). Returns None when there's no confident match -- the caller
    then asks instead of guessing an email."""
    if guild is None:
        return None
    normalized = (name or "").strip().lower()
    if not normalized:
        return None
    for member in guild.members:
        if member.bot:
            continue
        for candidate in (member.display_name, member.global_name, member.name):
            if candidate and candidate.strip().lower() == normalized:
                return member
    pool: dict = {}
    key_list: list = []
    for member in guild.members:
        if member.bot:
            continue
        for attr in ("display_name", "global_name", "name"):
            key = str(getattr(member, attr, "") or "").strip().lower()
            if key and key not in pool:
                pool[key] = member
                key_list.append(key)
    match = best_match(normalized, key_list) if key_list else None
    if match is not None:
        return pool[key_list[match.index]]
    return None


async def _resolve_github_for_member(discord_id: int, member: Optional[discord.Member] = None) -> Optional[str]:
    """GitHub identity for the invite chain: local user_data first, then the
    platform cloud DB, then the existing github_username_map (needs the member)."""
    entry = get_user_data(discord_id)
    if entry.get("github_username"):
        return entry["github_username"]
    cloud = await load_member_profile(discord_id)
    if cloud.get("github_username"):
        return cloud["github_username"]
    if member is not None:
        return _get_github_username_for_member(member)
    return None


async def _pending_plaky_ask_pop(thread_id: int) -> Optional[dict]:
    async with _pending_plaky_email_lock:
        entry = PENDING_PLAKY_EMAIL_THREADS.get(thread_id)
        if not entry:
            return None
        if time.time() - float(entry.get("requested_at", 0)) > PLAKY_PENDING_ASK_TTL_SECONDS:
            PENDING_PLAKY_EMAIL_THREADS.pop(thread_id, None)
            return None
        return PENDING_PLAKY_EMAIL_THREADS.pop(thread_id)


async def _pending_plaky_ask_set(thread_id: int, entry: dict) -> None:
    async with _pending_plaky_email_lock:
        PENDING_PLAKY_EMAIL_THREADS[thread_id] = entry


async def _invite_member_to_plaky(
    *,
    discord_id: int,
    discord_username: str,
    member: Optional[discord.Member] = None,
    github_username: Optional[str] = None,
    email: Optional[str] = None,
    role: str = "MEMBER",
) -> tuple:
    """Core invite pipeline: resolve an email through the ordered chain
    (local -> cloud DB -> GitHub profile -> Plaky roster), then hand it to the
    headless bridge. Returns (status, email) where status is one of:
    'ok', 'already', 'asked', 'failed:<reason>'. Every capture feeds both the
    local user_data mirror and the platform cloud DB."""
    # Whether THIS request explicitly supplied the address matters for how the
    # result gets worded -- a repeat "invite me to plaky" with no email typed
    # silently reused whatever resolve_member_email's fallback chain (local ->
    # cloud -> GitHub -> Plaky roster) turned up, with nothing telling the
    # requester it did that or giving them a chance to correct a stale/wrong
    # address on file. The "_from_file" status variants surface that instead
    # of a bare "invite sent" that looks identical to a genuinely fresh one.
    email_was_given = bool(email)
    if not email:
        email = await resolve_member_email(
            discord_id,
            github_username=github_username,
            member_hints=_member_name_hints(member),
            api_key=PLAKY_API_KEY or None,
        )
    if not email:
        return ("asked", None)
    result = await call_plaky_bridge_invite(email, role=role)
    if result.get("success"):
        await persist_member_email(discord_id, discord_username, email, github_username=github_username, overwrite=email_was_given)
        return ("ok" if email_was_given else "ok_from_file", email)
    # overwrite=email_was_given here too -- an explicit correction must be
    # saved even when the bridge reports "already" for the new address (real
    # incident: a corrected email kept losing to the stale one on file
    # because this call, on the "already" path, never overwrote at all).
    remember_user_data(discord_id, email=email, github_username=github_username, overwrite=email_was_given)
    if result.get("already"):
        return ("already" if email_was_given else "already_from_file", email)
    return (f"failed:{result.get('error') or 'bridge error'}", email)


def _plaky_invite_status_text(status: str, email: Optional[str], sender_mention: str) -> str:
    if status in ("ok", "ok_from_file"):
        text = f"✅ For the Plaky board invite, we just sent one to **{email}** — you should get an email shortly, accept it from there."
        if status == "ok_from_file":
            text += " That's the email already on file for you -- reply with a different one if that's wrong."
        return text
    if status in ("already", "already_from_file"):
        text = f"ℹ️ For the Plaky board invite, your email **{email}** is already in the workspace — you should have already received an invite for it."
        if status == "already_from_file":
            text += " That's the email already on file for you -- reply with a different one if that's wrong."
        return text
    if status == "asked":
        return (
            f"{sender_mention} For the Plaky board invite, I need your email. "
            "Reply in this thread with the address (for example `jane@deepiri.com`) and I'll take it from there."
        )
    reason = status.split(":", 1)[1] if ":" in status else status
    return f"⚠️ Plaky invite for **{email or '?'}** failed: {reason}"


async def _maybe_handle_plaky_pending_email_reply(message: discord.Message) -> bool:
    """Completes a pending email ask for a Plaky invite. Two shapes:

    - A ticket thread that was asked in-thread for the invite address -- the
      next message carrying one completes the invite for the pending member.
    - A DM reply to the email ask Norozo sent after an IPCA sign (the DM is
      framed purely as 'adding you to Plaky'; the captured address also goes on
      file via the usual persistence chain -- nothing here ever mentions
      offboarding).

    Replays in whatever channel the ask was issued."""
    if message.author.bot:
        return False
    if message.guild is None:
        if not isinstance(message.channel, discord.DMChannel):
            return False
        entry = await _pending_plaky_ask_pop(message.channel.id)
        if entry is None or str(entry.get("discord_id") or "") != str(message.author.id):
            return False
        email = _email_from_text(message.content or "")
        if not email:
            # Still waiting on the address -- don't let another handler eat the reply.
            await _pending_plaky_ask_set(message.channel.id, entry)
            return True
        status, used_email = await _invite_member_to_plaky(
            discord_id=message.author.id,
            discord_username=str(message.author),
            github_username=entry.get("github_username"),
            email=email,
            role=entry.get("role") or "MEMBER",
        )
        await message.channel.send(_plaky_invite_status_text(status, used_email or email, ""))
        return True
    if not isinstance(message.channel, discord.Thread):
        return False
    entry = await _pending_plaky_ask_pop(message.channel.id)
    if not entry:
        return False
    email = _email_from_text(message.content or "")
    if not email:
        # Still waiting on the address -- don't let another handler eat the reply.
        await _pending_plaky_ask_set(message.channel.id, entry)
        return True
    status, used_email = await _invite_member_to_plaky(
        discord_id=int(entry.get("discord_id") or message.author.id),
        discord_username=str(message.author),
        github_username=entry.get("github_username"),
        email=email,
        role=entry.get("role") or "MEMBER",
    )
    reply_channel = await _resolve_reply_channel(message)
    await reply_channel.send(_plaky_invite_status_text(status, used_email or email, message.author.mention))
    return True


async def _maybe_handle_plaky_add_request(message: discord.Message) -> bool:
    """Support-ticket intent: 'I want to add someone to the Plaky' /
    'I need to be added to Plaky'. Resolves who's being added (mention wins,
    then self, then a named member, then ask), resolves the email, invites, or
    starts an in-thread ask for the address. Returns True when it handled it."""
    if message.guild is None or message.author.bot:
        return False
    if not isinstance(message.author, discord.Member):
        return False
    if not _is_support_sessions_channel(message.channel):
        return False
    content = (message.content or "").strip()
    if not content or not _is_plaky_add_intent(content):
        return False

    guild = message.guild
    reply_channel = await _resolve_reply_channel(message)

    mentioned = next((m for m in getattr(message, "mentions", []) if isinstance(m, discord.Member) and not m.bot), None)
    email = _email_from_text(content)
    target: Optional[discord.Member] = mentioned
    github_username: Optional[str] = None

    if target is not None and target.id == message.author.id:
        target = None  # self-mention counts as self-request
    if target is None and _is_self_add_phrasing(content):
        target = message.author
    if target is None:
        named = _extract_add_target_name(content)
        if named:
            target = await _find_member_by_name(guild, named)
            if target is None:
                github_username = _extract_github_profile_username(content) if URL_RE.search(content) else None

    if target is not None:
        github_username = github_username or await _resolve_github_for_member(target.id, target)
        status, used_email = await _invite_member_to_plaky(
            discord_id=target.id,
            discord_username=str(target),
            member=target,
            github_username=github_username,
            email=email,
            role="MEMBER",
        )
        if status in ("asked", "ok_from_file", "already_from_file"):
            # Real gap this closes: telling the requester "reply with a
            # different email if that's wrong" (the "_from_file" wording) did
            # nothing if they actually did reply -- no pending entry was ever
            # registered for those statuses, so the correction silently fell
            # on the floor with no confirmation either way. Same pending-ask
            # mechanism as a fresh "asked" -- the next email-carrying reply in
            # this thread re-invites with the corrected address and persists
            # it, overwriting the stale one on file.
            await _pending_plaky_ask_set(
                reply_channel.id if getattr(reply_channel, "id", None) else message.channel.id,
                {
                    "discord_id": target.id,
                    "github_username": github_username,
                    "role": "MEMBER",
                    "requested_at": time.time(),
                    "sender_id": message.author.id,
                },
            )
        await reply_channel.send(_plaky_invite_status_text(status, used_email or email, message.author.mention))
        return True

    # No member resolved but the message carried an email -- invite that address
    # and keep the record attached to the requester (best available attribution).
    if email:
        status, used_email = await _invite_member_to_plaky(
            discord_id=message.author.id,
            discord_username=str(message.author),
            member=message.author,
            github_username=github_username,
            email=email,
            role="MEMBER",
        )
        await reply_channel.send(_plaky_invite_status_text(status, used_email or email, message.author.mention))
        return True

    await reply_channel.send(
        f"{message.author.mention} Who should I add to the Plaky? Ping the person here "
        "or paste their email directly and I'll set it up."
    )
    return True


def _is_self_add_phrasing(content: str) -> bool:
    low = (content or "").lower()
    if re.search(r"\b(?:me|myself)\b", low):
        return True
    return re.search(r"\bi\s+(?:want|need|would\s+like|want\s+to|need\s+to|should)\b", low) is not None


def _extract_add_target_name(content: str) -> Optional[str]:
    """'add <name> to plaky' -> the name; 'add me/someone' returns None so the
    caller routes those to self/ask paths."""
    match = _ADD_TARGET_RE.search(content or "")
    if not match:
        return None
    candidate = match.group(1).strip()
    if not candidate or candidate.lower() in ("me", "myself", "i", "someone", "a person", "the person"):
        return None
    if candidate.endswith(" to the") or candidate.endswith(" to"):
        return None
    return candidate


async def _resolve_plaky_invite_for_ipca_sign(message: discord.Message, member: discord.Member) -> tuple:
    """IPCA-sign side effect: once roles are granted, the same ticket message
    also kicks off the Plaky invite (email from the message, GitHub link if
    present, then the resolution chain). Runs the resolution only -- sends
    nothing itself -- so the caller (_maybe_auto_assign_ipca_roles) can fold
    the outcome into the SAME access-confirmation message, in the SAME
    support-ticket thread the conversation is already happening in, rather
    than a separate message (or, previously, a separate DM the person had to
    go find). Called from both the live on_message path and the restart
    catch-up sweep, so a bot-downtime window no longer silently skips the
    Plaky invite the way it used to when only on_message triggered it.
    Returns (status, email, github_username)."""
    content = message.content or ""
    email = _email_from_text(content)
    github_username = None
    if URL_RE.search(content):
        candidate = _extract_github_profile_username(content)
        if candidate:
            github_username = candidate
            try:
                await _remember_identity(member.id, candidate, member)
            except Exception:
                logger.exception("Failed to remember GitHub username %s during Plaky invite", candidate)
    status, used_email = await _invite_member_to_plaky(
        discord_id=member.id,
        discord_username=str(member),
        member=member,
        github_username=github_username or await _resolve_github_for_member(member.id, member),
        email=email,
        role="MEMBER",
    )
    return status, used_email or email, github_username


def _ipca_access_and_plaky_text(mention: str, status: str, email: Optional[str]) -> str:
    """One combined message: the access-grant confirmation plus the Plaky
    outcome, instead of two separate sends."""
    prefix = f"{mention} We gave you access to the rest of the Discord."
    if status == "asked":
        return f"{prefix} What's your email? So I can get you invited to Plaky."
    return f"{prefix} {_plaky_invite_status_text(status, email, mention)}"


def _github_it_team_dm_ask_text(member_name: str) -> str:
    """DM copy for when the IT/Support-Operations role grant fires the GitHub
    org+team auto-invite but no GitHub identity could be resolved yet."""
    return (
        f"Hey {member_name}! You just got IT/Support Operations access, which "
        f"also gets you added to the `{GITHUB_IT_TEAM_SLUG}` GitHub team -- but I "
        "couldn't find a GitHub username on file for you yet. Either post your "
        "GitHub profile link in #github-profiles, or reply here with your GitHub "
        "username, and I'll send the org + team invite right away."
    )


async def _maybe_handle_plaky_kick_on_kickout(message: discord.Message, target: discord.Member) -> str:
    """Best-effort Plaky deactivate during kick-out: resolve the target's email
    (already-loaded identity chain) and hand it to the bridge. Returns a short
    status line for the kick-out summary."""
    try:
        email = await resolve_member_email(
            target.id,
            github_username=_get_github_username_for_member(target),
            member_hints=_member_name_hints(target),
            api_key=PLAKY_API_KEY or None,
        )
    except Exception:
        logger.exception("Plaky email resolution failed for kick-out target %s", target.id)
        return "Plaky: email resolution failed"
    if not email:
        return "Plaky: no email on file, skipped"
    result = await call_plaky_bridge_kick(email)
    if result.get("success"):
        return f"Plaky: deactivated {email}"
    return f"Plaky: kick failed ({result.get('error')})"


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
            target_channel = await _resolve_reply_channel(message)
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

    plaky_status, plaky_email, plaky_github_username = await _resolve_plaky_invite_for_ipca_sign(message, message.author)
    try:
        # support-tickets uses Discord's auto-thread feature: the triggering
        # message lives in the parent channel and spawns a same-id companion
        # thread as a SIDE EFFECT that can land after on_message already fired.
        # Re-resolve fresh rather than trusting message.thread captured at
        # handler-start -- same race _resolve_reply_channel exists to handle
        # for the kick-out command. One combined message: access confirmation
        # + the Plaky outcome (invited / already in / or the email ask),
        # instead of two separate sends.
        target_channel = await _resolve_reply_channel(message)
        await target_channel.send(_ipca_access_and_plaky_text(message.author.mention, plaky_status, plaky_email))
    except Exception:
        logger.exception("Failed to post IPCA access confirmation reply for %s", message.author.id)

    if plaky_status == "asked":
        ask_thread = await _resolve_reply_channel(message)
        await _pending_plaky_ask_set(
            ask_thread.id if getattr(ask_thread, "id", None) else message.channel.id,
            {
                "discord_id": message.author.id,
                "github_username": plaky_github_username,
                "role": "MEMBER",
                "requested_at": time.time(),
                "sender_id": message.author.id,
                "via": "thread",
            },
        )

    # Resolve again (not reuse target_channel) -- more real async time has
    # passed since the send above, during which the companion thread may have
    # only just been created.
    ticket_thread = await _resolve_reply_channel(message)
    await _close_ticket_thread(ticket_thread)

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
    """Legacy wrapper — reads from user_data.json for backward compatibility."""
    data = _load_user_data()
    return {k: v.get("github", "") for k, v in data.items() if v.get("github")}


def _save_github_username_map(mapping: dict) -> None:
    """Legacy wrapper — no-op; prefer _remember_user_data."""
    pass


def _remember_github_username(discord_id: int, github_username: str) -> None:
    if not discord_id or not github_username:
        return
    _remember_user_data(discord_id, github_username=github_username)


_LOOKS_LIKE_REAL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z.'-]*\s+[A-Za-z][A-Za-z.'-]*$")


async def _remember_identity(discord_id: int, github_username: str, member: Optional[discord.Member] = None) -> None:
    """Persists the discord_id<->github_username mapping AND opportunistically
    caches their real name, whichever path just resolved this identity --
    a self-reported GitHub link at onboarding, a #github-profiles scan match,
    an org-roster fuzzy match, or a PR-staleness reverse lookup. This is the
    dynamic identity cache growing itself: every successful resolution feeds
    it, not just the one at onboarding, so it fills in for members who joined
    before this existed too.

    Prefers GitHub's public profile name (a genuine "real name" field), but
    falls back to the Discord global_name/display_name when GitHub has none
    set AND that name is actually "First Last"-shaped -- Discord's own
    display name is frequently someone's real name too, and a name this
    specific is a far better identity-search candidate than the raw handle,
    even without GitHub confirming it.
    """
    _remember_github_username(discord_id, github_username)
    real_name = None
    if GITHUB_PAT:
        try:
            profile = await asyncio.to_thread(get_user_profile, github_username, GITHUB_PAT)
            real_name = profile.get("name")
        except Exception:
            logger.exception("Failed to fetch GitHub profile for %s (%s)", discord_id, github_username)
    if not real_name and member is not None:
        for candidate in (str(getattr(member, "global_name", "") or ""), str(getattr(member, "display_name", "") or "")):
            if candidate and _LOOKS_LIKE_REAL_NAME_RE.match(candidate.strip()):
                real_name = candidate.strip()
                break
    if real_name:
        try:
            await save_member_real_name(discord_id, real_name, github_username)
        except Exception:
            logger.exception("Failed to cache real name for %s (%s)", discord_id, github_username)


def _get_github_username_for_member(member: discord.Member) -> Optional[str]:
    """Return an explicitly mapped username, or a best-effort name-based guess.

    The name fallback is not authoritative. Critical operations should first collect
    an explicit mapping through ``/github-invite-request``.
    """
    data = _load_user_data()
    entry = data.get(str(member.id))
    if entry and entry.get("github"):
        return entry["github"]
    # Fallback: try display name or global name if it looks like a github username
    for candidate in [getattr(member, "global_name", None), getattr(member, "display_name", None), str(member.name) if hasattr(member, "name") else None]:
        if candidate and GITHUB_USERNAME_RE.match(candidate.strip()) and candidate.strip().lower() not in GITHUB_RESERVED_PATHS:
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
        logger.warning("#github-profiles scan for %s skipped: GITHUB_PROFILES_CHANNEL_ID not configured", member.id)
        return None
    channel = await _channel_from_id(GITHUB_PROFILES_CHANNEL_ID)
    if channel is None:
        logger.warning("#github-profiles scan for %s skipped: could not resolve channel %s", member.id, GITHUB_PROFILES_CHANNEL_ID)
        return None
    scanned = 0
    messages_from_author = 0
    try:
        async for msg in channel.history(limit=1000):
            scanned += 1
            if msg.author.id != member.id:
                continue
            messages_from_author += 1
            candidate = _extract_github_profile_username(msg.content or "")
            if not candidate:
                logger.info("#github-profiles: message from %s has no extractable GitHub link: %r", member.id, msg.content[:200])
                continue
            if not await asyncio.to_thread(is_org_member, candidate, GITHUB_ORG, GITHUB_PAT):
                logger.warning("Found GitHub link %s for %s in #github-profiles but they're not in %s", candidate, member.id, GITHUB_ORG)
                continue
            logger.info("#github-profiles: matched %s -> %s after scanning %s messages", member.id, candidate, scanned)
            await _remember_identity(member.id, candidate, member)
            return candidate
    except Exception:
        logger.exception("Failed scanning #github-profiles for member %s", member.id)
        return None
    logger.warning(
        "#github-profiles: scanned %s messages, found %s from member %s, no usable GitHub link",
        scanned, messages_from_author, member.id,
    )
    return None


async def _find_github_username_via_org_roster(member: discord.Member) -> Optional[str]:
    """Last resort: fuzzy-match the Discord name against the full GitHub org
    member list -- against BOTH each member's raw login AND their GitHub
    profile real name, keeping whichever scores higher (find_user_email in
    plaky.py already uses this same "try every candidate string, keep the
    single best score" shape). Real incident this fixes: org member
    "shan-versc" has GitHub real name "Shanley V.", and a Discord global_name
    of "Shanley" scored a correctly-refused ~0.47 against the bare login
    "shan-versc" but would score 0.93 (first-name-token match) against the
    real name "Shanley V." -- login-only matching silently failed to resolve
    a real member because it never looked at the one field that would work.
    GitHub logins are ALSO sometimes a genuine overlap on their own -- a
    Discord handle like "mahlaka." can be a truncated form of the GitHub
    login "samimahlaka" (SequenceMatcher ratio ~0.78) -- so both signals stay
    in play rather than real-name replacing login. Every candidate here is
    already a confirmed org member by construction, so no separate
    is_org_member re-check is needed the way the other two sources require.
    """
    if not GITHUB_ORG or not GITHUB_PAT:
        logger.warning("Org roster fallback for %s skipped: GITHUB_ORG or GITHUB_PAT not configured", member.id)
        return None
    usernames = await asyncio.to_thread(list_org_members, GITHUB_ORG, GITHUB_PAT)
    if not usernames:
        logger.warning("Org roster fallback for %s: list_org_members returned nothing", member.id)
        return None

    async def _fetch_name(username: str) -> str:
        profile = await asyncio.to_thread(get_user_profile, username, GITHUB_PAT)
        return profile.get("name") or ""

    real_names = await asyncio.gather(*(_fetch_name(u) for u in usernames))

    best_username = None
    best_score = -1.0
    best_query = None
    for candidate_name in (member.display_name, str(getattr(member, "global_name", "") or ""), str(member.name)):
        if not candidate_name:
            continue
        login_match = best_match(candidate_name, usernames)
        if login_match is not None and login_match.score > best_score:
            best_username, best_score, best_query = usernames[login_match.index], login_match.score, candidate_name
        name_match = best_match(candidate_name, real_names)
        if name_match is not None and name_match.score > best_score:
            best_username, best_score, best_query = usernames[name_match.index], name_match.score, candidate_name

    if best_username is not None:
        logger.info("Org roster fallback: matched %s (query %r) -> %s (score=%s)", member.id, best_query, best_username, best_score)
        await _remember_identity(member.id, best_username, member)
        return best_username
    logger.warning("Org roster fallback for %s: no confident match among %s org members", member.id, len(usernames))
    return None


def _is_qa_role_name(name: str) -> bool:
    """Dynamic role-name match for the GitHub support-team sync -- no hardcoded
    role id, so it keeps working across servers/role renames as long as the
    role is recognizably QA ("QA", "QA Engineer", "Quality Assurance", ...).
    \\bqa\\b (not a bare substring check) so it doesn't fire on unrelated role
    names that merely contain "qa" as part of another word.
    """
    n = (name or "").strip().lower()
    if not n:
        return False
    return "quality assurance" in n or re.search(r"\bqa\b", n) is not None


def _is_qa_lead_role_name(name: str) -> bool:
    """Subset of _is_qa_role_name for the "QA Lead" tier specifically -- Discord
    roles named like "QA Lead" or "Quality Assurance Lead" get maintainer
    access on GITHUB_SUPPORT_TEAM_SLUG instead of the plain member access every
    other QA role name (e.g. "QA Engineer") gets."""
    n = (name or "").strip().lower()
    if not n:
        return False
    return "quality assurance lead" in n or re.search(r"\bqa\s+lead\b", n) is not None


async def _resolve_github_username_for_role_sync(member: discord.Member) -> Optional[str]:
    """Full identity-resolution chain for a Discord role -> GitHub team auto-sync,
    same three sources used for the offboarding kick-out flow (in that order of
    cheapness/confidence): the persisted mapping/name-guess, then a scan of
    #github-profiles for a link this exact member posted, then a last-resort
    fuzzy match against the org roster. Each fallback only runs when the one
    before it came up empty, and the profiles-channel/org-roster hits already
    persist the mapping via _remember_identity so this chain is O(1) next time.
    """
    github_username = _get_github_username_for_member(member)
    if github_username and GITHUB_ORG and GITHUB_PAT and not await asyncio.to_thread(is_org_member, github_username, GITHUB_ORG, GITHUB_PAT):
        github_username = None
    if not github_username:
        github_username = await _find_github_username_in_profiles_channel(member)
    if not github_username:
        github_username = await _find_github_username_via_org_roster(member)
    return github_username


async def _resolve_discord_member_for_github_login(login: str, guild: discord.Guild) -> Optional[discord.Member]:
    """Reverse direction of the GitHub<->Discord identity chain used everywhere
    else this session (kick-out resolves Discord->GitHub; this resolves
    GitHub->Discord for PR staleness pings), going through Plaky as an
    intermediate hop for more context when GitHub/Discord alone aren't enough:

    1. Reverse-check the persisted github_username_map -- if some discord_id
       already maps to this login (built by kick-out's resolution + the
       onboarding DM's github-link capture), done instantly.
    2. GitHub's real display name AND raw login, each fuzzy-matched against
       current guild members' display_name/global_name/name (best score of
       the two queries wins -- a login often matches a reused Discord handle
       even when the formal real name doesn't), via identity_match.best_match,
       same refuse-rather-than-guess philosophy as everywhere else.
    3. Plaky hop: find_user_email([login, real_name], ...) -- if Plaky has this
       person under a self-reported email, reverse-look that email up against
       member_emails (self-reported at onboarding) to land on a discord_id
       directly.

    Persists a successful match back into the github_username_map so this
    resolves instantly next time. Returns None (never guesses) if nothing
    confident is found anywhere in the chain.
    """
    if not login:
        return None

    mapping = _load_github_username_map()
    login_lower = login.strip().lower()
    for discord_id_str, mapped_login in mapping.items():
        if mapped_login.strip().lower() == login_lower:
            member = guild.get_member(int(discord_id_str))
            if member is not None:
                return member

    profile = await asyncio.to_thread(get_user_profile, login, GITHUB_PAT) if GITHUB_PAT else {"name": None, "email": None}
    real_name = profile.get("name")
    github_email = profile.get("email")

    # All three Discord name fields as separate candidates (not just
    # display_name, despite what this docstring's step 2 already claimed) --
    # a member whose display_name is abbreviated ("Sergio V.") can still
    # resolve via a fuller global_name or raw username that display_name
    # alone would never surface. Built unconditionally (not gated behind
    # `if real_name`) because the login itself is also tried below -- a
    # GitHub profile with no public real name shouldn't skip this step
    # entirely when the login alone might still match a Discord handle.
    # Real incident this dedup fixes: best_match's ambiguity-refusal logic
    # treats two candidate STRINGS tied at the top score as two different
    # people and refuses to guess -- correct when they really are two
    # different members, but a member whose display_name/global_name/name
    # fields collide (e.g. display_name == global_name == "Asmita N.", or
    # username "superhuygaming" is just the login casefolded) gets counted as
    # tied against THEMSELVES in this flattened list, producing a false
    # "ambiguous" refusal for a match that was never actually ambiguous.
    # Deduping per-member (casefolded, so "SuperHuyGaming"/"superhuygaming"
    # collide too) keeps each member contributing only genuinely distinct
    # name strings.
    candidate_members = []
    candidate_names = []
    for m in guild.members:
        seen_for_member = set()
        for name in (m.display_name, getattr(m, "global_name", None), m.name):
            if isinstance(name, str) and name and name.casefold() not in seen_for_member:
                seen_for_member.add(name.casefold())
                candidate_members.append(m)
                candidate_names.append(name)

    if candidate_names:
        # Try the real name, the raw login, AND a synthesized "real name
        # (login)" combination as separate queries, keeping whichever scores
        # highest -- same "try every candidate string, keep the single best
        # score" shape as _find_github_username_via_org_roster. Real gaps
        # this closes: a formal real name ("Ricardo Beale") can fail to clear
        # the fuzzy-match threshold against an unrelated-looking Discord
        # display name, while the bare login ("RiccoWrld") matches a Discord
        # username ("riccowrld") trivially -- people often reuse the same
        # handle across platforms even when their GitHub profile name is
        # fully spelled out. The combined form additionally catches a Discord
        # display name that itself embeds BOTH pieces (a not-uncommon nickname
        # shape like "Ricardo Beale (riccowrld)" or "Ricardo | riccowrld"),
        # which the containment/token-overlap rules in identity_match.py can
        # match even when neither name nor login alone clears the threshold.
        candidate_queries = [real_name, login]
        if real_name and login:
            candidate_queries.append(f"{real_name} {login}")
        if github_email:
            # GitHub's own public profile email, if set -- same "local part is
            # often the real name concatenated" signal already used for the
            # Plaky-confirmed email fallback below, but sourced directly from
            # GitHub itself rather than requiring the Plaky hop to even run.
            candidate_queries.append(github_email.split("@", 1)[0])

        # Score PER MEMBER (the max across that member's own distinct name
        # fields and every query signal), THEN apply ambiguity-refusal only
        # ACROSS different members -- not by flattening every member's name
        # variants into one shared list and letting best_match's tie-check
        # run over it. Real incident the per-member dedup above didn't fully
        # cover: a member's display_name ("Asmita N.") and username
        # ("asmita_n_handle") are different strings that can EACH
        # independently score against the same query -- a flattened list
        # would see two "different" top-scoring candidates tied and refuse,
        # even though they're the same person. Scoring each candidate string
        # via a single-candidate best_match call (nothing else in that call
        # to be ambiguous against) and taking the max per member sidesteps
        # this entirely; genuine ambiguity between two DIFFERENT people is
        # still caught by the cross-member check below.
        per_member_best: Dict[int, tuple] = {}
        for member_obj, name in zip(candidate_members, candidate_names):
            for query in candidate_queries:
                if not query:
                    continue
                m = best_match(query, [name])
                if m is None:
                    continue
                current = per_member_best.get(member_obj.id)
                if current is None or m.score > current[0]:
                    per_member_best[member_obj.id] = (m.score, query, member_obj)

        if per_member_best:
            ranked = sorted(per_member_best.values(), key=lambda t: t[0], reverse=True)
            top_score, top_query, top_member = ranked[0]
            ambiguous = len(ranked) > 1 and (top_score - ranked[1][0]) < AMBIGUITY_MARGIN
            if not ambiguous:
                await _remember_identity(top_member.id, login, top_member)
                logger.info("PR staleness identity: matched GitHub %s (query %r, score=%s) -> Discord %s via name fuzzy match", login, top_query, top_score, top_member.id)
                return top_member

        # No confident match despite trying every signal -- log what was
        # actually compared instead of failing silently into the Plaky hop.
        # A truncated sample isn't enough to actually answer "is this person
        # in the roster at all" (a real incident: a 20-name sample out of 134
        # looked like a miss but said nothing about the other 114) -- do an
        # explicit raw substring scan of the FULL candidate list for anything
        # resembling either query, so the log gives a definitive yes/no
        # instead of a coin-flip on where a truncated sample happened to cut
        # off. This is deliberately looser than best_match's scoring (raw
        # casefolded substring, no length floor) -- it's a diagnostic, not a
        # decision, so it's fine if it's noisier than the real matcher.
        near_hits = []
        for query in candidate_queries:
            if not query or len(query) < 3:
                continue
            q_lower = re.sub(r"[^a-z0-9]", "", query.lower())
            for candidate in candidate_names:
                c_lower = re.sub(r"[^a-z0-9]", "", candidate.lower())
                if q_lower and c_lower and (q_lower in c_lower or c_lower in q_lower) and candidate not in near_hits:
                    near_hits.append(candidate)
        logger.info(
            "PR staleness identity: no confident Discord name match for GitHub %s (queries tried: %s) against %s candidates from %s guild members. Raw substring near-hits (should have matched if present): %s. Full candidate list: %s",
            login, [q for q in candidate_queries if q], len(candidate_names), len(guild.members), near_hits or "NONE FOUND -- this person's name/login does not appear anywhere in the guild roster we searched", candidate_names,
        )
    else:
        logger.info("PR staleness identity: guild has no usable member name candidates at all (guild.members len=%s, chunked=%s)", len(guild.members), getattr(guild, "chunked", "?"))

    if PLAKY_API_KEY:
        # Same widened candidate set as the Discord-side match above -- real
        # name, raw login, AND the synthesized combination -- so the Plaky
        # hop gets the same "whatever signal actually matches" treatment
        # instead of only the two standalone strings. find_user_email already
        # tries every candidate in the list and keeps the single best score.
        plaky_candidates = [n for n in (real_name, login) if n]
        if real_name and login:
            plaky_candidates.append(f"{real_name} {login}")
        # GitHub's own public profile email (if set) feeds Plaky's exact-email
        # fast path -- a far stronger signal than any fuzzy name match, and
        # sourced directly from GitHub rather than needing Plaky to separately
        # rediscover it by name.
        known_emails = [github_email] if github_email else None
        plaky_email = await asyncio.to_thread(find_user_email, plaky_candidates, PLAKY_API_KEY, known_emails)
        if plaky_email:
            discord_id_str = await find_discord_id_by_email(plaky_email)
            if discord_id_str:
                try:
                    member = guild.get_member(int(discord_id_str))
                except ValueError:
                    member = None
                if member is not None:
                    await _remember_identity(member.id, login, member)
                    logger.info("PR staleness identity: matched GitHub %s -> Plaky email %s -> Discord %s", login, plaky_email, member.id)
                    return member

            # find_discord_id_by_email only finds a hit if someone self-reported
            # this exact email at onboarding -- but Plaky itself already
            # confirmed the email belongs to this person (a real, independent
            # identity source, not a guess), so a missing self-report shouldn't
            # be a dead end. The email's local part is very often the person's
            # real name concatenated (e.g. "minhhuyngoctruong@gmail.com" for
            # "Huy Truong") -- worth one more fuzzy-match pass against the same
            # guild roster before giving up.
            if candidate_names:
                email_local_part = plaky_email.split("@", 1)[0]
                m = best_match(email_local_part, candidate_names)
                if m is not None:
                    member = candidate_members[m.index]
                    await _remember_identity(member.id, login, member)
                    logger.info("PR staleness identity: matched GitHub %s -> Plaky email %s local-part -> Discord %s", login, plaky_email, member.id)
                    return member

    logger.warning("PR staleness identity: could not resolve GitHub login %s to any Discord member", login)
    return None


async def _resolve_pr_qa_reviewers(pr: dict, guild: discord.Guild) -> tuple:
    """Requested reviewers on the PR, resolved via the same GitHub->Discord
    identity chain used for the author (name fuzzy match, then Plaky email --
    no hardcoded username mapping). Returns (raw_logins, identity_map, qa_role_pairs):

    `raw_logins` is every requested-reviewer login exactly as GitHub reports
    it -- the ground truth for whether a PR has "Assigned QA" at all, so the
    staleness channel post never disagrees with what GitHub itself shows.

    `identity_map` is {login: discord.Member} for every login the identity
    chain actually resolved, regardless of Discord role -- used for display,
    so the channel post mentions a real person whenever we know who they are.
    A login missing from this map (chain found no match) falls back to the
    bare GitHub username in the post, never a hardcoded guess.

    `qa_role_pairs` is the subset of identity_map entries whose member also
    holds QA_ROLE_ID -- only that subset can be DMed as a QA reviewer nudge,
    since a stale/missing Discord role sync doesn't change who GitHub
    considers the assigned reviewer, only who we're able to notify in Discord.
    """
    full_pr = await asyncio.to_thread(get_pull_request, pr["repo"], pr["number"], GITHUB_PAT)
    if not full_pr:
        return [], {}, []
    requested = full_pr.get("requested_reviewers") or []
    logins = [r.get("login") for r in requested if r.get("login")]
    identity_map: Dict[str, discord.Member] = {}
    qa_role_pairs = []
    for login in logins:
        member = await _resolve_discord_member_for_github_login(login, guild)
        if member is None:
            continue
        identity_map[login] = member
        if member.get_role(QA_ROLE_ID) is not None:
            qa_role_pairs.append((login, member))
    return logins, identity_map, qa_role_pairs


async def _pr_already_reviewed_by(pr: dict, login: str) -> bool:
    """Any submitted review (approve/request-changes/comment) counts as
    "weighed in" -- they're off the nag list regardless of the review outcome."""
    reviews = await asyncio.to_thread(get_pull_request_reviews, pr["repo"], pr["number"], GITHUB_PAT)
    login_lower = login.strip().lower()
    return any((r.get("user") or {}).get("login", "").strip().lower() == login_lower for r in reviews)


async def _post_pr_staleness_qa_channel(pr: dict, raw_logins: list, identity_map: dict) -> None:
    repo, number, title, url = pr["repo"], pr["number"], pr["title"], pr["html_url"]
    channel = await _channel_from_id(PR_STALE_QA_CHANNEL_ID)
    if channel is None:
        return
    if raw_logins:
        assigned = ", ".join(
            identity_map[login].mention if login in identity_map else login
            for login in raw_logins
        )
        cta = "Let's get this merged when you can!"
    else:
        assigned = "No QA assigned"
        cta = "Let's get this assigned!"
    try:
        await channel.send(
            f"PR #{number} in {repo} (\"{title}\") has been open 2 weeks: {url}\nAssigned QA: {assigned}\n{cta}"
        )
    except Exception:
        logger.exception("Failed to post 2-week PR staleness notice for %s#%s", repo, number)


async def _dm_pr_staleness_nudge(member: discord.Member, pr: dict, *, as_reviewer: bool) -> None:
    repo, number, title, url = pr["repo"], pr["number"], pr["title"], pr["html_url"]
    if as_reviewer:
        text = (
            f"Hey — you're assigned as QA on PR #{number} in {repo} (\"{title}\") and it hasn't been "
            f"reviewed yet from your end. Take a look when you get a chance: {url}"
        )
    else:
        text = (
            f"Hey — your PR #{number} in {repo} (\"{title}\") is still open. "
            f"No pressure, just a nudge to take a look when you get a chance: {url}"
        )
    try:
        await member.send(text)
    except Exception:
        logger.exception("Failed to DM PR staleness nudge to %s for %s#%s", member.id, repo, number)


async def _post_pr_staleness_1month(pr: dict, member: Optional[discord.Member]) -> None:
    repo, number, title, url = pr["repo"], pr["number"], pr["title"], pr["html_url"]
    channel = await _channel_from_id(ANNOUNCEMENTS_CHANNEL_ID)
    if channel is None:
        return
    embed = discord.Embed(
        title="PR open over 2.5 months",
        description=f"[#{number} in {repo}]({url})\n\n{title}",
        color=discord.Color.red(),
    )
    mention = member.mention if member is not None else None
    try:
        await channel.send(content=mention, embed=embed)
    except Exception:
        logger.exception("Failed to post 1-month PR staleness notice for %s#%s", repo, number)


def _cooldown_elapsed(last_sent_iso: Optional[str], now: datetime, cadence_days: float) -> bool:
    if not last_sent_iso:
        return True
    try:
        last_sent = datetime.fromisoformat(last_sent_iso.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - last_sent).total_seconds() / 86400.0 >= cadence_days


async def _scan_stale_prs(guild: discord.Guild) -> None:
    """Runs periodically over every open PR org-wide. Bot-authored and draft
    PRs are skipped entirely -- a bot has no Discord identity, and a draft
    isn't yet asking for review.

    Three independent things happen as a PR ages:
    - At 2 weeks: #qa-support-team gets posted once (includes assigned QA, or
      "No QA assigned").
    - At 2.5 weeks: the author starts getting DMed on a cadence that tightens
      with age (weekly -> every 3 days at 3 weeks -> daily at 1 month+), not
      just once.
    - Also from 2.5 weeks: any requested reviewer holding the QA role who
      hasn't reviewed yet gets the same recurring DM on the same cadence,
      independently of whether it's their PR -- a separate DM thread from the
      author's.

    #announcements at 2.5 months is the only one-time-forever tier -- it never
    repeats no matter how much older the PR gets.
    """
    if not GITHUB_ORG or not GITHUB_PAT:
        logger.warning("PR staleness scan skipped: GITHUB_ORG or GITHUB_PAT not configured")
        return
    prs = await asyncio.to_thread(list_open_prs, GITHUB_ORG, GITHUB_PAT)
    if not prs:
        return

    now = datetime.now(timezone.utc)
    for pr in prs:
        repo_name = (pr.get("repo") or "").split("/")[-1].strip().lower()
        author_login = pr.get("author_login") or ""
        if repo_name in PR_STALE_EXCLUDED_REPOS:
            continue
        if author_login.strip().lower() in PR_STALE_EXCLUDED_AUTHORS_PER_REPO.get(repo_name, set()):
            continue
        if pr.get("draft"):
            continue
        if author_login.endswith("[bot]"):
            continue
        created_at_raw = pr.get("created_at")
        if not created_at_raw:
            continue
        try:
            created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        age_days = (now - created_at).total_seconds() / 86400.0
        if age_days < PR_STALE_2WEEK_DAYS:
            continue

        state = await load_pr_staleness(pr["repo"], pr["number"])
        member = None
        if state["resolved_discord_id"]:
            try:
                member = guild.get_member(int(state["resolved_discord_id"]))
            except ValueError:
                member = None
        if member is None and author_login:
            member = await _resolve_discord_member_for_github_login(author_login, guild)
            if member is not None:
                await save_pr_staleness(pr["repo"], pr["number"], resolved_discord_id=str(member.id))

        raw_qa_logins, qa_identity_map, qa_reviewers = await _resolve_pr_qa_reviewers(pr, guild)

        if not state["notified_2week"]:
            await _post_pr_staleness_qa_channel(pr, raw_qa_logins, qa_identity_map)
            await save_pr_staleness(pr["repo"], pr["number"], notified_2week=True)

        if age_days >= PR_STALE_2_5WEEK_DAYS:
            cadence = _pr_stale_dm_cadence_days(age_days)
            if member is not None and _cooldown_elapsed(state["last_author_dm_at"], now, cadence):
                await _dm_pr_staleness_nudge(member, pr, as_reviewer=False)
                await save_pr_staleness(pr["repo"], pr["number"], last_author_dm_at=now.isoformat())

            reviewer_dm_state = dict(state["reviewer_dm_state"])
            reviewer_state_changed = False
            for login, reviewer_member in qa_reviewers:
                if await _pr_already_reviewed_by(pr, login):
                    continue
                if _cooldown_elapsed(reviewer_dm_state.get(login), now, cadence):
                    await _dm_pr_staleness_nudge(reviewer_member, pr, as_reviewer=True)
                    reviewer_dm_state[login] = now.isoformat()
                    reviewer_state_changed = True
            if reviewer_state_changed:
                await save_pr_staleness(pr["repo"], pr["number"], reviewer_dm_state=reviewer_dm_state)

        if age_days >= PR_STALE_ANNOUNCE_DAYS and not state["notified_1month"]:
            # Atomically claim the one-time announcement slot before posting --
            # two overlapping scan loops (e.g. during a Render redeploy) must
            # never both read notified_1month=False and both post.
            if await claim_pr_staleness_1month(pr["repo"], pr["number"]):
                await _post_pr_staleness_1month(pr, member)


async def _pr_staleness_scan_loop() -> None:
    while True:
        try:
            guild = await _get_primary_guild()
            if guild is not None:
                if not guild.chunked:
                    # The very first iteration after every restart/reconnect
                    # otherwise runs within ~10-15s of the gateway connecting --
                    # well before Discord has finished delivering the full
                    # member list for a guild this size. guild.members would
                    # then silently be missing whoever hasn't arrived yet,
                    # producing a CONSISTENT (not random) identity-resolution
                    # failure for specific people on every single restart,
                    # regardless of how good the fuzzy-name-matching logic is.
                    # Real incident: SuperHuyGaming/Huy Truong failed to
                    # resolve on the first scan after every deploy even after
                    # the matching logic itself was fixed and verified correct.
                    try:
                        await guild.chunk(cache=True)
                    except Exception:
                        logger.exception("PR staleness scan: failed to chunk guild %s members before scanning", guild.id)
                await _scan_stale_prs(guild)
        except Exception:
            logger.exception("PR staleness scan iteration failed")
        await asyncio.sleep(PR_STALE_SCAN_INTERVAL_SECONDS)


async def _run_security_assessment(guild: discord.Guild) -> Dict[str, Dict[str, object]]:
    return await asyncio.to_thread(
        run_full_assessment,
        github_org=GITHUB_ORG or "",
        github_pat=GITHUB_PAT or "",
        plaky_api_key=PLAKY_API_KEY or "",
        guild=guild,
        website_url=SECURITY_ASSESSMENT_WEBSITE_URL,
        email_domain=SECURITY_ASSESSMENT_EMAIL_DOMAIN,
    )


async def _post_security_digest(results: Dict[str, Dict[str, object]]) -> None:
    channel = await _channel_from_id(STAFF_CHANNEL_ID)
    if channel is None:
        logger.warning("Security assessment digest not posted: STAFF_CHANNEL_ID not resolved")
        return
    status = overall_status(results)
    color = {
        "critical": discord.Color.red(),
        "warning": discord.Color.orange(),
        "ok": discord.Color.green(),
    }.get(status, discord.Color.light_grey())
    embed = discord.Embed(
        title="Routine security assessment",
        description="\n".join(render_digest_lines(results)),
        color=color,
    )
    embed.set_footer(text="Run /security-check for an in-depth report.")
    role_mention = f"<@&{IT_OPERATIONS_SUPPORT_ROLE_ID}>" if status == "critical" and IT_OPERATIONS_SUPPORT_ROLE_ID is not None else None
    try:
        await channel.send(content=role_mention, embed=embed)
    except Exception:
        logger.exception("Failed to post security assessment digest")


def _build_indepth_security_embed(title: str, results: Dict[str, Dict[str, object]]) -> discord.Embed:
    status = overall_status(results)
    color = {
        "critical": discord.Color.red(),
        "warning": discord.Color.orange(),
        "ok": discord.Color.green(),
    }.get(status, discord.Color.light_grey())
    embed = discord.Embed(title=title, color=color)
    for block in render_indepth_report(results):
        heading, _, body = block.partition("\n")
        embed.add_field(name=heading[:256], value=(body or "—")[:1024], inline=False)
    return embed


async def _dm_security_digest_to_it_team(results: Dict[str, Dict[str, object]], guild: discord.Guild) -> int:
    """DMs every IT_OPERATIONS_SUPPORT_ROLE_ID member the full in-depth
    results directly -- not just a one-line-per-category summary -- on a
    weekly cadence, regardless of whether anyone ran /security-check. Manual
    routine checks stay expected of the team; this guarantees everyone gets
    the actual findings in their DMs every week rather than having to go
    looking for them."""
    if IT_OPERATIONS_SUPPORT_ROLE_ID is None:
        return 0
    role = guild.get_role(IT_OPERATIONS_SUPPORT_ROLE_ID)
    if role is None:
        return 0
    embed = _build_indepth_security_embed("Weekly security assessment — full results", results)
    sent = 0
    for member in role.members:
        if member.bot:
            continue
        try:
            await member.send(embed=embed)
            sent += 1
        except Exception:
            logger.warning("Could not DM weekly security digest to %s (%s)", member, member.id)
    return sent


_SECURITY_DIGEST_STATE_KEY = "norozo_security_digest_last_sent_date"
_SECURITY_DM_STATE_KEY = "norozo_security_dm_last_sent_date"


async def _security_assessment_loop() -> None:
    """Hourly tick that fires the twice-weekly #it-notifications digest and
    the weekly IT-team DM at most once per calendar day each, tracked via the
    same durable key/value state store as the PR-staleness checkpoint so a
    restart mid-day doesn't cause a duplicate post."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour == SECURITY_ASSESSMENT_RUN_HOUR_UTC:
                today = now.date().isoformat()
                guild = await _get_primary_guild()
                if guild is not None:
                    if now.weekday() in SECURITY_ASSESSMENT_DIGEST_WEEKDAYS and await load_state(_SECURITY_DIGEST_STATE_KEY) != today:
                        results = await _run_security_assessment(guild)
                        await _post_security_digest(results)
                        await save_state(_SECURITY_DIGEST_STATE_KEY, today)

                    if now.weekday() == SECURITY_ASSESSMENT_DM_WEEKDAY and await load_state(_SECURITY_DM_STATE_KEY) != today:
                        results = await _run_security_assessment(guild)
                        await _dm_security_digest_to_it_team(results, guild)
                        await save_state(_SECURITY_DM_STATE_KEY, today)
        except Exception:
            logger.exception("Security assessment loop iteration failed")
        await asyncio.sleep(SECURITY_ASSESSMENT_LOOP_INTERVAL_SECONDS)


async def _forward_announcement_to_platform(message: discord.Message) -> None:
    if not PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL:
        return
    if not PLATFORM_ANNOUNCEMENTS_SECRET:
        logger.error("Announcement forward disabled: PLATFORM_ANNOUNCEMENTS_WEBHOOK_SECRET is not configured")
        return
    title = format_discussion_title(resolve_discord_mentions(message, message.content or ""))
    body = format_discussion_body(message)
    # Embed color (e.g. the PR-staleness 1-month red alert) doesn't live in
    # message.content -- pull it off the first embed so the web page can show
    # the same color bar Discord shows, instead of flattening everything to plain text.
    color_hex = None
    if message.embeds:
        embed_color = message.embeds[0].color
        if embed_color is not None:
            color_hex = f"#{embed_color.value:06x}"
    payload = {
        "source": "discord",
        "discord_message_id": str(message.id),
        "discord_channel_id": str(getattr(message.channel, "id", "")),
        "author": str(message.author),
        "author_id": str(getattr(message.author, "id", "")),
        "title": title,
        "body": body,
        "content": message.content or "",
        "color": color_hex,
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


def _is_staff_or_security_ops(member: discord.Member) -> bool:
    """Shared gate for anything sensitive enough to require admins or Security &
    Operations Support specifically -- /ipca-signed (grants DEV Team + Available
    roles on approval) and the "kick out <name>" command (Discord kick + GitHub
    org removal) both use this, so a plain STAFF_ROLE_ID member without the
    security/ops role can't self-serve either escalation path."""
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

    # Build name fallback maps for when role IDs not configured
    added_roles = [r for r in after.roles if r.id in added]
    added_names_lower = {r.name.strip().lower() for r in added_roles}

    # Deliberately name-matched rather than a hardcoded QA_ROLE_ID -- QA_ROLE_ID
    # is optional config used elsewhere (PR-staleness reviewer pings) and often
    # isn't set at all, so the GitHub support-team sync must not depend on it.
    # Matches "QA", "QA Engineer", "Quality Assurance", etc. by role name alone.
    qa_triggered = any(_is_qa_role_name(r.name) for r in added_roles)
    qa_lead_triggered = any(_is_qa_lead_role_name(r.name) for r in added_roles)

    it_triggered = False
    if IT_OPERATIONS_SUPPORT_ROLE_ID is not None and IT_OPERATIONS_SUPPORT_ROLE_ID in added:
        it_triggered = True
    elif IT_OPERATIONS_SUPPORT_ROLE_ID is None:
        # Fallback by name: check for pink IT role variants
        it_candidates = {"it operations support", "support operations", "it", "it-management", "security it", "it operations", "support operations and security it"}
        if added_names_lower & it_candidates:
            it_triggered = True

    if not qa_triggered and not it_triggered:
        return

    # QA -> support-team: "QA Lead" gets maintainer access on the team, every
    # other QA-shaped role name ("QA Engineer", bare "QA", ...) gets plain
    # member access. No identity-chain/invite needed here, unchanged from
    # before -- only the IT branch below gained the full chain.
    if qa_triggered:
        qa_role = "maintainer" if qa_lead_triggered else "member"
        github_username = _get_github_username_for_member(after)
        if not github_username:
            logger.info("Member %s gained QA role but no GitHub username mapping found, skipping team sync", after.id)
        else:
            logger.info("Syncing %s (%s) to GitHub team %s for QA role as %s", after, github_username, GITHUB_SUPPORT_TEAM_SLUG, qa_role)
            try:
                result = await asyncio.to_thread(
                    add_user_to_team,
                    username=github_username,
                    github_org=GITHUB_ORG,
                    github_pat=GITHUB_PAT,
                    team_slug=GITHUB_SUPPORT_TEAM_SLUG,
                    role=qa_role,
                )
                if not result.get("ok"):
                    logger.warning("Failed to add %s to support team: %s", github_username, result.get("message"))
            except Exception:
                logger.exception("Exception syncing QA to GitHub team")

    # IT Operations / Support Operations -> it-management-team, as a maintainer.
    # Runs the full GitHub-identity resolution chain (stored mapping ->
    # #github-profiles scan -> org-roster fuzzy match) rather than giving up
    # when there's no mapping yet, sends the actual org invite (not just the
    # team-membership PUT, which also invites but silently), and DMs the
    # member either the invite confirmation or an ask for their username when
    # every resolution source came up empty.
    if it_triggered:
        github_username = await _resolve_github_username_for_role_sync(after)
        if not github_username:
            logger.info("Member %s gained IT/Support Operations role but no GitHub username could be resolved, asking them directly", after.id)
            try:
                await after.send(_github_it_team_dm_ask_text(getattr(after, "display_name", None) or getattr(after, "name", None) or "there"))
            except discord.Forbidden:
                logger.warning("Could not DM %s to ask for their GitHub username — DMs disabled", after.id)
            except Exception:
                logger.exception("Failed to DM %s the GitHub username ask", after.id)
            return

        logger.info("Syncing %s (%s) to GitHub org %s + team %s (maintainer) for IT role", after, github_username, GITHUB_ORG, GITHUB_IT_TEAM_SLUG)
        try:
            invite_result = await asyncio.to_thread(
                invite_user,
                username=github_username,
                github_org=GITHUB_ORG,
                github_pat=GITHUB_PAT,
            )
            if not invite_result.get("ok") and invite_result.get("status") != 422:
                # 422 just means they're already a member/already invited -- expected
                # on every re-sync after the first, not worth a warning.
                logger.warning("GitHub org invite failed for %s: %s", github_username, invite_result.get("message"))

            team_result = await asyncio.to_thread(
                add_user_to_team,
                username=github_username,
                github_org=GITHUB_ORG,
                github_pat=GITHUB_PAT,
                team_slug=GITHUB_IT_TEAM_SLUG,
                role="maintainer",
            )
            if not team_result.get("ok"):
                logger.warning("Failed to add %s to IT team as maintainer: %s", github_username, team_result.get("message"))
                return
        except Exception:
            logger.exception("Exception syncing IT role to GitHub org/team")
            return

        try:
            await after.send(
                f"You've been invited to the Deepiri GitHub org and added as a **maintainer** on "
                f"`{GITHUB_IT_TEAM_SLUG}` (GitHub: `{github_username}`). Check your GitHub notifications "
                "or https://github.com/settings/organizations to accept the invite."
            )
        except discord.Forbidden:
            pass
        except Exception:
            logger.exception("Failed to DM %s the GitHub IT team invite confirmation", after.id)

        if STAFF_CHANNEL_ID is not None:
            staff_channel = await _channel_from_id(STAFF_CHANNEL_ID)
            if staff_channel:
                try:
                    await staff_channel.send(
                        f"Auto-invited `{github_username}` ({after.mention}) to GitHub org `{GITHUB_ORG}` "
                        f"and added as maintainer on `{GITHUB_IT_TEAM_SLUG}` (IT/Support Operations role grant)."
                    )
                except Exception:
                    logger.warning("Could not post GitHub IT auto-invite log to staff channel %s", STAFF_CHANNEL_ID)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    content = message.content or ""

    await notify_support_team_for_message(message)
    if _is_announcements_channel(message.channel):
        title = format_discussion_title(resolve_discord_mentions(message, message.content or ""))
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
        _remember_user_data(interaction.user.id, github_username=normalized_username)
        await _remember_identity(interaction.user.id, normalized_username, interaction.user if isinstance(interaction.user, discord.Member) else None)
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

    # Kick out of Plaky too (best-effort, resolved via _resolve_member_email_for_plaky).
    plaky_note = "no email on file, skipped"
    if isinstance(member, discord.Member):
        user_email = await _resolve_member_email_for_plaky(member, normalized_username)
        if user_email:
            plaky_result = await call_plaky_bridge_kick(user_email)
            if plaky_result.get("success"):
                plaky_note = f"removed {user_email}"
            else:
                plaky_note = f"{user_email} — {plaky_result.get('error')}"
                logger.warning("Plaky kick failed for %s during offboard: %s", user_email, plaky_result.get("error"))

    await interaction.edit_original_response(
        content=f"Offboarding completed for {getattr(member, 'mention', normalized_username)} ({plaky_note})."
    )


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

    # Also kick out of Plaky if we can resolve their email (best-effort).
    user_email = await _resolve_member_email_for_plaky(member)
    if user_email:
        plaky_result = await call_plaky_bridge_kick(user_email)
        if not plaky_result.get("success"):
            logger.warning("Plaky kick failed for %s via /discord-kick: %s", user_email, plaky_result.get("error"))

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


def _retirement_notice_text(display_name: str) -> str:
    return (
        f"Dear {display_name},\n\n"
        "This confirms your retirement from the Deepiri project, effective immediately, "
        "per your own request. Thank you for your contributions.\n\n"
        "As with any departure, the following terms apply:\n"
        "1. Cessation of Representation\n\n"
        "Effective immediately, you may not represent yourself as:\n\n"
        "    A current contributor to Deepiri\n\n"
        "    Acting on behalf of Deepiri\n\n"
        "    Affiliated with Deepiri in any ongoing capacity\n\n"
        "You're welcome to update LinkedIn, résumés, and other public profiles to reflect "
        "your past involvement -- any description of it must be accurate and must not imply "
        "ongoing affiliation, authority, or endorsement.\n"
        "2. Access and Assets\n\n"
        "Your access to Deepiri systems, repositories, accounts, credentials, and internal "
        "tools has been removed.\n\n"
        "If you possess any materials that were explicitly designated as private and not "
        "publicly released under Apache 2.0, those materials must be deleted or returned in "
        "accordance with Sections 11 and 12 of the Deepiri Contributor and Intellectual "
        "Property Agreement. This does not apply to publicly released open-source "
        "repositories governed by Apache 2.0.\n"
        "3. Continuing Obligations\n\n"
        "All confidentiality provisions remain in effect with respect to any non-public "
        "materials previously accessed.\n\n"
        "Thanks again for everything you built here -- if you have questions, please submit "
        "them in writing.\n\n"
        "Sincerely,\n"
        "Deepiri Management"
    )


async def _send_offboarding_notice(
    target: discord.Member, github_username: Optional[str], *, subject: str, body: str
) -> str:
    """Resolve an email for the departing member (self-reported at join -> GitHub
    public email -> best-effort Plaky lookup -> Discord DM as last resort) and
    send the given notice. Returns a short human-readable outcome string for the
    calling flow's summary. Shared by both the involuntary kick-out path
    (termination notice) and the voluntary retirement path (retirement notice)."""
    # Check the dynamic identity cache first -- if this person ever self-reported
    # a GitHub link, their real name was captured then, independent of whether
    # github_username (resolved just now, possibly from a stylized handle that
    # doesn't fuzzy-match anything) resolves at all this time.
    cached_profile = await load_member_profile(target.id)
    email = cached_profile["email"]
    github_real_name = cached_profile["real_name"]
    if not github_username:
        github_username = cached_profile["github_username"]

    if not email and github_username:
        profile = await asyncio.to_thread(get_user_profile, github_username, GITHUB_PAT)
        email = profile.get("email")
        github_real_name = github_real_name or profile.get("name")
        if not email:
            logger.info("Termination notice: no public GitHub email for %s (real name on profile: %r)", github_username, github_real_name)
    if not email and not PLAKY_API_KEY:
        logger.warning("Termination notice: PLAKY_API_KEY not configured, skipping Plaky lookup for %s", target.id)
    if not email and PLAKY_API_KEY:
        # Throw every known identifier at Plaky at once instead of trying one
        # candidate and giving up: the cached/fetched real display name (e.g.
        # login "riccorx" -> name "Ricardo Beale" -- the person's own
        # self-reported real name, a far stronger signal than any account
        # handle), the GitHub login itself, and every Discord identifier.
        # find_user_email picks the single best-scoring match across all of
        # them, not just the first candidate that happens to clear the threshold.
        candidates = [
            github_real_name,
            github_username,
            target.display_name,
            str(getattr(target, "global_name", "") or ""),
            str(target.name),
        ]
        logger.info("Termination notice: trying Plaky lookup for %s with candidates %s", target.id, candidates)
        email = await asyncio.to_thread(find_user_email, candidates, PLAKY_API_KEY)

    email_fail_reason = None
    if email:
        sent, email_fail_reason = await asyncio.to_thread(send_email, email, subject, body)
        if sent:
            return f"emailed to {email}"
        logger.warning("Termination email to %s failed to send (%s); falling back to DM for %s", email, email_fail_reason, target.id)
        # A credentials failure isn't specific to this one person -- it silently
        # breaks every future termination/retirement email until someone fixes
        # SMTP_PASSWORD, so this deserves a channel alert now rather than only
        # a per-kick "failed" note that's easy to shrug off as a one-off.
        if email_fail_reason and "credentials" in email_fail_reason.lower() and STAFF_CHANNEL_ID:
            alert_channel = await _channel_from_id(STAFF_CHANNEL_ID)
            if alert_channel is not None:
                try:
                    await alert_channel.send(
                        f"⚠️ Offboarding email to {email} failed: {email_fail_reason}. "
                        "This will keep failing for every future kick-out/retirement until SMTP_PASSWORD is refreshed."
                    )
                except Exception:
                    logger.exception("Failed to post SMTP credentials alert to #it-notifications")

    try:
        await target.send(f"**{subject}**\n\n{body}")
        if not email:
            return "no email found — sent via Discord DM"
        return f"email send failed ({email_fail_reason}) — sent via Discord DM instead"
    except Exception:
        logger.exception("Failed to DM termination notice to %s", target.id)
        return "could not deliver notice via email or DM"


async def _send_termination_notice(target: discord.Member, github_username: Optional[str]) -> str:
    return await _send_offboarding_notice(
        target,
        github_username,
        subject="Notice of Termination — Deepiri Contributor Agreement",
        body=_termination_notice_text(target.display_name),
    )


def _candidate_roles_by_category(guild: discord.Guild) -> "dict[str, discord.Role]":
    """One representative role per category, resolved live from the guild's actual
    role list every time (no IDs to configure/maintain). IT/Security & Operations
    Support is excluded before any fuzzy scoring runs -- never a self-service
    candidate, elevated permissions. Cloud/Infra/Security additionally requires
    "Engineer" in the name to qualify, per how that category is actually named
    in this server."""
    result: "dict[str, discord.Role]" = {}
    for role in guild.roles:
        name = role.name
        if _is_elevated_role(name):
            continue
        for label, patterns, requires_engineer in _ROLE_CATEGORY_PATTERNS:
            if label in result:
                continue
            if requires_engineer and not _ENGINEER_WORD_RE.search(name):
                continue
            if any(p.search(name) for p in patterns):
                result[label] = role
    return result


async def _maybe_handle_onboarding_dm(message: discord.Message) -> bool:
    """Classify a DM from (possibly) a new member, stateless: any DM at any time
    can be an email or a role pick, checked independently, no conversation state
    to track or lose on a restart. Only replies when the content plausibly looks
    like an attempt at one of these -- otherwise stays silent, so Norozo doesn't
    start responding to unrelated DMs with onboarding noise.
    """
    if message.guild is not None or message.author.bot:
        return False
    content = (message.content or "").strip()
    if not content:
        return False

    email_match = EMAIL_SEARCH_RE.search(content)
    if email_match:
        email = email_match.group(0)
        ok = await save_member_email(message.author.id, str(message.author), email)
        if ok:
            remember_user_data(message.author.id, email=email)
            await message.channel.send(f"Got it — saved {email} on file. Thanks!")
        else:
            logger.error("Failed to persist member email for %s", message.author.id)
            await message.channel.send("Got your email but couldn't save it right now — please try sending it again shortly.")
        return True

    # Self-reported GitHub username, most reliable source there is for kick-out's
    # GitHub org removal — checked ahead of the #github-profiles channel scan and
    # the display-name guess heuristic (_get_github_username_for_member already
    # checks this mapping first). Only run this when the message actually
    # contains a URL — _extract_github_profile_username's real host validation
    # (host == "github.com" after urlparse) already lives downstream and is what
    # actually enforces the domain; this is just a gate to skip that function's
    # bare-word fallback, which would otherwise swallow a plain role pick like
    # "Backend" or "AI" as if it were a username before role-matching ever ran.
    # (Deliberately not a domain substring check — "notgithub.com.evil.com"
    # contains "github.com" too, which is exactly the pattern CodeQL flags.)
    github_username = _extract_github_profile_username(content) if URL_RE.search(content) else None
    if github_username:
        # _remember_identity captures their real name right now too, while
        # it's a certain, self-reported signal -- not a fuzzy guess later.
        # This is the dynamic alias table: no one hand-types a nickname
        # mapping, it's recorded the moment it's known, so a kick-out for a
        # stylized handle months later is a cache hit on the real name
        # instead of a string-similarity gamble on the handle itself. Same
        # helper also runs from every other place a GitHub username gets
        # resolved (org-roster fuzzy match, #github-profiles scan, PR-staleness
        # reverse lookup), so this fills in for members who joined before it
        # existed too, not just fresh onboarding.
        await _remember_identity(message.author.id, github_username, message.author)
        remember_user_data(message.author.id, github_username=github_username)
        await message.channel.send(f"Got it — linked your GitHub as **{github_username}**.")
        return True

    guild = await _get_primary_guild()
    if guild is None:
        return False
    member = guild.get_member(message.author.id)
    if member is None:
        return False

    candidates = _candidate_roles_by_category(guild)
    labels = list(candidates.keys())
    match = best_match(content, labels) if labels else None

    if match is None and _is_elevated_role(content):
        # Only reached when nothing in the real, self-assignable candidate list
        # matched -- "Security" alone should (and does) resolve to Cloud/Infra/
        # Security Engineer above before ever reaching this fallback, since that
        # role isn't in `labels` to begin with and can't compete for the match.
        await message.channel.send(
            "The IT / Security & Operations Support role isn't self-assignable "
            "(it has elevated permissions) — please contact staff directly if you need it."
        )
        return True

    if match is not None:
        role = candidates[labels[match.index]]
        if role in member.roles:
            await message.channel.send(f"You already have the {role.name} role.")
            return True
        try:
            await member.add_roles(role, reason="Self-selected via onboarding DM")
        except Exception:
            logger.exception("Failed to add role %s to %s via onboarding DM", role.id, member.id)
            await message.channel.send(f"Matched you to {role.name} but couldn't assign it — please contact staff.")
            return True
        await message.channel.send(f"Added you to the {role.name} role!")
        return True

    if _ROLE_ATTEMPT_HINT_RE.search(content):
        options = ", ".join(labels) if labels else "no roles currently configured"
        await message.channel.send(
            f"Couldn't confidently match a role from that. Options: {options}. "
            "Try replying with just the team name, e.g. \"Backend\"."
        )
        return True

    return False


async def _resolve_reply_channel(message: discord.Message):
    """support-tickets uses Discord's auto-thread feature: the triggering message
    lives in the parent channel and spawns a same-id companion thread as a SIDE
    EFFECT that can land after on_message already fired. message.thread captured
    once at handler-start can still be None even though the thread exists by the
    time a handler is ready to reply — real async work (role checks, kicks,
    GitHub calls, email resolution) happens in between. Re-check fresh every
    time a reply is about to be sent instead of trusting a value cached earlier.
    Shared by both the IPCA auto-assign flow and the kick-out command — both
    hit this exact race, since both can fire on the message that itself spawns
    the companion thread.
    """
    if message.thread is not None:
        return message.thread
    # A reply sent directly inside an already-open thread (not a message that
    # spawns a new companion thread) has message.channel be the Thread itself --
    # nothing left to resolve.
    if isinstance(message.channel, discord.Thread):
        return message.channel
    cached = message.channel.get_thread(message.id) if hasattr(message.channel, "get_thread") else None
    if cached is not None:
        return cached
    try:
        fresh = await message.channel.fetch_message(message.id)
        if fresh.thread is not None:
            return fresh.thread
    except Exception:
        pass
    return message.channel


async def _close_ticket_thread(channel) -> None:
    """Closes a resolved support-ticket thread via Discord's own archive API.

    Confirmed correct via "probe needle" against a real ticket: Needle's own
    ticket-panel message has an "Archive thread" button (custom_id="close",
    ButtonStyle.success) -- there's no separate Needle-internal close
    mechanism to reverse-engineer, that button's whole job is exactly the
    Discord archive call this makes. (A "/close" text command, tried
    earlier, was never it -- Discord slash commands only fire through a real
    user interaction, and there's no supported way for one bot to invoke
    another bot's slash command or press a button on another bot's message.)

    Archiving does hide the thread from the sidebar unless "Archived
    Threads" is expanded -- that's expected once a ticket is actually
    closed, the same as clicking Needle's own button would do, not a bug.
    """
    if not isinstance(channel, discord.Thread):
        return
    try:
        await channel.edit(archived=True, locked=False, reason="Ticket resolved")
    except discord.Forbidden:
        logger.error("No permission to archive ticket thread %s (check Manage Threads)", channel.id)
    except Exception:
        logger.exception("Failed to archive ticket thread %s", channel.id)


PROBE_NEEDLE_COMMAND_RE = re.compile(r"^\s*probe\s+needle\s*$", re.IGNORECASE)


async def _maybe_handle_probe_needle_command(message: discord.Message) -> bool:
    """Staff-only diagnostic: "probe needle" typed in a ticket thread dumps
    every bot-authored message in that thread -- content, embed titles/
    descriptions/fields, and every button/select component's label + style +
    custom_id -- to whoever ran it, via DM. This is how to actually find out
    what Needle expects to close a ticket (a button on its own ticket-panel
    message, a reaction, particular text, ...) instead of guessing again:
    real observed data from Needle's own posts, not another assumption.
    """
    if message.guild is None or not isinstance(message.channel, discord.Thread):
        return False
    if not PROBE_NEEDLE_COMMAND_RE.match(message.content or ""):
        return False
    if not isinstance(message.author, discord.Member) or not _is_staff_or_security_ops(message.author):
        return False

    thread = message.channel
    lines = [f"**Needle probe -- #{thread.name} ({thread.id})**\n"]
    found_any = False
    try:
        async for msg in thread.history(limit=200, oldest_first=True):
            if not msg.author.bot:
                continue
            found_any = True
            lines.append(f"--- message {msg.id} by {msg.author} ({msg.author.id}) ---")
            if msg.content:
                lines.append(f"content: {msg.content[:500]!r}")
            for embed in msg.embeds:
                lines.append(f"embed: title={embed.title!r} description={(embed.description or '')[:300]!r}")
                for field in embed.fields:
                    lines.append(f"  field: name={field.name!r} value={(field.value or '')[:200]!r}")
            for row in msg.components:
                for child in getattr(row, "children", []):
                    lines.append(
                        f"component: type={type(child).__name__} label={getattr(child, 'label', None)!r} "
                        f"style={getattr(child, 'style', None)!r} custom_id={getattr(child, 'custom_id', None)!r} "
                        f"url={getattr(child, 'url', None)!r}"
                    )
            reactions = [(str(r.emoji), r.count) for r in msg.reactions]
            if reactions:
                lines.append(f"reactions: {reactions}")
    except Exception:
        logger.exception("Failed to probe ticket thread %s for Needle diagnostics", thread.id)
        lines.append("(error scanning thread history -- see Norozo logs)")

    if not found_any:
        lines.append("(no bot-authored messages found in this thread)")

    dump = "\n".join(lines)
    try:
        for chunk_start in range(0, len(dump), 1900):
            await message.author.send(f"```\n{dump[chunk_start:chunk_start + 1900]}\n```")
    except discord.Forbidden:
        await thread.send(f"{message.author.mention} Couldn't DM you the probe results -- check your DM settings.")
        return True

    await thread.send(f"{message.author.mention} Sent you the Needle probe results via DM.")
    return True


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
    if not isinstance(message.author, discord.Member) or not _is_staff_or_security_ops(message.author):
        return False

    target = _resolve_kick_target(message, match.group(1))
    if target is None:
        await (await _resolve_reply_channel(message)).send(f"{message.author.mention} Couldn't find that member to kick.")
        return True
    if target.id == message.author.id:
        await (await _resolve_reply_channel(message)).send(f"{message.author.mention} You cannot kick yourself.")
        return True
    if target.guild_permissions.administrator or (STAFF_ROLE_ID is not None and target.get_role(STAFF_ROLE_ID) is not None):
        await (await _resolve_reply_channel(message)).send(f"{message.author.mention} Cannot kick an Admin/staff member this way.")
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
    if not github_username:
        github_username = await _find_github_username_via_org_roster(target)

    notice_outcome = await _send_termination_notice(target, github_username)

    discord_ok = True
    try:
        await target.kick(reason=reason)
    except discord.Forbidden:
        discord_ok = False
        await (await _resolve_reply_channel(message)).send(f"{message.author.mention} I don't have permission to kick {target.mention} (check my role position).")
    except Exception:
        discord_ok = False
        logger.exception("Failed to kick member %s via kick-out command", target.id)
        await (await _resolve_reply_channel(message)).send(f"{message.author.mention} Failed to kick {target.mention} from Discord.")

    github_ok = False
    github_note = "no mapped GitHub username, skipped"
    if github_username:
        org_result = remove_user_from_org(username=github_username, github_org=GITHUB_ORG, github_pat=GITHUB_PAT)
        github_ok = bool(org_result.get("ok"))
        github_note = github_username if github_ok else f"{github_username} — {org_result.get('message')}"
        if not github_ok:
            logger.warning("GitHub org removal failed for %s during kick-out: %s", github_username, org_result.get("message"))

    # Kick out of Plaky too. Resolve the email via the same chain used by the
    # termination notice (user_data -> member_emails -> GitHub -> Plaky), so a
    # Plaky kick isn't skipped whenever the email wasn't collected at onboarding.
    # Best-effort: a missing email shouldn't block the Discord/GitHub removal,
    # just note it in the summary.
    plaky_ok = False
    plaky_note = "no email on file, skipped"
    user_email = await _resolve_member_email_for_plaky(target, github_username)
    if user_email:
        plaky_result = await call_plaky_bridge_kick(user_email)
        plaky_ok = bool(plaky_result.get("success"))
        plaky_note = user_email if plaky_ok else f"{user_email} — {plaky_result.get('error')}"
        if not plaky_ok:
            logger.warning("Plaky kick failed for %s during kick-out: %s", user_email, plaky_result.get("error"))

    summary = (
        f"{'✅' if discord_ok else '⚠️'} Discord kick: {target} ({target.id})\n"
        f"{'✅' if github_ok else '⚠️'} GitHub org removal: {github_note}\n"
        f"{'✅' if plaky_ok else '⚠️'} Plaky removal: {plaky_note}\n"
        f"📧 Termination notice: {notice_outcome}"
    )
    plaky_summary = await _maybe_handle_plaky_kick_on_kickout(message, target)
    summary += f"\n📋 {plaky_summary}"
    summary_channel = await _resolve_reply_channel(message)
    await summary_channel.send(summary)
    if STAFF_CHANNEL_ID:
        log_channel = await _channel_from_id(STAFF_CHANNEL_ID)
        if log_channel:
            try:
                await log_channel.send(f"{message.author.mention} kicked {target} ({target.id}) via kick-out command.\n{summary}")
            except Exception:
                pass

    # Same as IPCA's resolve-and-close: the kick-out ticket is now handled,
    # close it out the way a human staffer would rather than leaving it open.
    await _close_ticket_thread(summary_channel)
    return True


async def _execute_retirement(target: discord.Member, guild: discord.Guild) -> str:
    """Same underlying offboarding as kick-out (Discord kick + GitHub org
    removal + notice), but framed as a voluntary retirement rather than a
    termination. Returns a human-readable summary."""
    github_username = _get_github_username_for_member(target)
    if github_username and not await asyncio.to_thread(is_org_member, github_username, GITHUB_ORG, GITHUB_PAT):
        github_username = None
    if not github_username:
        github_username = await _find_github_username_in_profiles_channel(target)
    if not github_username:
        github_username = await _find_github_username_via_org_roster(target)

    notice_outcome = await _send_offboarding_notice(
        target,
        github_username,
        subject="Retirement Confirmation — Deepiri",
        body=_retirement_notice_text(target.display_name),
    )

    discord_ok = True
    try:
        await target.kick(reason=f"Voluntary retirement, confirmed by {target}")
    except Exception:
        discord_ok = False
        logger.exception("Failed to kick retiring member %s", target.id)

    github_ok = False
    github_note = "no mapped GitHub username, skipped"
    if github_username:
        org_result = remove_user_from_org(username=github_username, github_org=GITHUB_ORG, github_pat=GITHUB_PAT)
        github_ok = bool(org_result.get("ok"))
        github_note = github_username if github_ok else f"{github_username} — {org_result.get('message')}"

    return (
        f"{'✅' if discord_ok else '⚠️'} Discord kick: {target} ({target.id})\n"
        f"{'✅' if github_ok else '⚠️'} GitHub org removal: {github_note}\n"
        f"📧 Retirement notice: {notice_outcome}"
    )


class RetirementConfirmView(discord.ui.View):
    """Sent as a DM to the person named as retiring -- only they can confirm,
    so a staff member naming someone else in chat can't force it through
    without that person's own say-so."""

    def __init__(self, target_id: int, origin_channel_id: Optional[int]):
        super().__init__(timeout=24 * 60 * 60)
        self.target_id = target_id
        self.origin_channel_id = origin_channel_id

    async def _disable(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if interaction.message:
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Confirm Retirement", style=discord.ButtonStyle.danger, custom_id="retirement_confirm")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user is None or interaction.user.id != self.target_id:
            await interaction.response.send_message("This confirmation isn't yours to click.", ephemeral=True)
            return

        await interaction.response.defer()
        await self._disable(interaction)

        guild = await _get_primary_guild()
        member = guild.get_member(self.target_id) if guild else None
        if member is None:
            await interaction.followup.send("Couldn't find you in the server anymore -- nothing to do.")
            return

        summary = await _execute_retirement(member, guild)
        await interaction.followup.send(f"Retirement confirmed. {summary}")

        if self.origin_channel_id:
            origin_channel = await _channel_from_id(self.origin_channel_id)
            if origin_channel is not None:
                try:
                    await origin_channel.send(f"{member} confirmed their retirement.\n{summary}")
                except Exception:
                    logger.exception("Failed to post retirement summary to origin channel %s", self.origin_channel_id)
                await _close_ticket_thread(origin_channel)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="retirement_cancel")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user is None or interaction.user.id != self.target_id:
            await interaction.response.send_message("This confirmation isn't yours to click.", ephemeral=True)
            return
        await interaction.response.send_message("No changes made.")
        await self._disable(interaction)


def _resolve_retirement_target(message: discord.Message) -> Optional[discord.Member]:
    """@mention in the message wins; otherwise falls back to the current
    ticket thread's creator (the member who opened it)."""
    for mentioned in message.mentions:
        if isinstance(mentioned, discord.Member) and not mentioned.bot:
            return mentioned
    if isinstance(message.channel, discord.Thread) and message.channel.owner_id:
        guild = message.guild
        if guild is not None:
            owner = guild.get_member(message.channel.owner_id)
            if owner is not None and not owner.bot:
                return owner
    return None


async def _maybe_handle_retirement_announcement(message: discord.Message) -> bool:
    """Staff saying "<@member> is retiring" / "is leaving Deepiri" (or just
    "retiring"/"leaving deepiri" in a ticket thread, falling back to the
    ticket creator) posts a confirmation prompt directly in that ticket
    thread -- only the named person can confirm, at which point it's the
    same offboarding as kick-out (Discord kick + GitHub org removal), framed
    as a retirement rather than a termination."""
    if message.guild is None or not RETIRING_TRIGGER_RE.search(message.content or ""):
        return False
    if not isinstance(message.author, discord.Member) or not _is_staff_or_security_ops(message.author):
        return False

    target = _resolve_retirement_target(message)
    reply_channel = await _resolve_reply_channel(message)
    if target is None:
        await reply_channel.send(
            f"{message.author.mention} Couldn't tell who's retiring -- @ mention them, or say it in their ticket thread."
        )
        return True

    view = RetirementConfirmView(target_id=target.id, origin_channel_id=reply_channel.id)
    await reply_channel.send(
        f"{target.mention} **Are you sure you want to retire from Deepiri?** "
        "Click below to confirm -- this will remove your Discord access and GitHub org membership.",
        view=view,
    )
    return True


async def handle_ipca_signed(interaction: discord.Interaction, github_username: str) -> None:
    if not isinstance(interaction.user, discord.Member) or not _is_staff_or_security_ops(interaction.user):
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
            await _remember_identity(interaction.user.id, normalized, interaction.user if isinstance(interaction.user, discord.Member) else None)
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


async def handle_plaky_invite(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
    email: Optional[str] = None,
) -> None:
    """/plaky-invite -- staff/security-ops invite a member (or a raw address)
    to the Plaky right now. Uses the same resolution chain + headless bridge as
    the automated ticket/IPCA paths."""
    if not isinstance(interaction.user, discord.Member) or not _is_staff_or_security_ops(interaction.user):
        await interaction.response.send_message(
            "Only Admins or Security & Operations Support can run this command.", ephemeral=True
        )
        return

    target = member or interaction.user
    provided_email = (email or "").strip()
    if provided_email and not is_valid_email(provided_email):
        await interaction.response.send_message("That doesn't look like a valid email address.", ephemeral=True)
        return

    github_username = await _resolve_github_for_member(target.id, target if isinstance(target, discord.Member) else None)
    if provided_email:
        status, used_email = await _invite_member_to_plaky(
            discord_id=target.id,
            discord_username=str(target),
            member=target if isinstance(target, discord.Member) else None,
            github_username=github_username,
            email=provided_email,
            role="MEMBER",
        )
    else:
        status, used_email = await _invite_member_to_plaky(
            discord_id=target.id,
            discord_username=str(target),
            member=target if isinstance(target, discord.Member) else None,
            github_username=github_username,
            role="MEMBER",
        )
    if status == "asked":
        await interaction.response.send_message(f"No email on file for {target} — supply one with the `email:` option.", ephemeral=True)
        return
    await interaction.response.send_message(_plaky_invite_status_text(status, used_email, interaction.user.mention), ephemeral=True)


def _register_slash_commands(target_bot: DeepiriBot) -> None:
    @target_bot.tree.command(name="github-invite-request", description="Request a GitHub invite after signing ICPA")
    @app_commands.describe(
        github_username="Your GitHub profile username",
        team="Optional team to add the user to (support or it)",
    )
    @app_commands.choices(
        team=[
            app_commands.Choice(name="support", value="support"),
            app_commands.Choice(name="it", value="it"),
        ]
    )
    async def github_invite_request(
        interaction: discord.Interaction,
        github_username: str,
        team: app_commands.Choice[str] | None = None,
    ) -> None:
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


    @target_bot.tree.command(name="plaky-invite", description="Invite a member to the Plaky (staff only)")
    @app_commands.describe(member="The Discord member to invite", email="Their email, if not on file yet")
    async def plaky_invite(interaction: discord.Interaction, member: discord.Member | None = None, email: str | None = None) -> None:
        await handle_plaky_invite(interaction, member, email)

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

    @target_bot.tree.command(name="security-check", description="Run an in-depth security assessment now (staff/IT only)")
    async def security_check(interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        if not _is_staff_or_security_ops(interaction.user):
            await interaction.response.send_message("This command is restricted to staff/IT.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        results = await _run_security_assessment(interaction.guild)
        embed = _build_indepth_security_embed("In-depth security assessment", results)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @target_bot.tree.command(name="resolve-github-identity", description="Test the GitHub->Discord identity resolver for a login on demand (staff/IT only)")
    @app_commands.describe(github_login="The GitHub username to resolve")
    async def resolve_github_identity(interaction: discord.Interaction, github_login: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        if not _is_staff_or_security_ops(interaction.user):
            await interaction.response.send_message("This command is restricted to staff/IT.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        if not interaction.guild.chunked:
            try:
                await interaction.guild.chunk(cache=True)
            except Exception:
                logger.exception("resolve-github-identity: failed to chunk guild before resolving %s", github_login)
        member = await _resolve_discord_member_for_github_login(github_login, interaction.guild)
        if member is not None:
            await interaction.followup.send(f"`{github_login}` resolved to {member.mention} ({member.id}).", ephemeral=True)
        else:
            await interaction.followup.send(f"`{github_login}` did not resolve to any Discord member. Check the logs (search for `{github_login}`) for the full diagnostic -- queries tried, near-hits, and the complete candidate roster.", ephemeral=True)

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


async def _get_primary_guild() -> Optional[discord.Guild]:
    """This bot only ever operates in one guild; there's no GUILD_ID env var, so
    resolve it via any well-known channel instead."""
    for candidate_channel_id in (STAFF_CHANNEL_ID, SUPPORT_SESSIONS_CHANNEL_ID, ANNOUNCEMENTS_CHANNEL_ID):
        channel = await _channel_from_id(candidate_channel_id)
        if channel is not None and getattr(channel, "guild", None) is not None:
            return channel.guild
    return None


async def _dm_role_members(role_id: int, embed: discord.Embed) -> int:
    """Critical alerts don't wait for someone to be looking at #it-notifications —
    DM every member holding the given role (Security & Operations Support) directly.
    Best-effort per member: one blocked-DMs member shouldn't stop the rest."""
    guild = await _get_primary_guild()
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

    # Critical/urgent alerts @ the Security & Operations Support role directly in
    # the channel post (not just the individual DMs below) so it's visible to
    # anyone watching #it-notifications, not only the people who got DMed.
    role_mention = f"<@&{IT_OPERATIONS_SUPPORT_ROLE_ID}>" if severity == "critical" and IT_OPERATIONS_SUPPORT_ROLE_ID is not None else None

    try:
        await channel.send(content=role_mention, embed=embed)
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


async def test_email_debug_handler(request: web.Request) -> web.Response:
    """Debug-only: exercises the exact same identity-resolution + email-send
    path as kick-out/retirement (_send_offboarding_notice), for one Discord
    member, WITHOUT kicking them from Discord or removing them from the
    GitHub org. Signed the same way as the other inbound webhooks so this
    can't be hit by anyone who doesn't already have the shared secret.
    """
    raw_body = await request.read()
    if not ANNOUNCEMENTS_INBOUND_SECRET:
        return web.json_response({"ok": False, "message": "Webhook authentication is not configured"}, status=503)
    sig_header = request.headers.get("X-Norozo-Signature") or request.headers.get("X-Platform-Signature") or ""
    if not sig_header or not _is_valid_announcement_signature(raw_body, sig_header, ANNOUNCEMENTS_INBOUND_SECRET):
        return web.json_response({"ok": False, "message": "Missing or invalid signature"}, status=401)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return web.json_response({"ok": False, "message": "Invalid JSON"}, status=400)

    discord_id_raw = payload.get("discord_id")
    if not discord_id_raw:
        return web.json_response({"ok": False, "message": "discord_id is required"}, status=400)
    try:
        discord_id = int(discord_id_raw)
    except ValueError:
        return web.json_response({"ok": False, "message": "discord_id must be numeric"}, status=400)

    guild = await _get_primary_guild()
    if guild is None:
        return web.json_response({"ok": False, "message": "Could not resolve the primary guild"}, status=500)

    member = guild.get_member(discord_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except discord.NotFound:
            member = None
    if member is None:
        return web.json_response({"ok": False, "message": f"No member {discord_id} found in guild"}, status=404)

    github_username = _get_github_username_for_member(member)
    if github_username and GITHUB_ORG and GITHUB_PAT and not await asyncio.to_thread(is_org_member, github_username, GITHUB_ORG, GITHUB_PAT):
        github_username = None
    if not github_username:
        github_username = await _find_github_username_in_profiles_channel(member)
    if not github_username:
        github_username = await _find_github_username_via_org_roster(member)

    outcome = await _send_offboarding_notice(
        member,
        github_username,
        subject="Norozo Test Email",
        body=(
            f"Hi {member.display_name},\n\n"
            "This is a test email from Norozo to confirm SMTP delivery is working. "
            "No action needed -- you have not been removed from Discord or GitHub.\n\n"
            "-- Deepiri"
        ),
    )
    return web.json_response({"ok": True, "discord_id": discord_id, "github_username": github_username, "outcome": outcome})


async def start_webhook_server() -> None:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/plaky/webhook", plaky_webhook_handler)
    app.router.add_post("/announcements/webhook", platform_announcement_handler)
    app.router.add_post("/platform/announcements", platform_announcement_handler)
    app.router.add_post("/webhooks/platform-announcements", platform_announcement_handler)
    app.router.add_post("/alerts/webhook", platform_alert_handler)
    app.router.add_post("/webhooks/platform-alerts", platform_alert_handler)
    app.router.add_post("/debug/test-email", test_email_debug_handler)

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
        asyncio.create_task(_pr_staleness_scan_loop())
        asyncio.create_task(_security_assessment_loop())

    @new_bot.event  # type: ignore[attr-defined]
    async def on_member_join(member: discord.Member) -> None:  # type: ignore[no-redef]
        # This channel post and the DM below are independent -- a permission
        # failure on one (confirmed happening: bot lacks Send Messages in
        # SERVER_COM_CHANNEL_ID) must never take down the other. Previously
        # unguarded, so a 403 here silently killed the whole handler before it
        # ever reached the DM containing the actual onboarding questionnaire.
        welcome_channel = await _channel_from_id(SERVER_COM_CHANNEL_ID)
        if welcome_channel:
            try:
                await welcome_channel.send(
                    f"Welcome {member.mention}! Please sign the IPCA first, then run /github-invite-request in the support tickets channel to request a GitHub invite."
                )
            except discord.Forbidden:
                logger.warning("Missing permission to post welcome message in channel %s", SERVER_COM_CHANNEL_ID)
            except Exception:
                logger.exception("Failed to post welcome message for %s", member.id)
        try:
            await member.send(
                "Welcome to Deepiri. Before joining the DEV team, please sign the IPCA. "
                "After signing, run /github-invite-request in the support tickets channel so IT/staff can approve your GitHub invite.\n\n"
                "Also, reply here with your email so we can keep it on file, your GitHub profile link, "
                "and which team you're on: AI, ML, Data, Cloud/Infra/Security Engineer, Frontend, Fullstack, or Backend. "
                "You can send these as separate messages, in any order."
            )
        except discord.Forbidden:
            pass

    @new_bot.event  # type: ignore[attr-defined]
    async def on_member_update(before: discord.Member, after: discord.Member) -> None:  # type: ignore[no-redef]
        # Delegates to the primary bot's on_member_update instead of keeping a
        # second copy of the QA/IT GitHub-team sync logic in sync by hand --
        # this retry-path bot had drifted stale (missing the IT-maintainer
        # sync, the identity-resolution chain, and the QA Lead/Engineer role
        # split) precisely because a duplicate is easy to update in one place
        # and forget in the other.
        await globals()["on_member_update"](before, after)

    @new_bot.event  # type: ignore[attr-defined]
    async def on_message(message: discord.Message) -> None:  # type: ignore[no-redef]
        if message.author.bot:
            return
        if message.guild is None:
            # A DM reply to the Plaky email ask sent after an IPCA sign outranks
            # onboarding so the invite gets completed and confirmed in-DM.
            if await _maybe_handle_plaky_pending_email_reply(message):
                await new_bot.process_commands(message)
                return
            # Falls through to the older, volatile in-memory ask -- set by the
            # auto-invite-after-role-assignment flow, which doesn't go through
            # the persistent per-channel pending-ask store above.
            if message.author.id in pending_email_requests:
                email = message.content.strip()
                if is_valid_email(email):
                    _remember_user_data(message.author.id, email=email)
                    result = await call_plaky_bridge_invite(email)
                    if result.get("success"):
                        await message.channel.send(f"Plaky invite sent to {email}")
                    else:
                        await message.channel.send(f"Plaky invite failed: {result.get('error')}")
                    del pending_email_requests[message.author.id]
                else:
                    await message.channel.send("That doesn't look like a valid email. Please type a valid email (e.g., name@domain.com).")
                return
            await _maybe_handle_onboarding_dm(message)
            await new_bot.process_commands(message)
            return
        content = message.content or ""
        await notify_support_team_for_message(message)
        if await _maybe_handle_probe_needle_command(message):
            await new_bot.process_commands(message)
            return
        if await _maybe_handle_kick_out_command(message):
            await new_bot.process_commands(message)
            return
        if await _maybe_handle_retirement_announcement(message):
            await new_bot.process_commands(message)
            return
        if await _maybe_handle_plaky_pending_email_reply(message):
            # A ticket thread that was asked for its invite email answered it.
            await new_bot.process_commands(message)
            return
        if await _maybe_handle_plaky_add_request(message):
            await new_bot.process_commands(message)
            return
        # Plaky invite is now folded into _maybe_auto_assign_ipca_roles itself
        # (one combined access+Plaky message, and shared with the restart
        # catch-up sweep) rather than a separate call here.
        await _maybe_auto_assign_ipca_roles(message)
        if _is_announcements_channel(message.channel):
            title = format_discussion_title(resolve_discord_mentions(message, message.content or ""))
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
