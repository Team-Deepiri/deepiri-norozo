import time
from urllib.parse import urlparse
from typing import Any, Dict, Optional

import requests


GITHUB_API_BASE = "https://api.github.com"


def _normalize_org_name(github_org: str) -> str:
    org = (github_org or "").strip()
    if not org:
        return ""
    if org.startswith("http://") or org.startswith("https://"):
        parsed = urlparse(org)
        return parsed.path.strip("/").split("/")[0] if parsed.path else ""
    return org.strip("/")


def _request_with_rate_limit_retry(method: str, url: str, headers: Dict[str, str], json: Optional[Dict[str, Any]] = None, retries: int = 2) -> requests.Response:
    """Perform an HTTP request and retry on 429 using Retry-After when available."""
    for attempt in range(retries + 1):
        response = requests.request(method=method, url=url, headers=headers, json=json, timeout=20)

        if response.status_code != 429:
            return response

        if attempt == retries:
            return response

        retry_after = response.headers.get("Retry-After")
        wait_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 2
        time.sleep(wait_seconds)

    return response


def _get_user_id(username: str, github_pat: str) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {github_pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{GITHUB_API_BASE}/users/{username}"
    response = _request_with_rate_limit_retry("GET", url, headers=headers)

    if response.status_code in (401, 403):
        return {
            "ok": False,
            "status": response.status_code,
            "message": (
                "GitHub authentication failed while resolving the username. "
                "Check that GITHUB_PAT is valid and has permission to read GitHub users."
            ),
        }

    if response.status_code != 200:
        return {
            "ok": False,
            "status": response.status_code,
            "message": f"Could not resolve GitHub user '{username}'.",
        }

    payload = response.json()
    return {"ok": True, "user_id": payload.get("id")}


def invite_user(username: str, github_org: str, github_pat: str) -> Dict[str, Any]:
    """Invite a GitHub user to the configured org by username."""
    normalized_org = _normalize_org_name(github_org)
    if not github_pat or not normalized_org:
        return {
            "ok": False,
            "status": 400,
            "message": "GitHub configuration is missing (GITHUB_PAT or GITHUB_ORG).",
        }

    user_lookup = _get_user_id(username=username, github_pat=github_pat)
    if not user_lookup.get("ok"):
        return user_lookup

    invitee_id = user_lookup.get("user_id")
    if not invitee_id:
        return {
            "ok": False,
            "status": 404,
            "message": f"GitHub user '{username}' does not have a resolvable id.",
        }

    headers = {
        "Authorization": f"Bearer {github_pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{GITHUB_API_BASE}/orgs/{normalized_org}/invitations"
    body = {"invitee_id": invitee_id}

    response = _request_with_rate_limit_retry("POST", url, headers=headers, json=body)

    if response.status_code in (201, 202):
        return {
            "ok": True,
            "status": response.status_code,
            "message": f"Invite sent to GitHub user '{username}'.",
        }

    if response.status_code == 429:
        return {
            "ok": False,
            "status": 429,
            "message": "GitHub API rate limited the request. Please retry shortly.",
        }

    if response.status_code == 403:
        return {
            "ok": False,
            "status": 403,
            "message": (
                "GitHub invite failed (403): the PAT does not have permission to create organization "
                "invitations. Use an org owner/admin token with the required organization permissions "
                "(and SSO authorization if your org requires it)."
            ),
        }

    if response.status_code == 422:
        return {
            "ok": False,
            "status": 422,
            "message": f"Could not invite '{username}' (already invited, already in org, or invalid target).",
        }

    return {
        "ok": False,
        "status": response.status_code,
        "message": f"GitHub invite failed ({response.status_code}): {response.text[:200]}",
    }
