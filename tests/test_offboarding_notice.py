"""_send_offboarding_notice's identity resolution: checks the dynamic identity
cache (member_emails, populated automatically at onboarding when someone
self-reports a GitHub link) before ever needing to fuzzy-match a stylized
Discord handle against anything."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main


def _target(id_=1, display_name="wrenx1005"):
    return SimpleNamespace(
        id=id_,
        display_name=display_name,
        global_name="wrenx",
        name="wrenx1005",
        send=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_cached_real_name_used_without_needing_github_username(monkeypatch):
    """The exact wrenx1005 scenario: no github_username resolves at kick time,
    but a real name was cached at onboarding -- that cached name alone should
    still get tried against Plaky instead of just the bare Discord handle."""
    monkeypatch.setattr(
        main, "load_member_profile",
        AsyncMock(return_value={"email": None, "real_name": "Taylor Chen", "github_username": None}),
    )
    monkeypatch.setattr(main, "PLAKY_API_KEY", "fake-key")
    # find_user_email is sync (called via asyncio.to_thread) in main.py
    find_calls = {}

    def fake_find_user_email(candidates, key):
        find_calls["candidates"] = candidates
        return "chentaylor206@example.com"

    monkeypatch.setattr(main, "find_user_email", fake_find_user_email)
    monkeypatch.setattr(main, "send_email", lambda *a, **k: (True, None))

    target = _target()
    result = await main._send_offboarding_notice(target, None, subject="Subject", body="Body")

    assert result == "emailed to chentaylor206@example.com"
    assert "Taylor Chen" in find_calls["candidates"]


@pytest.mark.asyncio
async def test_cached_email_short_circuits_everything_else(monkeypatch):
    monkeypatch.setattr(
        main, "load_member_profile",
        AsyncMock(return_value={"email": "self-reported@example.com", "real_name": None, "github_username": None}),
    )
    monkeypatch.setattr(main, "send_email", lambda *a, **k: (True, None))
    find_mock = AsyncMock()
    monkeypatch.setattr(main, "find_user_email", find_mock)

    target = _target()
    result = await main._send_offboarding_notice(target, "some-github-username", subject="Subject", body="Body")

    assert result == "emailed to self-reported@example.com"
    find_mock.assert_not_called()


@pytest.mark.asyncio
async def test_cached_github_username_used_when_none_passed_in(monkeypatch):
    monkeypatch.setattr(
        main, "load_member_profile",
        AsyncMock(return_value={"email": None, "real_name": None, "github_username": "wrenx1005"}),
    )
    monkeypatch.setattr(main, "GITHUB_PAT", "fake-pat")
    monkeypatch.setattr(main, "get_user_profile", lambda username, pat: {"name": "Taylor Chen", "email": "david@example.com"})
    monkeypatch.setattr(main, "send_email", lambda *a, **k: (True, None))

    target = _target()
    result = await main._send_offboarding_notice(target, None, subject="Subject", body="Body")

    assert result == "emailed to david@example.com"


@pytest.mark.asyncio
async def test_falls_back_to_dm_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr(
        main, "load_member_profile",
        AsyncMock(return_value={"email": None, "real_name": None, "github_username": None}),
    )
    monkeypatch.setattr(main, "PLAKY_API_KEY", None)

    target = _target()
    result = await main._send_offboarding_notice(target, None, subject="Subject", body="Body")

    assert result == "no email found — sent via Discord DM"
    target.send.assert_awaited_once()
