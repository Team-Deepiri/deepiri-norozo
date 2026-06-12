from types import SimpleNamespace

import github


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

    async def request(self, *args, **kwargs):
        return self._responses.pop(0)


def test_invite_user_403_returns_permission_hint(monkeypatch):
    responses = [
        DummyResponse(status_code=200, json_data={"id": 12345}),
        DummyResponse(status_code=403, text='{"message":"Resource not accessible by personal access token"}'),
    ]

    def fake_request(method, url, headers=None, json=None, timeout=20):
        return responses.pop(0)

    monkeypatch.setattr(github.requests, "request", fake_request)

    result = github.invite_user(username="SomeUser", github_org="Team-Deepiri", github_pat="token")

    assert result["ok"] is False
    assert result["status"] == 403
    assert "organization invitations" in result["message"]
