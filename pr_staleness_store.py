"""Durable per-PR staleness-tier tracking on platform.deepiri.com's Postgres
(pr_staleness_state table, via deepiri-api-gateway), reached through the same
signed webhook channel as state_store.py/member_email_store.py. Ensures each
of the three escalation tiers (2 week / 2.5 week / 1 month) fires exactly once
per PR, not on every periodic scan.
"""

import hashlib
import hmac
import json
import logging
import os
from typing import Optional
from urllib.parse import urlparse

import httpx


logger = logging.getLogger("deepiri.pr_staleness_store")


def _pr_staleness_url() -> Optional[str]:
    announcements_url = (os.getenv("PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL") or "").strip()
    if not announcements_url:
        return None
    parsed = urlparse(announcements_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/api/webhooks/norozo/pr-staleness"


def _secret() -> Optional[str]:
    secret = (
        os.getenv("PLATFORM_ANNOUNCEMENTS_WEBHOOK_SECRET")
        or os.getenv("PLATFORM_WEBHOOK_SECRET")
        or os.getenv("ANNOUNCEMENTS_WEBHOOK_SECRET")
        or ""
    ).strip()
    return secret or None


def _pr_staleness_claim_url() -> Optional[str]:
    base = _pr_staleness_url()
    return f"{base}/claim-1month" if base else None


async def claim_pr_staleness_1month(repo: str, pr_number: int) -> bool:
    """Atomically claims the one-time #announcements slot for a PR -- returns
    True only for the single caller that transitions notified_1month from
    false to true (a conditional UPDATE on the gateway's side), so two
    overlapping scan loops (e.g. during a Render redeploy) can never both post
    the same PR's 1-month announcement. Fails closed: any error means "don't
    post" rather than risking a duplicate."""
    url = _pr_staleness_claim_url()
    secret = _secret()
    if not url or not secret:
        return False
    body = {"repo": repo, "pr_number": pr_number}
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
        return bool(resp.json().get("claimed"))
    except Exception:
        logger.exception("Failed to claim PR staleness 1-month announcement for %s#%s", repo, pr_number)
        return False


async def load_pr_staleness(repo: str, pr_number: int) -> dict:
    """Returns {notified_2week, notified_1month, resolved_discord_id,
    last_author_dm_at, reviewer_dm_state} -- all False/None/{} if nothing's
    been recorded yet, or on any failure (fail open toward re-checking rather
    than silently never notifying at all)."""
    default = {
        "notified_2week": False,
        "notified_1month": False,
        "resolved_discord_id": None,
        "last_author_dm_at": None,
        "reviewer_dm_state": {},
    }
    url = _pr_staleness_url()
    secret = _secret()
    if not url or not secret:
        return default
    signing_string = f"GET /api/webhooks/norozo/pr-staleness?repo={repo}&pr_number={pr_number}"
    signature = hmac.new(secret.encode("utf-8"), signing_string.encode("utf-8"), hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                params={"repo": repo, "pr_number": str(pr_number)},
                headers={"X-Norozo-Signature": f"sha256={signature}"},
            )
        resp.raise_for_status()
        data = resp.json()
        return {
            "notified_2week": bool(data.get("notified2Week")),
            "notified_1month": bool(data.get("notified1Month")),
            "resolved_discord_id": data.get("resolvedDiscordId"),
            "last_author_dm_at": data.get("lastAuthorDmAt"),
            "reviewer_dm_state": data.get("reviewerDmState") or {},
        }
    except Exception:
        logger.exception("Failed to load PR staleness state for %s#%s", repo, pr_number)
        return default


async def save_pr_staleness(
    repo: str,
    pr_number: int,
    *,
    notified_2week: Optional[bool] = None,
    notified_1month: Optional[bool] = None,
    resolved_discord_id: Optional[str] = None,
    last_author_dm_at: Optional[str] = None,
    reviewer_dm_state: Optional[dict] = None,
) -> bool:
    url = _pr_staleness_url()
    secret = _secret()
    if not url or not secret:
        return False
    body = {
        "repo": repo,
        "pr_number": pr_number,
        "notified_2week": notified_2week,
        "notified_1month": notified_1month,
        "resolved_discord_id": resolved_discord_id,
        "last_author_dm_at": last_author_dm_at,
        "reviewer_dm_state": reviewer_dm_state,
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
        logger.exception("Failed to save PR staleness state for %s#%s", repo, pr_number)
        return False


async def find_discord_id_by_email(email: str) -> Optional[str]:
    """Reverse lookup on member_emails -- part of the GitHub-PR-author ->
    Discord identity chain: Plaky hands back a self-reported email, this finds
    which Discord account reported it at onboarding."""
    announcements_url = (os.getenv("PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL") or "").strip()
    secret = _secret()
    if not announcements_url or not secret or not email:
        return None
    parsed = urlparse(announcements_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    url = f"{parsed.scheme}://{parsed.netloc}/api/webhooks/norozo/member-email/by-email"
    email_norm = email.strip().lower()
    signing_string = f"GET /api/webhooks/norozo/member-email/by-email?email={email_norm}"
    signature = hmac.new(secret.encode("utf-8"), signing_string.encode("utf-8"), hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                params={"email": email_norm},
                headers={"X-Norozo-Signature": f"sha256={signature}"},
            )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("discordId")
    except Exception:
        logger.exception("Failed reverse email lookup for %s", email)
        return None
