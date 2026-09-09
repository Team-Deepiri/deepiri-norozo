"""Plaky membership via the headless bridge + multi-source identity resolution.

Invites and kicks are routed through deepiri-plaky-bridge's HTTP surface
(POST /plaky/invite, POST /plaky/kick, both gated by X-Internal-Secret), which
drives the captcha-free Cake Account API in a headless browser -- so Norozo can
invite someone to the Plaky the moment they sign the IPCA or open a support
ticket asking to be added, without a human touching the Plaky UI.

Emails are resolved through an ordered trust chain:

    1. Platform cloud DB  -- member_email_store's member_emails table on
                            platform.deepiri.com (Postgres, via the signed
                            webhook channel), the PRIMARY store. Survives bot
                            container recycles and is the only store every
                            write path keeps correctly overwritten.
    2. user_data.json     -- local backup mirror, checked only when the cloud
                            lookup comes up empty (a transient outage, or an
                            entry not yet migrated -- see
                            migrate_user_data_json_to_postgres below). Never
                            the primary source any more: real incident, when
                            it WAS checked first, a corrected email could
                            never actually take effect because the stale
                            local copy always won the race.
    3. GitHub profile     -- public email on a self-reported GitHub link.
    4. Plaky roster fuzzy -- find_user_email matching GitHub real name/login +
                            Discord display/global/username against the Plaky
                            workspace roster (only meaningful for existing
                            members, never for a brand-new invite).

Every email the bot ever sees is persisted into BOTH the cloud DB and
user_data.json (the local write is now purely a backup/full-table-scan
source -- see get_user_data's docstring), so the chain only gets stronger
over time. The bridge is deliberately left as the ground truth for whether
an address is already in the workspace -- an invite attempt to an existing
address fails fast with "Already ..." rather than being speculated on here.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
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

USER_DATA_PATH = Path(os.getenv("USER_DATA_FILE", "user_data.json"))

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_user_data_lock = asyncio.Lock()


def is_valid_email(text: Optional[str]) -> bool:
    """True for a real, bridge-safe email address; rejects bare fragments and
    the obvious garbage (spaces in the middle, marker-like test addresses)."""
    if not text:
        return False
    candidate = str(text).strip()
    if len(candidate) > 254 or " " in candidate or ".." in candidate:
        return False
    return EMAIL_RE.fullmatch(candidate) is not None


def _load_user_data() -> dict:
    try:
        if not USER_DATA_PATH.exists():
            return {}
        data = json.loads(USER_DATA_PATH.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Failed to load user data from %s", USER_DATA_PATH)
        return {}


def _save_user_data(data: dict) -> None:
    try:
        USER_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = USER_DATA_PATH.with_suffix(f"{USER_DATA_PATH.suffix}.tmp")
        temporary_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary_path.replace(USER_DATA_PATH)
    except Exception:
        logger.exception("Failed to save user data to %s", USER_DATA_PATH)


def get_user_data(discord_id: int) -> dict:
    """Local per-member record: {email, github_username, real_name, recorded_at}.
    This is now a BACKUP mirror only -- platform.deepiri.com's Postgres
    member_emails table (member_email_store.load_member_profile) is the
    primary source of truth. Kept around as: (1) a fallback when the cloud
    lookup is briefly unreachable, and (2) the source for the one thing
    Postgres genuinely can't do today -- a full-table reverse scan (see
    main.py's _load_github_username_map), since the webhook API only
    supports lookup by a single discord_id, not "list every row" or
    "find by github_username"."""
    entry = _load_user_data().get(str(discord_id))
    return entry if isinstance(entry, dict) else {}


def _remember_user_data_local(
    discord_id: int,
    *,
    email: Optional[str] = None,
    github_username: Optional[str] = None,
    real_name: Optional[str] = None,
    overwrite: bool = False,
) -> None:
    """Local-JSON half of remember_user_data -- kept as an always-on backup
    write (never the primary store any more, see get_user_data's docstring)."""
    data = _load_user_data()
    key = str(discord_id)
    entry = data.get(key) if isinstance(data.get(key), dict) else {}
    data[key] = entry
    for field, value in {
        "email": email,
        "github_username": github_username,
        "real_name": real_name,
    }.items():
        if value and (overwrite or not entry.get(field)):
            entry[field] = value
    entry["recorded_at"] = entry.get("recorded_at") or _now_iso()
    _save_user_data(data)


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
    member_emails table (the primary store) AND the local user_data.json
    backup mirror. By default never overwrites an existing value -- an
    opportunistic background capture (a name spotted while scraping a
    GitHub profile, a fuzzy Plaky-roster guess) shouldn't clobber a
    previously-confirmed real value.

    Pass overwrite=True when the person EXPLICITLY, deliberately supplied
    this value in the current request -- that's a correction, not a guess,
    and must always win. Real incident this fixed: a corrected email
    ("joeblack@deepiri.com") replied in response to "that's the email
    already on file, reply with a different one if wrong" never actually
    got saved, because every persistence path funneled through the old
    monotonic-only, LOCAL-ONLY version of this function -- the correction
    was silently discarded and the next lookup kept returning the original
    stale ("joeblacky@...") address forever, with no way to ever fix it.
    Moving to Postgres as primary fixes this at the root: the platform's
    own upsert (`COALESCE(EXCLUDED.x, member_emails.x)`) already overwrites
    unconditionally whenever a non-null value is sent, so overwrite=True
    here just means "send the value"; overwrite=False means "only send it
    if Postgres doesn't already have one" (one extra read first)."""
    # Normalized once here, the single canonical write path, rather than
    # separately by each caller (main.py's _remember_user_data wrapper used
    # to duplicate this same .lower()/.strip() -- one copy avoids drift).
    email = email.lower().strip() if email else None
    github_username = github_username.lower().strip() if github_username else None

    _remember_user_data_local(discord_id, email=email, github_username=github_username, real_name=real_name, overwrite=overwrite)

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


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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
    then ask the person in-thread rather than guessing).

    Cloud (platform.deepiri.com's Postgres member_emails table) is checked
    FIRST -- it's the primary store and the only one every write path keeps
    correctly overwritten (see remember_user_data). Local user_data.json is
    now only a fallback for when the cloud lookup is briefly unreachable,
    not a source that can shadow a more recent cloud correction the way it
    used to (real incident: local being checked first meant a corrected
    email could never actually take effect, even after the write itself
    succeeded)."""
    cloud = await load_member_profile(discord_id)
    cloud_email = cloud.get("email")
    if cloud_email:
        return cloud_email

    local = get_user_data(discord_id)
    local_email = local.get("email")
    if local_email:
        # Cloud didn't have it but local backup does -- not yet migrated (or a
        # transient cloud hiccup). Backfill cloud so this converges for next time.
        await remember_user_data(discord_id, email=local_email, overwrite=False)
        return local_email

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
        known_emails = [local_email, cloud_email]
        match = await asyncio.to_thread(
            find_user_email, names, api_key, [e for e in known_emails if e]
        )
        if match:
            await remember_user_data(discord_id, email=match)
            return match

    return None


async def persist_member_email(discord_id: int, discord_username: Optional[str], email: str, *, github_username: Optional[str] = None, overwrite: bool = False) -> None:
    """Save a confirmed email into platform.deepiri.com's Postgres (the
    primary store) AND the local user_data.json backup mirror, so every
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


async def migrate_user_data_json_to_postgres() -> dict:
    """One-time (but idempotent -- safe to run on every startup) backfill of
    whatever's in the local user_data.json backup mirror into
    platform.deepiri.com's Postgres member_emails table, now the primary
    store. Never overwrites a value Postgres already has (overwrite=False on
    every call here) -- this only fills gaps for entries that predate the
    cutover to cloud-primary reads, it never clobbers a more recent cloud
    correction with a possibly-stale local value. Safe to call every startup:
    once every row has been backfilled, every field is already non-empty in
    Postgres and remember_user_data's own existing-value check makes each
    call here a fast no-op (one GET, no POST).

    Returns {"total": N, "migrated": N, "skipped": N, "failed": N} for
    startup-log visibility into whether the backfill actually did anything.
    """
    data = _load_user_data()
    summary = {"total": len(data), "migrated": 0, "skipped": 0, "failed": 0}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        try:
            discord_id = int(key)
        except (TypeError, ValueError):
            summary["skipped"] += 1
            continue
        email = entry.get("email")
        github_username = entry.get("github_username") or entry.get("github")
        real_name = entry.get("real_name")
        if not (email or github_username or real_name):
            summary["skipped"] += 1
            continue
        try:
            before = await load_member_profile(discord_id)
            await remember_user_data(
                discord_id,
                email=email,
                github_username=github_username,
                real_name=real_name,
                overwrite=False,
            )
            after = await load_member_profile(discord_id)
            if after != before:
                summary["migrated"] += 1
            else:
                summary["skipped"] += 1
        except Exception:
            logger.exception("Failed to migrate user_data.json entry for discord_id %s to Postgres", key)
            summary["failed"] += 1
    logger.info(
        "user_data.json -> Postgres backfill: %s total, %s migrated, %s skipped (already had it), %s failed",
        summary["total"], summary["migrated"], summary["skipped"], summary["failed"],
    )
    return summary