import argparse
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

from github_discussion import GRAPHQL_URL, GitHubDiscussionError, _graphql_request


REPO_AND_CATEGORIES_QUERY = """
query RepositoryDetails($owner: String!, $name: String!) {
    repository(owner: $owner, name: $name) {
        id
        hasDiscussionsEnabled
        discussionCategories(first: 10) {
            nodes {
                id
                name
            }
        }
    }
}
"""


def _upsert_env_file(env_path: Path, values: Dict[str, str]) -> None:
    existing_lines: List[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    remaining = dict(values)
    output: List[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue

        key, _ = line.split("=", 1)
        key = key.strip()
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)

    for key, value in remaining.items():
        output.append(f"{key}={value}")

    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")


async def fetch_repo_and_categories(owner: str, repo: str, pat: str) -> Tuple[str, List[Dict[str, str]], bool]:
    variables = {"owner": owner, "name": repo}
    data = await _graphql_request(REPO_AND_CATEGORIES_QUERY, variables, pat)

    repository = data.get("data", {}).get("repository")
    if not repository:
        raise GitHubDiscussionError(f"Repository not found: {owner}/{repo}")

    repo_id = repository.get("id", "")
    categories = repository.get("discussionCategories", {}).get("nodes", [])
    has_discussions_enabled = bool(repository.get("hasDiscussionsEnabled", False))
    if not repo_id:
        raise GitHubDiscussionError("Repository ID was not returned by GitHub.")

    return repo_id, categories, has_discussions_enabled


def pick_category_id(categories: List[Dict[str, str]], category_name: str) -> str:
    for category in categories:
        name = (category.get("name") or "").strip().lower()
        if name == category_name.strip().lower():
            return category.get("id", "")
    return ""


async def _async_main(
    owner: str,
    repo: str,
    category_name: str,
    env_path: Path,
    allow_missing_category: bool,
) -> None:
    load_dotenv(override=False)

    from os import getenv

    pat = (getenv("GITHUB_PAT") or "").strip()
    if not pat:
        raise GitHubDiscussionError("Missing GITHUB_PAT in environment.")

    print(f"Using GraphQL endpoint: {GRAPHQL_URL}")
    print(f"Fetching discussion categories for {owner}/{repo}...")

    repo_id, categories, has_discussions_enabled = await fetch_repo_and_categories(owner, repo, pat)
    print(f"Repository ID: {repo_id}")
    print("Discussion categories:")
    for category in categories:
        print(f"- {category.get('name')} => {category.get('id')}")

    base_updates = {
        "REPO_ID": repo_id,
        "GITHUB_REPO_ID": repo_id,
        "GITHUB_OWNER": owner,
        "GITHUB_REPO": repo,
    }

    if not has_discussions_enabled:
        _upsert_env_file(env_path, base_updates)
        raise GitHubDiscussionError(
            "GitHub Discussions are disabled for this repository. "
            "Enable Discussions in repository Settings, then create/select a category and rerun setup.py."
        )

    if not categories:
        _upsert_env_file(env_path, base_updates)
        raise GitHubDiscussionError(
            "No discussion categories were returned. Create a category in the repository Discussions tab "
            "(for example 'Announcements') and rerun setup.py."
        )

    category_id = pick_category_id(categories, category_name)
    if not category_id:
        _upsert_env_file(env_path, base_updates)
        available = ", ".join(sorted(c.get("name", "") for c in categories))
        if allow_missing_category:
            print(
                f"Category '{category_name}' not found. "
                f"Repo ID was saved. Available categories: {available}"
            )
            print(f"Updated env file: {env_path}")
            return
        raise GitHubDiscussionError(
            f"Could not find category '{category_name}'. Available categories: {available}"
        )

    updates = {
        **base_updates,
        "CATEGORY_ID": category_id,
        "GITHUB_CATEGORY_ID": category_id,
    }
    _upsert_env_file(env_path, updates)

    print(f"Selected category '{category_name}' with ID: {category_id}")
    print(f"Updated env file: {env_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover GitHub Discussions IDs and update .env")
    parser.add_argument("--owner", required=True, help="GitHub repository owner")
    parser.add_argument("--repo", required=True, help="GitHub repository name")
    parser.add_argument(
        "--category",
        default="Announcements",
        help="Discussion category name to select (default: Announcements)",
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env file (default: .env)")
    parser.add_argument(
        "--allow-missing-category",
        action="store_true",
        help="Do not fail if the category name is not found; still write repository IDs to .env.",
    )
    args = parser.parse_args()

    asyncio.run(
        _async_main(
            args.owner,
            args.repo,
            args.category,
            Path(args.env_file),
            args.allow_missing_category,
        )
    )


if __name__ == "__main__":
    main()
