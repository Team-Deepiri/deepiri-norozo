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


def test_list_org_members_single_page(monkeypatch):
    response = DummyResponse(status_code=200, json_data=[{"login": "alice"}, {"login": "bob"}])

    def fake_request(method, url, headers=None, json=None, timeout=20):
        return response

    monkeypatch.setattr(github.requests, "request", fake_request)

    usernames = github.list_org_members("Team-Deepiri", "token")

    assert usernames == ["alice", "bob"]


def test_list_org_members_follows_pagination(monkeypatch):
    page1 = DummyResponse(
        status_code=200,
        json_data=[{"login": "alice"}],
        headers={"Link": '<https://api.github.com/orgs/Team-Deepiri/members?page=2>; rel="next"'},
    )
    page2 = DummyResponse(status_code=200, json_data=[{"login": "bob"}])
    responses = [page1, page2]

    def fake_request(method, url, headers=None, json=None, timeout=20):
        return responses.pop(0)

    monkeypatch.setattr(github.requests, "request", fake_request)

    usernames = github.list_org_members("Team-Deepiri", "token")

    assert usernames == ["alice", "bob"]


def test_list_org_members_missing_config_returns_empty(monkeypatch):
    assert github.list_org_members("", "token") == []
    assert github.list_org_members("Team-Deepiri", "") == []


def test_get_user_profile_returns_email_and_name(monkeypatch):
    response = DummyResponse(status_code=200, json_data={"email": "ricco@example.com", "name": "Ricardo Beale"})

    def fake_request(method, url, headers=None, json=None, timeout=20):
        return response

    monkeypatch.setattr(github.requests, "request", fake_request)

    profile = github.get_user_profile("RiccoWrld", "token")

    assert profile == {"email": "ricco@example.com", "name": "Ricardo Beale"}


def test_get_user_profile_missing_fields_returns_none(monkeypatch):
    response = DummyResponse(status_code=200, json_data={})

    def fake_request(method, url, headers=None, json=None, timeout=20):
        return response

    monkeypatch.setattr(github.requests, "request", fake_request)

    profile = github.get_user_profile("someone", "token")

    assert profile == {"email": None, "name": None}


def test_get_user_email_wraps_get_user_profile(monkeypatch):
    response = DummyResponse(status_code=200, json_data={"email": "x@example.com", "name": "X Y"})

    def fake_request(method, url, headers=None, json=None, timeout=20):
        return response

    monkeypatch.setattr(github.requests, "request", fake_request)

    assert github.get_user_email("x", "token") == "x@example.com"
