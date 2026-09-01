import os
import time
from typing import Any, Dict, List, Optional

import requests

from identity_match import best_match


PLAKY_API_BASE = os.getenv("PLAKY_API_BASE", "https://api.plaky.com/v2")


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
        return None
    if not name.strip():
        return None
    try:
        response = _request_with_rate_limit_retry("GET", f"{PLAKY_API_BASE}/users", headers=_headers(api_key))
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    users = payload if isinstance(payload, list) else payload.get("users", [])
    if not users:
        return None

    display_names = [
        str(user.get("name") or user.get("displayName") or user.get("username") or "")
        for user in users
    ]
    match = best_match(name, display_names)
    if match is None:
        return None
    emails = _user_emails(users[match.index])
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
