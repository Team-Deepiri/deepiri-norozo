import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests

from identity_match import best_match


logger = logging.getLogger("deepiri.plaky")


PLAKY_API_BASE = os.getenv("PLAKY_API_BASE", "https://api.plaky.com/v2")


def _leading_name_token(s: str) -> str:
    """First alphabetic run in a Discord/Plaky-handle-shaped string —
    'wren.h._83898' -> 'wren', 'Wren.m.2h35' -> 'Wren'. Strips the random
    suffixes these account handles carry, which a real human-typed name never has."""
    m = re.match(r"[A-Za-z]+", (s or "").strip())
    return m.group(0) if m else ""


def _request_with_rate_limit_retry(method: str, url: str, headers: Dict[str, str], json: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None, retries: int = 2) -> requests.Response:
    """Perform an HTTP request and retry on 429 using Retry-After when available."""
    for attempt in range(retries + 1):
        response = requests.request(method=method, url=url, headers=headers, json=json, params=params, timeout=20)

        if response.status_code != 429:
            return response

        if attempt == retries:
            return response

        retry_after = response.headers.get("Retry-After")
        wait_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 2
        time.sleep(wait_seconds)

    return response


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_task(title: str, description: str, priority: str, api_key: str) -> Dict[str, Any]:
    """Create a Plaky task using the configured API key."""
    if not api_key:
        return {
            "ok": False,
            "status": 400,
            "message": "PLAKY_API_KEY is missing.",
        }

    url = f"{PLAKY_API_BASE}/tasks"
    body = {
        "title": title,
        "description": description,
        "priority": priority,
    }

    response = _request_with_rate_limit_retry("POST", url, headers=_headers(api_key), json=body)

    if response.status_code in (200, 201):
        payload = response.json()
        task_id = payload.get("id") or payload.get("taskId")
        task_url = payload.get("url") or payload.get("taskUrl") or (f"https://app.plaky.com/task/{task_id}" if task_id else None)

        return {
            "ok": True,
            "status": response.status_code,
            "task": payload,
            "task_url": task_url,
        }

    if response.status_code == 429:
        return {
            "ok": False,
            "status": 429,
            "message": "Plaky API rate limited the request. Please retry shortly.",
        }

    return {
        "ok": False,
        "status": response.status_code,
        "message": f"Failed to create Plaky task ({response.status_code}): {response.text[:200]}",
    }


def _user_emails(user: Dict[str, Any]) -> List[str]:
    """Mirrors deepiri-boardman's identity_common.plaky_email_addresses — Plaky user
    records aren't consistent about which field carries the email, so check all the
    field names boardman's own matcher has already had to account for."""
    out: List[str] = []
    for key in ("email", "primaryEmail", "mail", "userEmail"):
        v = user.get(key)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    raw_list = user.get("emails")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                ev = item.get("email") or item.get("value")
                if isinstance(ev, str) and ev.strip():
                    out.append(ev.strip())
    return out


def find_user_email_by_name(name: str, api_key: str) -> Optional[str]:
    """Best-effort: Plaky's public API docs describe GET /users but say it requires
    admin/project-owner privileges, and don't document a board-membership-by-name
    lookup. Try it and fail quietly (403/anything-but-200) rather than treating an
    unverified endpoint as guaranteed — callers should already have a fallback.

    Uses scored fuzzy name matching (identity_match.best_match) instead of exact
    equality — same philosophy as deepiri-boardman's person_match: a clear winner
    or nothing, never a guess on a near-tie between two different people.
    """
    if not api_key or not name:
        logger.warning("find_user_email_by_name: missing api_key or name (name=%r)", name)
        return None
    if not name.strip():
        logger.warning("find_user_email_by_name: name is blank after strip")
        return None
    try:
        response = _request_with_rate_limit_retry("GET", f"{PLAKY_API_BASE}/users", headers=_headers(api_key))
    except requests.RequestException:
        logger.exception("find_user_email_by_name: GET /users request failed")
        return None
    if response.status_code != 200:
        logger.warning(
            "find_user_email_by_name: GET /users returned %s for query %r: %s",
            response.status_code, name, response.text[:300],
        )
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("find_user_email_by_name: GET /users returned non-JSON body")
        return None
    users = payload if isinstance(payload, list) else payload.get("users", [])
    if not users:
        logger.warning("find_user_email_by_name: GET /users returned zero users (query=%r)", name)
        return None

    display_names = [
        str(user.get("name") or user.get("displayName") or user.get("username") or "")
        for user in users
    ]
    match = best_match(name, display_names)
    if match is None:
        # Second pass: Discord account handles/usernames aren't clean human-typed
        # names ("wren.h._83898") -- they carry random suffixes that make every
        # token required to line up, which kills an otherwise-unique first-name
        # match ("wren" vs the only "Wren.*" in the whole roster). Retry on
        # just the leading name token from both sides. Still goes through
        # best_match's ambiguity refusal, so two people sharing a first name
        # still won't get guessed at.
        leading_query = _leading_name_token(name)
        leading_candidates = [_leading_name_token(n) for n in display_names]
        if leading_query:
            match = best_match(leading_query, leading_candidates)
    if match is None:
        logger.warning(
            "find_user_email_by_name: no confident match for %r among %s Plaky users (sample: %s)",
            name, len(users), display_names[:10],
        )
        return None
    logger.info("find_user_email_by_name: matched %r -> %r (score=%s)", name, display_names[match.index], match.score)
    emails = _user_emails(users[match.index])
    if not emails:
        logger.warning("find_user_email_by_name: matched %r but that Plaky user has no email field set", display_names[match.index])
    return emails[0] if emails else None


def get_tasks(api_key: str, status: str = "open") -> Dict[str, Any]:
    """Fetch Plaky tasks by status."""
    if not api_key:
        return {
            "ok": False,
            "status": 400,
            "message": "PLAKY_API_KEY is missing.",
        }

    url = f"{PLAKY_API_BASE}/tasks"
    params = {"status": status}

    response = _request_with_rate_limit_retry("GET", url, headers=_headers(api_key), params=params)

    if response.status_code == 200:
        payload = response.json()
        tasks: List[Dict[str, Any]]

        if isinstance(payload, list):
            tasks = payload
        else:
            tasks = payload.get("tasks", [])

        return {
            "ok": True,
            "status": response.status_code,
            "tasks": tasks,
        }

    if response.status_code == 429:
        return {
            "ok": False,
            "status": 429,
            "message": "Plaky API rate limited the request. Please retry shortly.",
        }

    return {
        "ok": False,
        "status": response.status_code,
        "message": f"Failed to fetch Plaky tasks ({response.status_code}): {response.text[:200]}",
    }
