import pytest

import github_discussion
from github_discussion import GitHubAuthError, GitHubRateLimitError


class DummyResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json_data


class DummyClient:
    def __init__(self, responses):
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_create_discussion_success(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "token")
    monkeypatch.setenv("REPO_ID", "repo_node")
    monkeypatch.setenv("CATEGORY_ID", "cat_node")

    responses = [
        DummyResponse(
            status_code=200,
            json_data={
                "data": {
                    "createDiscussion": {
                        "discussion": {"url": "https://github.com/org/repo/discussions/1"}
                    }
                }
            },
        )
    ]

    monkeypatch.setattr(github_discussion.httpx, "AsyncClient", lambda timeout=20.0: DummyClient(responses))

    url = await github_discussion.create_github_discussion("Hello", "Body")
    assert url == "https://github.com/org/repo/discussions/1"


@pytest.mark.asyncio
async def test_create_discussion_auth_error(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "bad")
    monkeypatch.setenv("REPO_ID", "repo_node")
    monkeypatch.setenv("CATEGORY_ID", "cat_node")

    responses = [DummyResponse(status_code=401, text="Unauthorized")]
    monkeypatch.setattr(github_discussion.httpx, "AsyncClient", lambda timeout=20.0: DummyClient(responses))

    with pytest.raises(GitHubAuthError):
        await github_discussion.create_github_discussion("Hello", "Body")


@pytest.mark.asyncio
async def test_create_discussion_rate_limit_retry(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "token")
    monkeypatch.setenv("REPO_ID", "repo_node")
    monkeypatch.setenv("CATEGORY_ID", "cat_node")

    responses = [
        DummyResponse(status_code=429, text="Too Many Requests", headers={"Retry-After": "0"}),
        DummyResponse(
            status_code=200,
            json_data={
                "data": {
                    "createDiscussion": {
                        "discussion": {"url": "https://github.com/org/repo/discussions/2"}
                    }
                }
            },
        ),
    ]

    monkeypatch.setattr(github_discussion.httpx, "AsyncClient", lambda timeout=20.0: DummyClient(responses))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(github_discussion.asyncio, "sleep", no_sleep)

    url = await github_discussion.create_github_discussion("Hello", "Body")
    assert url.endswith("/2")


@pytest.mark.asyncio
async def test_create_discussion_rate_limit_exhausted(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "token")
    monkeypatch.setenv("REPO_ID", "repo_node")
    monkeypatch.setenv("CATEGORY_ID", "cat_node")

    responses = [
        DummyResponse(status_code=429, text="Too Many Requests", headers={"Retry-After": "0"}),
        DummyResponse(status_code=429, text="Too Many Requests", headers={"Retry-After": "0"}),
        DummyResponse(status_code=429, text="Too Many Requests", headers={"Retry-After": "0"}),
    ]

    monkeypatch.setattr(github_discussion.httpx, "AsyncClient", lambda timeout=20.0: DummyClient(responses))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(github_discussion.asyncio, "sleep", no_sleep)

    with pytest.raises(GitHubRateLimitError):
        await github_discussion.create_github_discussion("Hello", "Body")
