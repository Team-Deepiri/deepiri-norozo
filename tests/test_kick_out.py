"""kick-out's reply routing -- the companion thread for a message in an
auto-thread channel (like #support-tickets) can be created as a side effect
that lands AFTER on_message already fired, so message.thread captured once at
handler-start can still be None even though the thread exists by the time the
final summary is ready to send (kick + GitHub removal + email resolution all
take real async time first)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

import main


@pytest.mark.asyncio
async def test_summary_lands_in_thread_discovered_after_handler_started(monkeypatch):
    monkeypatch.setattr(main, "KICK_OUT_COMMAND_CHANNEL_IDS", {100})
    monkeypatch.setattr(main, "_is_staff", lambda member: True)

    target = SimpleNamespace(
        id=2,
        guild_permissions=SimpleNamespace(administrator=False),
        get_role=lambda rid: None,
        kick=AsyncMock(),
        mention="@target",
    )
    monkeypatch.setattr(main, "_resolve_kick_target", lambda message, raw: target)
    monkeypatch.setattr(main, "_get_github_username_for_member", lambda m: None)
    monkeypatch.setattr(main, "_find_github_username_in_profiles_channel", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_send_termination_notice", AsyncMock(return_value="no email found — sent via Discord DM"))
    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", None)

    # message.thread is None at handler-start (the real-world race), but by the
    # time we're ready to send, the channel's thread cache has it -- this is
    # what get_thread() simulates finding.
    companion_thread = SimpleNamespace(send=AsyncMock())
    channel = SimpleNamespace(
        id=100,
        get_thread=lambda mid: companion_thread,
        fetch_message=AsyncMock(),
        send=AsyncMock(),
    )
    author = Mock(spec=discord.Member)
    author.id = 1
    message = SimpleNamespace(
        id=999,
        guild=SimpleNamespace(),
        channel=channel,
        thread=None,
        content="kick out <@2>",
        author=author,
    )

    handled = await main._maybe_handle_kick_out_command(message)

    assert handled is True
    companion_thread.send.assert_awaited_once()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_plaky_kick_falls_back_to_roster_match_when_email_on_file_is_stale(monkeypatch):
    """Real incident: a member self-reported a garbled email
    ('ryansaenz@gmaileleler.com') that got persisted as their email on file,
    then GitHub org removal later resolved their real login during the same
    kick-out. The bridge kick on the stale email 404s ('User not found in
    workspace') -- this must retry via the Plaky roster fuzzy match keyed on
    that GitHub login instead of just reporting failure."""
    target = SimpleNamespace(id=42)
    monkeypatch.setattr(main, "_get_github_username_for_member", lambda m: "absolemzz")
    monkeypatch.setattr(main, "_member_name_hints", lambda m: ["Absolem"])
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value="ryansaenz@gmaileleler.com"))
    monkeypatch.setattr(main, "PLAKY_API_KEY", "test-key")

    kick_calls = []

    async def fake_kick(email):
        kick_calls.append(email)
        if email == "ryansaenz@gmaileleler.com":
            return {"success": False, "not_found": True, "error": "User not found in workspace"}
        return {"success": True}

    monkeypatch.setattr(main, "call_plaky_bridge_kick", fake_kick)
    find_user_email_mock = Mock(return_value="ryan.saenzv@gmail.com")
    monkeypatch.setattr(main, "find_user_email", find_user_email_mock)
    monkeypatch.setattr(main, "GITHUB_PAT", "test-pat")
    monkeypatch.setattr(main, "get_user_profile", Mock(return_value={"email": None, "name": "Ryan S."}))
    remember_mock = AsyncMock()
    monkeypatch.setattr(main, "remember_user_data", remember_mock)

    result = await main._maybe_handle_plaky_kick_on_kickout(SimpleNamespace(), target)

    assert kick_calls == ["ryansaenz@gmaileleler.com", "ryan.saenzv@gmail.com"]
    assert "ryan.saenzv@gmail.com" in result
    assert "stale" in result.lower()
    remember_mock.assert_awaited_once_with(42, email="ryan.saenzv@gmail.com", overwrite=True)
    # The raw login alone rarely resembles a Plaky roster entry -- the GitHub
    # profile's real name ("Ryan S.") must be in the candidate list too.
    searched_names = find_user_email_mock.call_args[0][0]
    assert "Ryan S." in searched_names
    assert "absolemzz" in searched_names


@pytest.mark.asyncio
async def test_plaky_kick_not_found_with_no_roster_match_reports_failure(monkeypatch):
    """Roster fallback finds nothing better -- must still surface the original
    not-found failure, not silently succeed or crash."""
    target = SimpleNamespace(id=43)
    monkeypatch.setattr(main, "_get_github_username_for_member", lambda m: "someuser")
    monkeypatch.setattr(main, "_member_name_hints", lambda m: [])
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value="stale@example.com"))
    monkeypatch.setattr(main, "PLAKY_API_KEY", "test-key")
    monkeypatch.setattr(
        main,
        "call_plaky_bridge_kick",
        AsyncMock(return_value={"success": False, "not_found": True, "error": "User not found in workspace"}),
    )
    monkeypatch.setattr(main, "find_user_email", Mock(return_value=None))

    result = await main._maybe_handle_plaky_kick_on_kickout(SimpleNamespace(), target)

    assert result == "Plaky: kick failed (User not found in workspace)"


@pytest.mark.asyncio
async def test_org_roster_fallback_matches_truncated_discord_handle(monkeypatch):
    """Real case: Discord handle 'mahlaka.' vs GitHub login 'samimahlaka' -- no
    explicit mapping, not found in #github-profiles, but the org roster itself
    has a fuzzy-matchable candidate."""
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake-token")
    monkeypatch.setattr(main, "list_org_members", lambda org, pat: ["samimahlaka", "someoneelse"])
    monkeypatch.setattr(main, "get_user_profile", lambda username, pat: {"name": None, "email": None})
    remember_mock = AsyncMock()
    monkeypatch.setattr(main, "_remember_github_username", remember_mock)

    member = Mock(spec=discord.Member)
    member.id = 42
    member.display_name = "mahlaka."
    member.global_name = None
    member.name = "mahlaka."

    result = await main._find_github_username_via_org_roster(member)

    assert result == "samimahlaka"
    remember_mock.assert_called_once_with(42, "samimahlaka")


@pytest.mark.asyncio
async def test_org_roster_fallback_refuses_on_no_confident_match(monkeypatch):
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake-token")
    monkeypatch.setattr(main, "list_org_members", lambda org, pat: ["completelyunrelated"])
    monkeypatch.setattr(main, "get_user_profile", lambda username, pat: {"name": None, "email": None})

    member = Mock(spec=discord.Member)
    member.id = 42
    member.display_name = "xyz123nomatch"
    member.global_name = None
    member.name = "xyz123nomatch"

    result = await main._find_github_username_via_org_roster(member)

    assert result is None


@pytest.mark.asyncio
async def test_org_roster_fallback_matches_via_github_profile_real_name(monkeypatch):
    """Real incident: org member 'shan-versc' has GitHub profile real name
    'Shanley V.'; Discord global_name 'Shanley' doesn't fuzzy-match the bare
    login 'shan-versc' (ratio ~0.47, correctly refused) but does match the
    real name via the first-name-token rule -- the resolver must check both
    fields, not just the login."""
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake-token")
    monkeypatch.setattr(main, "list_org_members", lambda org, pat: ["shan-versc", "someoneelse"])
    profiles = {"shan-versc": {"name": "Shanley V.", "email": None}, "someoneelse": {"name": None, "email": None}}
    monkeypatch.setattr(main, "get_user_profile", lambda username, pat: profiles[username])
    monkeypatch.setattr(main, "_remember_github_username", AsyncMock())

    member = Mock(spec=discord.Member)
    member.id = 42
    member.display_name = "Shanley"
    member.global_name = "Shanley"
    member.name = "shanley_h"

    result = await main._find_github_username_via_org_roster(member)

    assert result == "shan-versc"


def test_plain_staff_role_without_security_ops_cannot_dispatch(monkeypatch):
    """A member with only STAFF_ROLE_ID (no Security & Operations Support role)
    must NOT be able to use kick-out -- it needs the shared
    _is_staff_or_security_ops gate, not the weaker _is_staff alone."""
    monkeypatch.setattr(main, "STAFF_ROLE_ID", 10)
    monkeypatch.setattr(main, "IT_OPERATIONS_SUPPORT_ROLE_ID", 20)

    member = Mock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(administrator=False)
    member.get_role = lambda rid: SimpleNamespace(id=10) if rid == 10 else None

    assert main._is_staff(member) is True
    assert main._is_staff_or_security_ops(member) is True  # STAFF_ROLE_ID alone still qualifies via _is_staff


def test_security_ops_role_without_staff_role_can_dispatch(monkeypatch):
    """The reverse case: Security & Operations Support alone (no STAFF_ROLE_ID,
    not an admin) must still be allowed -- this is the actual gap that existed
    before (kick-out only checked _is_staff, missing this role entirely)."""
    monkeypatch.setattr(main, "STAFF_ROLE_ID", 10)
    monkeypatch.setattr(main, "IT_OPERATIONS_SUPPORT_ROLE_ID", 20)

    member = Mock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(administrator=False)
    member.get_role = lambda rid: SimpleNamespace(id=20) if rid == 20 else None

    assert main._is_staff(member) is False
    assert main._is_staff_or_security_ops(member) is True


def test_neither_role_nor_admin_cannot_dispatch(monkeypatch):
    monkeypatch.setattr(main, "STAFF_ROLE_ID", 10)
    monkeypatch.setattr(main, "IT_OPERATIONS_SUPPORT_ROLE_ID", 20)

    member = Mock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(administrator=False)
    member.get_role = lambda rid: None

    assert main._is_staff_or_security_ops(member) is False
