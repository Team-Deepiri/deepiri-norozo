"""Durable checkpoint of 'last time the bot was known to be alive', stored in
platform.deepiri.com's Postgres via the same signed webhook channel Norozo already
uses for the announcements bridge (deepiri-api-gateway route
POST/GET /api/webhooks/norozo/state, table bot_state). Render's free-tier disk is
ephemeral and gets wiped on every spin-down/restart, so this can't just live on
local disk — it needs to survive the process itself going away.
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx


logger = logging.getLogger("deepiri.state_store")

STATE_KEY_LAST_ONLINE_AT = "norozo_last_online_at"
GET_SIGNING_STRING = b"GET /api/webhooks/norozo/state"


def _platform_state_url() -> Optional[str]:
    announcements_url = (os.getenv("PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL") or "").strip()
    if not announcements_url:
        return None
    parsed = urlparse(announcements_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/api/webhooks/norozo/state"


def _secret() -> Optional[str]:
    secret = (
        os.getenv("PLATFORM_ANNOUNCEMENTS_WEBHOOK_SECRET")
        or os.getenv("PLATFORM_WEBHOOK_SECRET")
        or os.getenv("ANNOUNCEMENTS_WEBHOOK_SECRET")
        or ""
    ).strip()
    return secret or None


async def load_last_online_at() -> Optional[datetime]:
    """Returns the checkpoint timestamp, or None if unavailable/never set —
    callers should fall back to a conservative default lookback in that case."""
    url = _platform_state_url()
    secret = _secret()
    if not url or not secret:
        return None
    signature = hmac.new(secret.encode("utf-8"), GET_SIGNING_STRING, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                params={"key": STATE_KEY_LAST_ONLINE_AT},
                headers={"X-Norozo-Signature": f"sha256={signature}"},
            )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return datetime.fromisoformat(resp.json()["value"])
    except Exception:
        logger.exception("Failed to load last-online checkpoint; will use default lookback")
        return None


async def save_last_online_at(when: Optional[datetime] = None) -> None:
    """Best-effort — a failed write just means the next restart's catch-up
    window is wider than ideal, not that anything is lost."""
    url = _platform_state_url()
    secret = _secret()
    if not url or not secret:
        return
    when = when or datetime.now(timezone.utc)
    body = {"key": STATE_KEY_LAST_ONLINE_AT, "value": when.isoformat()}
    raw = json.dumps(body).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Norozo-Signature": f"sha256={signature}",
                },
            )
            resp.raise_for_status()
    except Exception:
        logger.exception("Failed to save last-online checkpoint")
