"""PR staleness escalation: identity resolution (GitHub PR author -> Discord
member) and the three-tier scan logic, especially idempotency -- a tier must
fire exactly once per PR, never re-fire on a later scan."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

import main


def _pr(repo="Team-Deepiri/foo", number=1, days_old=15, author="someone", draft=False):
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
    return {
        "repo": repo,
        "number": number,
        "title": "Some PR",
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "created_at": created,
        "author_login": author,
        "draft": draft,
    }


def _member(id_=1, display_name="Ricco"):
    m = Mock(spec=discord.Member)
    m.id = id_
    m.display_name = display_name
    m.mention = f"<@{id_}>"
    m.send = AsyncMock()
    return m


@pytest.mark.asyncio
async def test_reverse_mapping_hit_resolves_instantly(monkeypatch):
    member = _member(id_=42)
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 42 else None, members=[member])
    monkeypatch.setattr(main, "_load_github_username_map", lambda: {"42": "riccowrld"})

    result = await main._resolve_discord_member_for_github_login("RiccoWrld", guild)

    assert result is member


@pytest.mark.asyncio
async def test_falls_back_to_name_fuzzy_match(monkeypatch):
    monkeypatch.setattr(main, "_load_github_username_map", lambda: {})
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "get_user_profile", lambda login, pat: {"name": "Ricardo Beale", "email": None})
    monkeypatch.setattr(main, "PLAKY_API_KEY", None)
    remember_mock = Mock()
    monkeypatch.setattr(main, "_remember_github_username", remember_mock)

    member = _member(id_=99, display_name="Ricardo Beale")
    guild = SimpleNamespace(get_member=lambda uid: member, members=[member])

    result = await main._resolve_discord_member_for_github_login("RiccoWrld", guild)

    assert result is member
    remember_mock.assert_called_once_with(99, "RiccoWrld")


@pytest.mark.asyncio
async def test_falls_back_to_plaky_email_reverse_lookup(monkeypatch):
    monkeypatch.setattr(main, "_load_github_username_map", lambda: {})
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "get_user_profile", lambda login, pat: {"name": "Unmatchable Name Xyz", "email": None})
    monkeypatch.setattr(main, "PLAKY_API_KEY", "fake-plaky-key")
    monkeypatch.setattr(main, "find_user_email", lambda names, key: "found@example.com")
    monkeypatch.setattr(main, "find_discord_id_by_email", AsyncMock(return_value="123"))
    remember_mock = Mock()
    monkeypatch.setattr(main, "_remember_github_username", remember_mock)

    member = _member(id_=123, display_name="Totally Different Display Name")
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 123 else None, members=[])

    result = await main._resolve_discord_member_for_github_login("someuser", guild)

    assert result is member
    remember_mock.assert_called_once_with(123, "someuser")


@pytest.mark.asyncio
async def test_no_confident_match_returns_none(monkeypatch):
    monkeypatch.setattr(main, "_load_github_username_map", lambda: {})
    monkeypatch.setattr(main, "GITHUB_PAT", None)
    monkeypatch.setattr(main, "PLAKY_API_KEY", None)
    guild = SimpleNamespace(get_member=lambda uid: None, members=[])

    result = await main._resolve_discord_member_for_github_login("nobody", guild)

    assert result is None


@pytest.mark.asyncio
async def test_scan_fires_each_tier_exactly_once(monkeypatch):
    """A 40-day-old PR should fire all three tiers on first scan, and none of
    them again on a second scan once they're recorded as notified."""
    pr = _pr(days_old=40)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "list_open_prs", lambda org, pat: [pr])
    monkeypatch.setattr(main, "_resolve_discord_member_for_github_login", AsyncMock(return_value=None))

    saved_state = {}

    async def fake_load(repo, number):
        return saved_state.get((repo, number), {
            "notified_2week": False, "notified_2_5week": False, "notified_1month": False, "resolved_discord_id": None,
        })

    async def fake_save(repo, number, **kwargs):
        current = saved_state.setdefault((repo, number), {
            "notified_2week": False, "notified_2_5week": False, "notified_1month": False, "resolved_discord_id": None,
        })
        for k, v in kwargs.items():
            if v is not None:
                current[k] = v
        return True

    monkeypatch.setattr(main, "load_pr_staleness", fake_load)
    monkeypatch.setattr(main, "save_pr_staleness", fake_save)

    post_mock = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_tier", post_mock)

    guild = SimpleNamespace()

    await main._scan_stale_prs(guild)
    assert post_mock.await_count == 3
    fired_tiers_first = {call.args[1] for call in post_mock.await_args_list}
    assert fired_tiers_first == {"2week", "2_5week", "1month"}

    post_mock.reset_mock()
    await main._scan_stale_prs(guild)
    post_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_skips_bot_and_draft_prs(monkeypatch):
    bot_pr = _pr(number=1, days_old=40, author="deepiri-cascade[bot]")
    draft_pr = _pr(number=2, days_old=40, draft=True)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "list_open_prs", lambda org, pat: [bot_pr, draft_pr])
    monkeypatch.setattr(main, "load_pr_staleness", AsyncMock())
    post_mock = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_tier", post_mock)

    await main._scan_stale_prs(SimpleNamespace())

    post_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_does_not_fire_tiers_not_yet_reached(monkeypatch):
    """A PR only 10 days old shouldn't fire any tier at all."""
    pr = _pr(days_old=10)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "list_open_prs", lambda org, pat: [pr])
    monkeypatch.setattr(main, "load_pr_staleness", AsyncMock(return_value={
        "notified_2week": False, "notified_2_5week": False, "notified_1month": False, "resolved_discord_id": None,
    }))
    post_mock = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_tier", post_mock)

    await main._scan_stale_prs(SimpleNamespace())

    post_mock.assert_not_awaited()
