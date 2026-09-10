"""Plaky membership via the headless bridge + multi-source identity resolution.

Invites and kicks are routed through deepiri-plaky-bridge's HTTP surface
(POST /plaky/invite, POST /plaky/kick, both gated by X-Internal-Secret), which
drives the captcha-free Cake Account API in a headless browser -- so Norozo can
invite someone to the Plaky the moment they sign the IPCA or open a support
ticket asking to be added, without a human touching the Plaky UI.

Emails are resolved through an ordered trust chain:

    1. Platform cloud DB  -- member_email_store's member_emails table on
                            platform.deepiri.com (Postgres, via the signed
                            webhook channel), the ONLY durable store. Survives
                            bot container recycles.
    2. GitHub profile     -- public email on a self-reported GitHub link.
    3. Plaky roster fuzzy -- find_user_email matching GitHub real name/login +
                            Discord display/global/username against the Plaky
                            workspace roster (only meaningful for existing
                            members, never for a brand-new invite).

There used to be a local user_data.json step between 1 and 2, framed as a
"backup mirror" -- removed entirely. Turned out this service's Render
deployment has no persistent disk: the file is baked into the container
image at build time (Dockerfile's `COPY . .`) and reset to that exact
git-committed snapshot on every single restart/redeploy, which happens on
every merge. Any write the running bot made to it during its lifetime was
gone the moment the container recycled -- it was never actually a backup of
anything, on top of having caused the real bug this whole chain fix started
from (checking it before cloud let a stale local copy permanently shadow a
correct cloud value). Postgres has been the only thing that actually
persists across restarts the entire time.
"""

import asyncio
import logging
import os
import re
from typing import List, Optional

import httpx

from github import get_user_profile
from member_email_store import load_member_profile, save_member_identity
from plaky import find_user_email


logger = logging.getLogger("deepiri.plaky_invite")

PLAKY_BRIDGE_URL = os.getenv("PLAKY_BRIDGE_URL", "http://plaky-bridge:5009").rstrip("/")
INTERNAL_SERVICE_SECRET = os.getenv("INTERNAL_SERVICE_SECRET", "").strip()
GITHUB_PAT = os.getenv("GITHUB_PAT", "").strip() or os.getenv("GITHUB_TOKEN", "").strip()
PLAKY_API_KEY = os.getenv("PLAKY_API_KEY", "").strip() or os.getenv("PLAKY_API_TOKEN", "").strip()

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def is_valid_email(text: Optional[str]) -> bool:
    """True for a real, bridge-safe email address; rejects bare fragments and
    the obvious garbage (spaces in the middle, marker-like test addresses)."""
    if not text:
        return False
    candidate = str(text).strip()
    if len(candidate) > 254 or " " in candidate or ".." in candidate:
        return False
    return EMAIL_RE.fullmatch(candidate) is not None


async def remember_user_data(
    discord_id: int,
    *,
    email: Optional[str] = None,
    github_username: Optional[str] = None,
    real_name: Optional[str] = None,
    discord_username: Optional[str] = None,
    overwrite: bool = False,
) -> None:
    """Merge newly-confirmed facts into platform.deepiri.com's Postgres
    member_emails table -- the only durable store (see module docstring for
    why the local-JSON "backup" was removed). By default never overwrites an
    existing value -- an opportunistic background capture (a name spotted
    while scraping a GitHub profile, a fuzzy Plaky-roster guess) shouldn't
    clobber a previously-confirmed real value.

    Pass overwrite=True when the person EXPLICITLY, deliberately supplied
    this value in the current request -- that's a correction, not a guess,
    and must always win. Postgres's own upsert
    (`COALESCE(EXCLUDED.x, member_emails.x)`) already overwrites
    unconditionally whenever a non-null value is sent, so overwrite=True
    here just means "send the value"; overwrite=False means "only send it
    if Postgres doesn't already have one" (one extra read first)."""
    email = email.lower().strip() if email else None
    github_username = github_username.lower().strip() if github_username else None

    payload_email, payload_github, payload_real_name = email, github_username, real_name
    if not overwrite and (email or github_username or real_name):
        existing = await load_member_profile(discord_id)
        if email and existing.get("email"):
            payload_email = None
        if github_username and existing.get("github_username"):
            payload_github = None
        if real_name and existing.get("real_name"):
            payload_real_name = None
    if payload_email or payload_github or payload_real_name or discord_username:
        await save_member_identity(
            discord_id,
            discord_username=discord_username,
            email=payload_email,
            real_name=payload_real_name,
            github_username=payload_github,
        )


async def call_plaky_bridge_invite(email: str, role: str = "MEMBER") -> dict:
    """POST /plaky/invite -> {success, via, status, error, ...}. Bridge is the
    ground truth for already-invited vs new; its 409 'Already ...' becomes
    success=False with error prefixed 'Already'."""
    if not is_valid_email(email):
        return {"success": False, "error": "Invalid email"}
    if not INTERNAL_SERVICE_SECRET:
        return {"success": False, "error": "Plaky bridge secret not configured"}
    url = f"{PLAKY_BRIDGE_URL}/plaky/invite"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url,
                json={"email": email, "role": str(role or "MEMBER").upper()},
                headers={"X-Internal-Secret": INTERNAL_SERVICE_SECRET},
            )
        if resp.status_code == 200:
            data = resp.json() if resp.content else {}
            return {"success": True, **data}
        if resp.status_code == 409:
            data = resp.json() if resp.content else {}
            return {"success": False, "error": data.get("error", "Already in Plaky"), "already": True, **data}
        return {"success": False, "error": f"Bridge HTTP {resp.status_code}"}
    except Exception:
        logger.exception("Plaky bridge invite failed for %s", email)
        return {"success": False, "error": "Plaky bridge unreachable"}


async def call_plaky_bridge_kick(email: str) -> dict:
    """POST /plaky/kick -> {success, status(id/inactive), error}. Deactivates the
    member in the Plaky workspace (Cake layer) so they lose access on the next
    sync. Best-effort: a not-found address is reported but not fatal."""
    if not is_valid_email(email):
        return {"success": False, "error": "Invalid email"}
    if not INTERNAL_SERVICE_SECRET:
        return {"success": False, "error": "Plaky bridge secret not configured"}
    url = f"{PLAKY_BRIDGE_URL}/plaky/kick"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url,
                json={"email": email},
                headers={"X-Internal-Secret": INTERNAL_SERVICE_SECRET},
            )
        data = resp.json() if resp.content else {}
        if resp.status_code not in (200, 404):
            return {"success": False, "error": f"Bridge HTTP {resp.status_code}", **data}
        return {"success": bool(resp.status_code == 200), "not_found": bool(resp.status_code == 404), **data}
    except Exception:
        logger.exception("Plaky bridge kick failed for %s", email)
        return {"success": False, "error": "Plaky bridge unreachable"}


async def resolve_member_email(
    discord_id: int,
    *,
    github_username: Optional[str] = None,
    member_hints: Optional[List[str]] = None,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """Ordered trust chain for one member's Plaky invite email. Returns the
    first confirmed address found, or None when nothing is known yet (callers
    then ask the person in-thread rather than guessing). Cloud
    (platform.deepiri.com's Postgres member_emails table) is the only
    durable store checked -- see module docstring for why there's no local
    fallback any more."""
    cloud = await load_member_profile(discord_id)
    cloud_email = cloud.get("email")
    if cloud_email:
        return cloud_email

    if github_username and GITHUB_PAT:
        profile = await asyncio.to_thread(get_user_profile, github_username, GITHUB_PAT)
        if profile.get("email"):
            await remember_user_data(discord_id, email=profile["email"], github_username=github_username)
            return profile["email"]

    # Plaky roster fuzzy match is last: it only helps for people ALREADY in the
    # workspace (a brand-new invited member can't be matched in a roster), and a
    # wrong guess that later collides with a real invite is worse than asking.
    if api_key and (member_hints or github_username or cloud.get("real_name")):
        names: List[str] = [n for n in (member_hints or []) if n]
        if github_username:
            names.append(github_username)
        if cloud.get("real_name"):
            names.append(cloud["real_name"])
        match = await asyncio.to_thread(
            find_user_email, names, api_key, [cloud_email] if cloud_email else []
        )
        if match:
            await remember_user_data(discord_id, email=match)
            return match

    return None


async def persist_member_email(discord_id: int, discord_username: Optional[str], email: str, *, github_username: Optional[str] = None, overwrite: bool = False) -> None:
    """Save a confirmed email into platform.deepiri.com's Postgres, so every
    capture path (onboarding DM, in-thread answer, IPCA sign, staff
    /plaky-invite) feeds the same chain.

    overwrite is forwarded to remember_user_data -- pass True when the caller
    explicitly supplied this email in the current request (a correction),
    so it always replaces whatever was on file rather than being silently
    dropped by the default monotonic-only behavior."""
    await remember_user_data(
        discord_id,
        email=email,
        github_username=github_username,
        discord_username=discord_username,
        overwrite=overwrite,
    )