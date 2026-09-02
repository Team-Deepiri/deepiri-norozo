"""Durable per-member email store on platform.deepiri.com's Postgres (member_emails
table, via deepiri-api-gateway), reached through the same signed webhook channel as
state_store.py's checkpoint. Self-reported at join time — the most authoritative
email source available, since it comes directly from the person rather than being
guessed from a GitHub profile or fuzzy-matched against Plaky.
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


async def save_member_email(discord_id: int, discord_username: str, email: str) -> bool:
    url = _member_email_url()
    secret = _secret()
    if not url or not secret:
        return False
    body = {"discord_id": str(discord_id), "discord_username": discord_username, "email": email}
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
        logger.exception("Failed to save member email for %s", discord_id)
        return False


async def load_member_email(discord_id: int) -> Optional[str]:
    url = _member_email_url()
    secret = _secret()
    if not url or not secret:
        return None
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
            return None
        resp.raise_for_status()
        return resp.json().get("email")
    except Exception:
        logger.exception("Failed to load member email for %s", discord_id)
        return None
