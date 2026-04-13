import asyncio
import logging
import os
from typing import Any, Dict

import httpx


GRAPHQL_URL = "https://api.github.com/graphql"
CREATE_DISCUSSION_MUTATION = """
mutation CreateDiscussion($repositoryId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
  createDiscussion(
    input: {
      repositoryId: $repositoryId
      categoryId: $categoryId
      title: $title
      body: $body
    }
  ) {
    discussion {
      url
    }
  }
}
"""


logger = logging.getLogger(__name__)


class GitHubDiscussionError(Exception):
    """Base error for GitHub Discussions failures."""


class GitHubAuthError(GitHubDiscussionError):
    """Raised for authentication/authorization failures."""


class GitHubRateLimitError(GitHubDiscussionError):
    """Raised when GitHub API rate limits requests."""


class GitHubNetworkError(GitHubDiscussionError):
    """Raised for transport-level errors."""


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise GitHubDiscussionError(f"Missing required environment variable: {name}")
    return value


def _resolve_repo_id() -> str:
    return os.getenv("REPO_ID", "").strip() or os.getenv("GITHUB_REPO_ID", "").strip()


def _resolve_category_id() -> str:
    return os.getenv("CATEGORY_ID", "").strip() or os.getenv("GITHUB_CATEGORY_ID", "").strip()


async def _graphql_request(
    query: str,
    variables: Dict[str, Any],
    pat: str,
    retries: int = 2,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {"query": query, "variables": variables}

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(GRAPHQL_URL, json=payload, headers=headers)
        except httpx.RequestError as exc:
            if attempt == retries:
                raise GitHubNetworkError(f"Network error calling GitHub GraphQL API: {exc}") from exc
            await asyncio.sleep(2**attempt)
            continue

        if response.status_code in (401, 403):
            raise GitHubAuthError("GitHub authentication failed. Check GITHUB_PAT scopes and validity.")

        if response.status_code == 429:
            if attempt == retries:
                raise GitHubRateLimitError("GitHub API rate limited the request after retries.")
            retry_after = response.headers.get("Retry-After", "1")
            wait_seconds = int(retry_after) if retry_after.isdigit() else 1
            await asyncio.sleep(wait_seconds)
            continue

        if response.status_code >= 400:
            raise GitHubDiscussionError(
                f"GitHub GraphQL request failed with HTTP {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        errors = data.get("errors") or []
        if errors:
            message = "; ".join(err.get("message", "Unknown GraphQL error") for err in errors)
            if "rate limit" in message.lower():
                if attempt == retries:
                    raise GitHubRateLimitError(f"GitHub GraphQL rate limit error: {message}")
                await asyncio.sleep(2**attempt)
                continue
            raise GitHubDiscussionError(f"GitHub GraphQL error: {message}")

        return data

    raise GitHubDiscussionError("GitHub GraphQL request failed unexpectedly.")


async def create_github_discussion(title: str, body: str) -> str:
    pat = _required_env("GITHUB_PAT")
    repo_id = _resolve_repo_id()
    category_id = _resolve_category_id()

    if not repo_id:
        raise GitHubDiscussionError("Missing REPO_ID (or GITHUB_REPO_ID) in environment.")
    if not category_id:
        raise GitHubDiscussionError("Missing CATEGORY_ID (or GITHUB_CATEGORY_ID) in environment.")

    variables = {
        "repositoryId": repo_id,
        "categoryId": category_id,
        "title": title,
        "body": body,
    }

    logger.info("Creating GitHub discussion for title: %s", title)
    data = await _graphql_request(CREATE_DISCUSSION_MUTATION, variables, pat)

    url = (
        data.get("data", {})
        .get("createDiscussion", {})
        .get("discussion", {})
        .get("url")
    )
    if not url:
        raise GitHubDiscussionError("GitHub response missing discussion URL.")

    logger.info("Created GitHub discussion: %s", url)
    return url
