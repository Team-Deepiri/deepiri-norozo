import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests

from identity_match import best_match


logger = logging.getLogger("deepiri.plaky")


# Was "https://api.plaky.com/v2" -- that path doesn't exist and every request to
# it returned a generic 401 "Missing authorization" (Plaky's catch-all for an
# unrecognized route, not a real auth failure). Confirmed the correct base URL
# and header scheme by reading deepiri-boardman's working Plaky client
# (boardman/plaky/client.py) and its live container env, then verified directly
# against the API with Norozo's own key.
PLAKY_API_BASE = os.getenv("PLAKY_API_BASE", "https://api.plaky.com/v1/public")
PLAKY_USERS_PAGE_LIMIT = 20  # safety cap on pagination, not an expected roster size


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
        "X-API-Key": api_key,
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


def _fetch_all_plaky_users(api_key: str) -> List[Dict[str, Any]]:
    """Paginated GET /users -- Plaky's public API docs describe this endpoint but
    say it requires admin/project-owner privileges, and don't document a
    board-membership-by-name lookup. Fail quietly (anything-but-200 on any page)
    rather than treating an unverified endpoint as guaranteed -- callers should
    already have a fallback."""
    if not api_key:
        logger.warning("_fetch_all_plaky_users: missing api_key")
        return []
    users: List[Dict[str, Any]] = []
    for page in range(1, PLAKY_USERS_PAGE_LIMIT + 1):
        try:
            response = _request_with_rate_limit_retry(
                "GET", f"{PLAKY_API_BASE}/users", headers=_headers(api_key), params={"page": page}
            )
        except requests.RequestException:
            logger.exception("_fetch_all_plaky_users: GET /users request failed (page=%s)", page)
            break
        if response.status_code != 200:
            logger.warning(
                "_fetch_all_plaky_users: GET /users returned %s (page=%s): %s",
                response.status_code, page, response.text[:300],
            )
            break
        try:
            payload = response.json()
        except ValueError:
            logger.warning("_fetch_all_plaky_users: GET /users returned non-JSON body (page=%s)", page)
            break
        page_users = payload if isinstance(payload, list) else payload.get("data") or payload.get("users") or []
        if not page_users:
            break
        users.extend(page_users)
        if not (isinstance(payload, dict) and payload.get("hasMore")):
            break
    if not users:
        logger.warning("_fetch_all_plaky_users: no Plaky users fetched at all")
    return users


def find_user_email(names: List[str], api_key: str, known_emails: Optional[List[str]] = None) -> Optional[str]:
    """Throw every known identifier signal at Plaky at once, rather than trying
    one candidate name and giving up: exact email match first (an email that's
    already public on the person's GitHub profile matching a Plaky user's email
    is about as certain as identity resolution gets), then the single
    best-scoring fuzzy name match across ALL candidate name strings (GitHub real
    name, GitHub login, Discord display name/global name/username, ...) rather
    than stopping at the first candidate that merely clears the threshold --
    a later, weaker candidate could otherwise shadow an earlier, stronger one
    that happened to score just under some earlier candidate's noise.
    """
    users = _fetch_all_plaky_users(api_key)
    if not users:
        return None

    known_emails_norm = {e.strip().lower() for e in (known_emails or []) if e and e.strip()}
    if known_emails_norm:
        for user in users:
            user_emails = _user_emails(user)
            if {e.lower() for e in user_emails} & known_emails_norm:
                logger.info("find_user_email: exact email match on %s", user_emails[0])
                return user_emails[0]

    display_names = [
        str(user.get("name") or user.get("displayName") or user.get("username") or "")
        for user in users
    ]
    leading_candidates = None  # computed lazily, shared across all query names

    best = None
    best_query = None
    for name in names or []:
        if not name or not name.strip():
            continue
        m = best_match(name, display_names)
        if m is not None and (best is None or m.score > best.score):
            best, best_query = m, name
        # Discord/GitHub account handles aren't clean human-typed names
        # ("wren.h._83898") -- they carry random suffixes that make every token
        # required to line up, which kills an otherwise-unique first-name match.
        # Retry on just the leading name token from both sides. Still goes
        # through best_match's ambiguity refusal, so two people sharing a first
        # name still won't get guessed at.
        leading_query = _leading_name_token(name)
        if leading_query:
            if leading_candidates is None:
                leading_candidates = [_leading_name_token(n) for n in display_names]
            m2 = best_match(leading_query, leading_candidates)
            if m2 is not None and (best is None or m2.score > best.score):
                best, best_query = m2, leading_query

    if best is None:
        logger.warning(
            "find_user_email: no confident match for any of %s among %s Plaky users (sample: %s)",
            names, len(users), display_names[:10],
        )
        return None
    logger.info("find_user_email: matched %r -> %r (score=%s)", best_query, display_names[best.index], best.score)
    emails = _user_emails(users[best.index])
    if not emails:
        logger.warning("find_user_email: matched %r but that Plaky user has no email field set", display_names[best.index])
    return emails[0] if emails else None


def find_user_email_by_name(name: str, api_key: str) -> Optional[str]:
    """Single-candidate convenience wrapper over find_user_email."""
    return find_user_email([name], api_key)


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
