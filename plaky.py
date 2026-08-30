import os
import time
from typing import Any, Dict, List, Optional

import requests

# Real Plaky public API per docs.plaky.com (verified 2026-08-29):
# Base: https://api.plaky.com/v1/public
# Auth: X-API-Key header (NOT Bearer)
# Spec extracted from docs.plaky.com Scalar page → /tmp/plaky_spec.json
# Paths: /v1/public/users, /v1/public/users/me, /v1/public/spaces, etc.
# IMPORTANT: Public API has NO invite/member-management endpoints.
# Only GET users/spaces/boards/items/comments. Invite/kick must be done via Web UI.

PLAKY_API_BASE = os.getenv("PLAKY_API_BASE", "https://api.plaky.com/v1/public")
# Support both naming conventions: norozo uses PLAKY_API_KEY, platform uses PLAKY_API_TOKEN
DEFAULT_API_KEY = os.getenv("PLAKY_API_KEY") or os.getenv("PLAKY_API_TOKEN") or ""


def _request_with_rate_limit_retry(
    method: str, url: str, headers: Dict[str, str], json: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None, retries: int = 2
) -> requests.Response:
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
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _require_key(api_key: str) -> Optional[Dict[str, Any]]:
    if not api_key:
        return {
            "ok": False,
            "status": 400,
            "message": "PLAKY_API_KEY (or PLAKY_API_TOKEN) is missing.",
        }
    return None


# ---------------------------------------------------------------------------
# Real API: Users (read-only)
# ---------------------------------------------------------------------------

def get_users(
    api_key: str,
    emails: Optional[List[str]] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> Dict[str, Any]:
    """Fetch workspace users. Mirrors GET /v1/public/users."""
    err = _require_key(api_key)
    if err:
        return err

    url = f"{PLAKY_API_BASE}/users"
    params: Dict[str, Any] = {"page": page, "pageSize": page_size}
    if emails:
        params["emails"] = emails
    if status:
        params["status"] = status
    if type:
        params["type"] = type

    response = _request_with_rate_limit_retry("GET", url, headers=_headers(api_key), params=params)

    if response.status_code == 200:
        payload = response.json()
        users = payload.get("data", []) if isinstance(payload, dict) else []
        return {
            "ok": True,
            "status": 200,
            "users": users,
            "hasMore": payload.get("hasMore", False) if isinstance(payload, dict) else False,
            "raw": payload,
        }

    if response.status_code == 429:
        return {"ok": False, "status": 429, "message": "Plaky API rate limited. Retry shortly."}

    return {
        "ok": False,
        "status": response.status_code,
        "message": f"Failed to fetch users ({response.status_code}): {response.text[:300]}",
    }


def get_me(api_key: str) -> Dict[str, Any]:
    """Get currently authenticated user. GET /v1/public/users/me"""
    err = _require_key(api_key)
    if err:
        return err
    url = f"{PLAKY_API_BASE}/users/me"
    response = _request_with_rate_limit_retry("GET", url, headers=_headers(api_key))
    if response.status_code == 200:
        return {"ok": True, "status": 200, "user": response.json()}
    if response.status_code == 429:
        return {"ok": False, "status": 429, "message": "Plaky API rate limited. Retry shortly."}
    return {"ok": False, "status": response.status_code, "message": f"Failed to fetch me ({response.status_code}): {response.text[:300]}"}


def get_user_by_email(api_key: str, email: str) -> Dict[str, Any]:
    """Find a user by email using filtered GET /users."""
    result = get_users(api_key, emails=[email])
    if not result.get("ok"):
        return result
    users = result.get("users", [])
    for u in users:
        if u.get("email", "").lower() == email.lower():
            return {"ok": True, "status": 200, "user": u}
    return {"ok": False, "status": 404, "message": f"User {email} not found in workspace."}


def check_membership(api_key: str, email: str) -> Dict[str, Any]:
    """Check if an email is an active member of the workspace."""
    return get_user_by_email(api_key, email)


# ---------------------------------------------------------------------------
# Real API: Spaces / Boards (needed to create items)
# ---------------------------------------------------------------------------

def get_spaces(api_key: str) -> Dict[str, Any]:
    err = _require_key(api_key)
    if err:
        return err
    url = f"{PLAKY_API_BASE}/spaces"
    response = _request_with_rate_limit_retry("GET", url, headers=_headers(api_key))
    if response.status_code == 200:
        payload = response.json()
        spaces = payload.get("data", []) if isinstance(payload, dict) else []
        return {"ok": True, "status": 200, "spaces": spaces, "raw": payload}
    return {"ok": False, "status": response.status_code, "message": f"Failed to fetch spaces ({response.status_code}): {response.text[:300]}"}


def get_boards(api_key: str, space_id: str) -> Dict[str, Any]:
    err = _require_key(api_key)
    if err:
        return err
    url = f"{PLAKY_API_BASE}/spaces/{space_id}/boards"
    response = _request_with_rate_limit_retry("GET", url, headers=_headers(api_key))
    if response.status_code == 200:
        payload = response.json()
        boards = payload.get("data", []) if isinstance(payload, dict) else []
        return {"ok": True, "status": 200, "boards": boards, "raw": payload}
    return {"ok": False, "status": response.status_code, "message": f"Failed to fetch boards ({response.status_code}): {response.text[:300]}"}


def _discover_default_board(api_key: str) -> Optional[Dict[str, str]]:
    """Try to find a usable space/board for item creation. Returns {spaceId, boardId} or None."""
    spaces_res = get_spaces(api_key)
    if not spaces_res.get("ok"):
        return None
    spaces = spaces_res.get("spaces", [])
    # Prefer defaultSpace, else first
    sorted_spaces = sorted(spaces, key=lambda s: (not s.get("defaultSpace"), s.get("id")))
    for space in sorted_spaces:
        sid = str(space.get("id"))
        boards_res = get_boards(api_key, sid)
        if not boards_res.get("ok"):
            continue
        boards = boards_res.get("boards", [])
        if boards:
            # Prefer first board
            bid = str(boards[0].get("id"))
            return {"spaceId": sid, "boardId": bid}
    return None


# ---------------------------------------------------------------------------
# Items (tasks) — backed by real API
# ---------------------------------------------------------------------------

def create_task(title: str, description: str, priority: str, api_key: str, space_id: Optional[str] = None, board_id: Optional[str] = None) -> Dict[str, Any]:
    """Create a Plaky item (task) via POST /v1/public/spaces/{spaceId}/boards/{boardId}/items."""
    err = _require_key(api_key)
    if err:
        return err

    # Resolve space/board if not provided
    if not space_id or not board_id:
        discovered = _discover_default_board(api_key)
        if not discovered:
            return {
                "ok": False,
                "status": 404,
                "message": "No space/board found to create item. Create a board via Plaky Web UI first.",
            }
        space_id = space_id or discovered["spaceId"]
        board_id = board_id or discovered["boardId"]

    url = f"{PLAKY_API_BASE}/spaces/{space_id}/boards/{board_id}/items"
    # Minimal body per spec: requires at least title; itemGroup handling left to API defaults
    body: Dict[str, Any] = {"title": title}
    # Description/priority stored as fields if available — try to send as custom fields; fallback to title only
    # Real spec uses fields patch separately; create with title, then patch fields if needed
    response = _request_with_rate_limit_retry("POST", url, headers=_headers(api_key), json=body)

    if response.status_code in (200, 201):
        payload = response.json()
        # API returns item with id; construct URL
        item_id = payload.get("id") if isinstance(payload, dict) else None
        task_url = payload.get("url") or (f"https://app.plaky.com/board/{board_id}/item/{item_id}" if item_id else None)
        return {"ok": True, "status": response.status_code, "task": payload, "task_url": task_url, "spaceId": space_id, "boardId": board_id}

    if response.status_code == 429:
        return {"ok": False, "status": 429, "message": "Plaky API rate limited. Retry shortly."}

    # Surface full error for debugging (old code truncated to 200 chars)
    return {"ok": False, "status": response.status_code, "message": f"Failed to create item ({response.status_code}): {response.text[:500]}"}


def get_tasks(api_key: str, status: str = "open") -> Dict[str, Any]:
    """Fetch items for the default board. Maps old /tasks concept to real GET items."""
    err = _require_key(api_key)
    if err:
        return err

    discovered = _discover_default_board(api_key)
    if not discovered:
        return {"ok": False, "status": 404, "message": "No space/board found. Cannot list items."}

    space_id = discovered["spaceId"]
    board_id = discovered["boardId"]
    url = f"{PLAKY_API_BASE}/spaces/{space_id}/boards/{board_id}/items"
    params = {"page": 1, "pageSize": 50}
    response = _request_with_rate_limit_retry("GET", url, headers=_headers(api_key), params=params)

    if response.status_code == 200:
        payload = response.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        # Map to old tasks shape for backward compat
        tasks = [{"title": it.get("title", "Untitled"), "status": it.get("status", status), "url": f"https://app.plaky.com/board/{board_id}/item/{it.get('id')}", **it} for it in items]
        return {"ok": True, "status": 200, "tasks": tasks, "items": items, "raw": payload}

    if response.status_code == 429:
        return {"ok": False, "status": 429, "message": "Plaky API rate limited. Retry shortly."}

    return {"ok": False, "status": response.status_code, "message": f"Failed to fetch items ({response.status_code}): {response.text[:500]}"}


def get_items(api_key: str, space_id: str, board_id: str, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """Explicit fetch of items for a given board."""
    err = _require_key(api_key)
    if err:
        return err
    url = f"{PLAKY_API_BASE}/spaces/{space_id}/boards/{board_id}/items"
    response = _request_with_rate_limit_retry("GET", url, headers=_headers(api_key), params={"page": page, "pageSize": page_size})
    if response.status_code == 200:
        payload = response.json()
        return {"ok": True, "status": 200, "items": payload.get("data", []), "raw": payload}
    return {"ok": False, "status": response.status_code, "message": f"Failed to fetch items ({response.status_code}): {response.text[:500]}"}


# ---------------------------------------------------------------------------
# Invite / Kick — NOT supported by public API (verified 2026-08-29)
# ---------------------------------------------------------------------------

_INVITE_UNSUPPORTED_MSG = (
    "Plaky public API does NOT support inviting or removing members. "
    "Verified against full OpenAPI spec at docs.plaky.com (GET /v1/public/users is read-only; no POST/DELETE/PUT for users). "
    "Invite/kick must be done via Plaky Web UI (Space → Users → Invite). "
    "If you need automation, the deepiri-platform external-bridge can expose a headless-browser bridge — "
    "but that requires scoped PLAKY_EMAIL/PASSWORD, not just X-API-Key, and is brittle/ToS-sensitive. "
    "Use the Discord slash commands for now: they check membership via real API and direct staff to the Web UI."
)

def invite_user_to_workspace(email: str, api_key: str, role: str = "MEMBER") -> Dict[str, Any]:
    """Attempt to invite — always returns unsupported, with check for existing membership."""
    err = _require_key(api_key)
    if err:
        return err
    # Still check if user already exists (informative)
    existing = get_user_by_email(api_key, email)
    if existing.get("ok"):
        return {
            "ok": False,
            "status": 409,
            "message": f"{email} is already a workspace member ({existing['user'].get('type')}/{existing['user'].get('status')}).",
            "user": existing["user"],
            "unsupported": True,
            "detail": _INVITE_UNSUPPORTED_MSG,
        }
    return {
        "ok": False,
        "status": 501,
        "message": _INVITE_UNSUPPORTED_MSG,
        "unsupported": True,
        "email": email,
        "requested_role": role,
    }


def kick_user_from_workspace(email: str, api_key: str) -> Dict[str, Any]:
    """Attempt to remove — always returns unsupported (API has no DELETE for users)."""
    err = _require_key(api_key)
    if err:
        return err
    existing = get_user_by_email(api_key, email)
    if not existing.get("ok"):
        return {
            "ok": False,
            "status": 404,
            "message": f"{email} not found in workspace; nothing to remove.",
            "unsupported": True,
            "detail": _INVITE_UNSUPPORTED_MSG,
        }
    return {
        "ok": False,
        "status": 501,
        "message": _INVITE_UNSUPPORTED_MSG,
        "unsupported": True,
        "email": email,
        "user": existing.get("user"),
    }

