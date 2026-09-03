"""New-member onboarding role matching -- especially the IT hard-exclusion,
since that role carries elevated permissions and must never be self-assignable
through the fuzzy-matched DM flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import main
from identity_match import best_match


def _role(name):
    return SimpleNamespace(name=name)


def test_it_role_never_becomes_a_candidate():
    guild = SimpleNamespace(roles=[_role("IT Operations Support"), _role("Backend Engineer")])
    candidates = main._candidate_roles_by_category(guild)
    assert "Backend Engineer" in candidates
    assert all("it operations support" not in r.name.lower() for r in candidates.values())


def test_it_word_boundary_does_not_exclude_security_or_infrastructure():
    """'Security' and 'Infrastructure' both contain the substring 'it' but must
    not be excluded -- the IT check has to be word-boundary, not substring."""
    guild = SimpleNamespace(roles=[_role("Security Engineer"), _role("Infrastructure Engineer")])
    candidates = main._candidate_roles_by_category(guild)
    assert "Cloud/Infra/Security Engineer" in candidates


def test_cloud_infra_security_requires_engineer_in_name():
    """A bare 'Security' role (no 'Engineer') must not qualify for this category."""
    guild = SimpleNamespace(roles=[_role("Security")])
    candidates = main._candidate_roles_by_category(guild)
    assert "Cloud/Infra/Security Engineer" not in candidates


def test_all_seven_categories_resolve_when_present():
    names = [
        "AI Engineer", "ML Engineer", "Data Engineer", "Cloud/Infra/Security Engineer",
        "Frontend Engineer", "Fullstack Engineer", "Backend Engineer",
    ]
    guild = SimpleNamespace(roles=[_role(n) for n in names])
    candidates = main._candidate_roles_by_category(guild)
    assert set(candidates.keys()) == set(names)


def test_role_pick_fuzzy_matches_first_word():
    guild = SimpleNamespace(roles=[_role("Backend Engineer"), _role("Frontend Engineer")])
    candidates = main._candidate_roles_by_category(guild)
    labels = list(candidates.keys())
    match = best_match("backend", labels)
    assert match is not None
    assert labels[match.index] == "Backend Engineer"


def test_it_query_never_matches_any_real_category():
    guild = SimpleNamespace(roles=[_role("IT Operations Support"), _role("Backend Engineer")])
    candidates = main._candidate_roles_by_category(guild)
    labels = list(candidates.keys())
    match = best_match("it", labels)
    assert match is None


def test_security_and_operations_support_excluded_by_fuzzy_name_not_just_id():
    """The real elevated role is named 'Security & Operations Support', not
    literally 'IT' -- must be excluded by fuzzy confidence against the known
    name, without requiring a role ID to be configured."""
    guild = SimpleNamespace(roles=[_role("Security & Operations Support"), _role("Backend Engineer")])
    candidates = main._candidate_roles_by_category(guild)
    assert "Backend Engineer" in candidates
    assert all("operations support" not in r.name.lower() for r in candidates.values())


@pytest.mark.asyncio
async def test_plain_security_still_resolves_to_cloud_infra_security_engineer(monkeypatch):
    """Regression: the elevated-role guard must not fire before the real role
    match is attempted -- typing "Security" alone is a legitimate pick for
    Cloud/Infra/Security Engineer and must not get blocked as if it were an
    attempt at Security & Operations Support."""
    security_engineer = _role("Cloud/Infra/Security Engineer")
    elevated = _role("Security & Operations Support")
    guild = SimpleNamespace(roles=[security_engineer, elevated], get_member=lambda uid: _member())
    monkeypatch.setattr(main, "_get_primary_guild", AsyncMock(return_value=guild))

    author = SimpleNamespace(id=999, bot=False, __str__=lambda self: "tester#0")
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(guild=None, author=author, content="Security", channel=channel)

    handled = await main._maybe_handle_onboarding_dm(message)

    assert handled is True
    sent_text = channel.send.call_args[0][0]
    assert "Cloud/Infra/Security Engineer" in sent_text
    assert "not self-assignable" not in sent_text


@pytest.mark.asyncio
async def test_security_ops_support_reply_still_gets_guidance_not_assigned(monkeypatch):
    guild = SimpleNamespace(roles=[_role("Backend Engineer"), _role("Security & Operations Support")], get_member=lambda uid: _member())
    monkeypatch.setattr(main, "_get_primary_guild", AsyncMock(return_value=guild))

    author = SimpleNamespace(id=999, bot=False, __str__=lambda self: "tester#0")
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(guild=None, author=author, content="Security & Operations Support", channel=channel)

    handled = await main._maybe_handle_onboarding_dm(message)

    assert handled is True
    assert "isn't self-assignable" in channel.send.call_args[0][0]


def _member(roles=None):
    return SimpleNamespace(roles=roles or [], add_roles=AsyncMock())


@pytest.mark.asyncio
async def test_plain_role_word_is_not_swallowed_as_a_github_username(monkeypatch):
    """Regression: a plain role reply like "Backend" must not get captured by
    the GitHub-username branch (_extract_github_profile_username accepts any
    bare single word) before role-matching ever runs."""
    backend_role = _role("Backend Engineer")
    guild = SimpleNamespace(roles=[backend_role], get_member=lambda uid: _member())
    monkeypatch.setattr(main, "_get_primary_guild", AsyncMock(return_value=guild))
    remember_mock = Mock()
    monkeypatch.setattr(main, "_remember_github_username", remember_mock)

    author = SimpleNamespace(id=999, bot=False, __str__=lambda self: "tester#0")
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(guild=None, author=author, content="Backend", channel=channel)

    handled = await main._maybe_handle_onboarding_dm(message)

    assert handled is True
    remember_mock.assert_not_called()
    channel.send.assert_awaited_once()
    assert "Backend Engineer" in channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_github_link_is_still_captured_as_username(monkeypatch):
    remember_mock = Mock()
    monkeypatch.setattr(main, "_remember_github_username", remember_mock)
    monkeypatch.setattr(main, "GITHUB_PAT", None)

    author = SimpleNamespace(id=999, bot=False, __str__=lambda self: "tester#0")
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(guild=None, author=author, content="https://github.com/octocat", channel=channel)

    handled = await main._maybe_handle_onboarding_dm(message)

    assert handled is True
    remember_mock.assert_called_once_with(999, "octocat")


@pytest.mark.asyncio
async def test_github_link_capture_also_caches_real_name(monkeypatch):
    """This is the whole "dynamic alias table": the moment someone
    self-reports their GitHub link, their real name is fetched and cached
    immediately -- not left to be fuzzy-guessed from a stylized handle
    ("wrenx1005") months later at kick-out time."""
    monkeypatch.setattr(main, "_remember_github_username", Mock())
    monkeypatch.setattr(main, "GITHUB_PAT", "fake-pat")
    monkeypatch.setattr(main, "get_user_profile", lambda username, pat: {"name": "Taylor Chen", "email": None})
    save_mock = AsyncMock()
    monkeypatch.setattr(main, "save_member_real_name", save_mock)

    author = SimpleNamespace(id=999, bot=False, __str__=lambda self: "tester#0")
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(guild=None, author=author, content="https://github.com/wrenx1005", channel=channel)

    handled = await main._maybe_handle_onboarding_dm(message)

    assert handled is True
    save_mock.assert_awaited_once_with(999, "Taylor Chen", "wrenx1005")


@pytest.mark.asyncio
async def test_github_link_capture_skips_cache_when_profile_has_no_name(monkeypatch):
    monkeypatch.setattr(main, "_remember_github_username", Mock())
    monkeypatch.setattr(main, "GITHUB_PAT", "fake-pat")
    monkeypatch.setattr(main, "get_user_profile", lambda username, pat: {"name": None, "email": None})
    save_mock = AsyncMock()
    monkeypatch.setattr(main, "save_member_real_name", save_mock)

    author = SimpleNamespace(id=999, bot=False, __str__=lambda self: "tester#0")
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(guild=None, author=author, content="https://github.com/octocat", channel=channel)

    handled = await main._maybe_handle_onboarding_dm(message)

    assert handled is True
    save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_github_link_capture_falls_back_to_discord_name_when_github_has_none(monkeypatch):
    """Not just onboarding, and not just GitHub: when GitHub's public profile
    has no real name set, a "First Last"-shaped Discord global_name is still
    a good identity-search candidate, so it gets cached too."""
    monkeypatch.setattr(main, "_remember_github_username", Mock())
    monkeypatch.setattr(main, "GITHUB_PAT", "fake-pat")
    monkeypatch.setattr(main, "get_user_profile", lambda username, pat: {"name": None, "email": None})
    save_mock = AsyncMock()
    monkeypatch.setattr(main, "save_member_real_name", save_mock)

    author = SimpleNamespace(id=999, bot=False, global_name="Jordan Rivera", display_name="jr_dev", __str__=lambda self: "tester#0")
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(guild=None, author=author, content="https://github.com/octocat", channel=channel)

    handled = await main._maybe_handle_onboarding_dm(message)

    assert handled is True
    save_mock.assert_awaited_once_with(999, "Jordan Rivera", "octocat")


@pytest.mark.asyncio
async def test_github_link_capture_ignores_non_name_shaped_discord_handle(monkeypatch):
    """A stylized single-word handle isn't "First Last"-shaped, so it must not
    get cached as if it were a real name."""
    monkeypatch.setattr(main, "_remember_github_username", Mock())
    monkeypatch.setattr(main, "GITHUB_PAT", "fake-pat")
    monkeypatch.setattr(main, "get_user_profile", lambda username, pat: {"name": None, "email": None})
    save_mock = AsyncMock()
    monkeypatch.setattr(main, "save_member_real_name", save_mock)

    author = SimpleNamespace(id=999, bot=False, global_name="wrenx1005", display_name="wrenx1005", __str__=lambda self: "tester#0")
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(guild=None, author=author, content="https://github.com/octocat", channel=channel)

    handled = await main._maybe_handle_onboarding_dm(message)

    assert handled is True
    save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_it_reply_gets_explicit_no_self_assign_guidance(monkeypatch):
    guild = SimpleNamespace(roles=[_role("IT Operations Support")], get_member=lambda uid: _member())
    monkeypatch.setattr(main, "_get_primary_guild", AsyncMock(return_value=guild))

    author = SimpleNamespace(id=999, bot=False, __str__=lambda self: "tester#0")
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(guild=None, author=author, content="IT", channel=channel)

    handled = await main._maybe_handle_onboarding_dm(message)

    assert handled is True
    assert "isn't self-assignable" in channel.send.call_args[0][0]
