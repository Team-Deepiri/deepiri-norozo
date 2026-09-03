"""Durable per-member identity cache on platform.deepiri.com's Postgres
(member_emails table, via deepiri-api-gateway), reached through the same
signed webhook channel as state_store.py's checkpoint.

This is the "dynamic nickname alias table" -- built automatically as a
byproduct of things Norozo already does (onboarding capturing a self-reported
email or GitHub link), never hand-curated. The moment someone links their own
GitHub profile, their real name gets fetched and cached here immediately --
so a later kick-out/retirement lookup for a stylized handle like "wrenx1005"
doesn't need to fuzzy-match a nickname to a real name at all; it's an exact
cache hit on "Taylor Chen", captured the day they onboarded, independent of
whatever GitHub-username resolution succeeds or fails at kick time.
"""

import hashlib
import hmac
import json
import logging
import os
from typing import Optional
from urllib.parse import urlparse

import httpx


logger = logging.getLogger("deepiri.member_email_store")

GET_SIGNING_PREFIX = "GET /api/webhooks/norozo/member-email?discord_id="


def _member_email_url() -> Optional[str]:
    announcements_url = (os.getenv("PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL") or "").strip()
    if not announcements_url:
        return None
    parsed = urlparse(announcements_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/api/webhooks/norozo/member-email"


def _secret() -> Optional[str]:
    secret = (
        os.getenv("PLATFORM_ANNOUNCEMENTS_WEBHOOK_SECRET")
        or os.getenv("PLATFORM_WEBHOOK_SECRET")
        or os.getenv("ANNOUNCEMENTS_WEBHOOK_SECRET")
        or ""
    ).strip()
    return secret or None


async def _save_member_identity(
    discord_id: int,
    *,
    discord_username: Optional[str] = None,
    email: Optional[str] = None,
    real_name: Optional[str] = None,
    github_username: Optional[str] = None,
) -> bool:
    url = _member_email_url()
    secret = _secret()
    if not url or not secret:
        return False
    body = {
        "discord_id": str(discord_id),
        "discord_username": discord_username,
        "email": email,
        "real_name": real_name,
        "github_username": github_username,
    }
    raw = json.dumps(body).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                content=raw,
                headers={"Content-Type": "application/json", "X-Norozo-Signature": f"sha256={signature}"},
            )
            resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to save member identity for %s", discord_id)
        return False


async def save_member_email(discord_id: int, discord_username: str, email: str) -> bool:
    return await _save_member_identity(discord_id, discord_username=discord_username, email=email)


async def save_member_real_name(discord_id: int, real_name: str, github_username: Optional[str] = None) -> bool:
    """Called the moment we ever get a confident real name for someone -- today,
    right after onboarding captures their self-reported GitHub link. This is
    the entire "dynamic alias table": no one ever hand-types a nickname
    mapping, it's just recorded automatically the first time it's known."""
    return await _save_member_identity(discord_id, real_name=real_name, github_username=github_username)


async def load_member_profile(discord_id: int) -> dict:
    """Returns {email, real_name, github_username} -- all None if nothing's
    on file yet or on any failure (fail open toward the caller's other
    resolution paths rather than erroring out)."""
    default = {"email": None, "real_name": None, "github_username": None}
    url = _member_email_url()
    secret = _secret()
    if not url or not secret:
        return default
    signing_string = f"{GET_SIGNING_PREFIX}{discord_id}"
    signature = hmac.new(secret.encode("utf-8"), signing_string.encode("utf-8"), hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                params={"discord_id": str(discord_id)},
                headers={"X-Norozo-Signature": f"sha256={signature}"},
            )
        if resp.status_code == 404:
            return default
        resp.raise_for_status()
        data = resp.json()
        return {
            "email": data.get("email"),
            "real_name": data.get("realName"),
            "github_username": data.get("githubUsername"),
        }
    except Exception:
        logger.exception("Failed to load member profile for %s", discord_id)
        return default


async def load_member_email(discord_id: int) -> Optional[str]:
    return (await load_member_profile(discord_id))["email"]
