import os
import time
from typing import Any

import requests

PLAKY_API_BASE = os.getenv("PLAKY_API_BASE", "https://api.plaky.com/v2")


def _request_with_rate_limit_retry(method: str, url: str, headers: dict[str, str], json: dict[str, Any] | None = None, params: dict[str, Any] | None = None, retries: int = 2) -> requests.Response:
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


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_task(title: str, description: str, priority: str, api_key: str) -> dict[str, Any]:
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


def get_tasks(api_key: str, status: str = "open") -> dict[str, Any]:
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
        tasks: list[dict[str, Any]]

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
