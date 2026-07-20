from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import discord
import pytest

import main


class FakeApprovalView:
    def __init__(self, dev_team_role_id: int, available_role_id: int):
        self.dev_team_role_id = dev_team_role_id
        self.available_role_id = available_role_id


class FakeRole:
    def __init__(self, role_id: int, mention: str, members=None):
        self.id = role_id
        self.mention = mention
        self.members = members or []


class FakeMember:
    def __init__(self, member_id: int, mention: str, *, roles=None, is_bot: bool = False):
        self.id = member_id
        self.mention = mention
        self.roles = roles or []
        self.bot = is_bot
        self.add_roles = AsyncMock()
        self.send = AsyncMock()

    def get_role(self, role_id: int):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None


class FakeStaffMember(FakeMember):
    def __init__(self, member_id: int, mention: str):
        super().__init__(member_id, mention)
        self.guild_permissions = SimpleNamespace(manage_roles=True, administrator=False)


class FakeGuild:
    def __init__(self, roles):
        self.roles = {role.id: role for role in roles}
        self.id = 999

    def get_role(self, role_id: int):
        return self.roles.get(role_id)


class FakeGuildWithMember:
    def __init__(self, member):
        self.member = member
        self.id = 999

    def get_member(self, member_id: int):
        return self.member if member_id == self.member.id else None

    async def fetch_member(self, member_id: int):
        if member_id == self.member.id:
            return self.member
        raise discord.NotFound(response=cast(Any, SimpleNamespace(status=404)), message="missing")


@pytest.mark.asyncio
async def test_github_invite_request_requires_support_channel(monkeypatch):
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, mention="@test-user"),
        channel=SimpleNamespace(id=777),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)

    await main.handle_github_invite_request(cast(discord.Interaction, interaction), "SomeUser")

    interaction.response.send_message.assert_awaited_once_with(
        "Please run /github-invite-request in the support tickets channel.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_github_invite_request_sends_invite_automatically(monkeypatch):
    user = SimpleNamespace(id=10, mention="@test-user", send=AsyncMock())
    interaction = SimpleNamespace(
        user=user,
        channel=SimpleNamespace(id=888),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", None)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "token")
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)
    monkeypatch.setattr(
        main,
        "invite_user",
        lambda *, username, github_org, github_pat: {"ok": True, "status": 201, "message": "Invite sent"},
    )

    await main.handle_github_invite_request(cast(discord.Interaction, interaction), "HTTPS://github.com/SomeUser")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    user.send.assert_awaited_once()
    dm_text = user.send.await_args.args[0]
    assert "https://github.com/orgs/Team-Deepiri/invitation" in dm_text
    interaction.edit_original_response.assert_awaited_once_with(
        content="Your GitHub invite has been sent! Check your DMs for the link."
    )


@pytest.mark.asyncio
async def test_github_invite_request_reports_api_failure(monkeypatch):
    user = SimpleNamespace(id=10, mention="@test-user", send=AsyncMock())
    interaction = SimpleNamespace(
        user=user,
        channel=SimpleNamespace(id=888),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", None)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "token")
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)
    monkeypatch.setattr(
        main,
        "invite_user",
        lambda *, username, github_org, github_pat: {"ok": False, "message": "User not found on GitHub."},
    )

    await main.handle_github_invite_request(cast(discord.Interaction, interaction), "SomeUser")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    user.send.assert_not_awaited()
    interaction.edit_original_response.assert_awaited_once_with(
        content="User not found on GitHub."
    )


@pytest.mark.asyncio
async def test_github_invite_request_adds_user_to_selected_team(monkeypatch):
    user = SimpleNamespace(id=10, mention="@test-user", send=AsyncMock())
    interaction = SimpleNamespace(
        user=user,
        channel=SimpleNamespace(id=888),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", None)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "token")
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)
    monkeypatch.setattr(main, "GITHUB_SUPPORT_TEAM_SLUG", "support-team")
    monkeypatch.setattr(main, "GITHUB_IT_TEAM_SLUG", "it-management-team")
    monkeypatch.setattr(
        main,
        "invite_user",
        lambda *, username, github_org, github_pat: {"ok": True, "status": 201, "message": "Invite sent"},
    )
    add_team_mock = Mock(return_value={"ok": True, "status": 200, "message": "Added to team"})
    monkeypatch.setattr(main, "add_user_to_team", add_team_mock)

    await main.handle_github_invite_request(cast(discord.Interaction, interaction), "SomeUser", team="support")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    add_team_mock.assert_called_once_with(
        username="someuser",
        github_org="Team-Deepiri",
        github_pat="token",
        team_slug="support-team",
    )
    interaction.edit_original_response.assert_awaited_once_with(
        content="Your GitHub invite has been sent and you were added to the support team."
    )


@pytest.mark.asyncio
async def test_github_invite_request_reports_team_assignment_failure(monkeypatch):
    user = SimpleNamespace(id=10, mention="@test-user", send=AsyncMock())
    interaction = SimpleNamespace(
        user=user,
        channel=SimpleNamespace(id=888),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", None)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "token")
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)
    monkeypatch.setattr(main, "GITHUB_SUPPORT_TEAM_SLUG", "support-team")
    monkeypatch.setattr(main, "GITHUB_IT_TEAM_SLUG", "it-management-team")
    monkeypatch.setattr(
        main,
        "invite_user",
        lambda *, username, github_org, github_pat: {"ok": True, "status": 201, "message": "Invite sent"},
    )
    monkeypatch.setattr(main, "add_user_to_team", Mock(return_value={"ok": False, "message": "Team not found."}))

    await main.handle_github_invite_request(cast(discord.Interaction, interaction), "SomeUser", team="support")

    interaction.edit_original_response.assert_awaited_once_with(
        content="Your GitHub invite has been sent, but there was an issue adding you to the support team: Team not found.."
    )


@pytest.mark.asyncio
async def test_support_session_message_dms_support_team(monkeypatch):
    author = FakeMember(10, "@author")
    support_member_one = FakeMember(11, "@support1")
    support_member_two = FakeMember(12, "@support2")
    bot_member = FakeMember(13, "@bot", is_bot=True)

    support_role = FakeRole(777, "@ITSupport", members=[author, support_member_one, support_member_two, bot_member])
    guild = FakeGuild([support_role])
    channel = SimpleNamespace(id=888, name="support-sessions")
    message = SimpleNamespace(
        author=author,
        channel=channel,
        guild=guild,
        content="Need help with deployment",
        jump_url="https://discord.com/channels/1/2/3",
    )

    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)
    monkeypatch.setattr(main, "IT_OPERATIONS_SUPPORT_ROLE_ID", 777)

    await main.notify_support_team_for_message(cast(discord.Message, message))

    support_member_one.send.assert_awaited_once()
    support_member_two.send.assert_awaited_once()
    author.send.assert_not_awaited()
    bot_member.send.assert_not_awaited()

    sent_call = support_member_one.send.await_args
    assert sent_call is not None
    sent_text = sent_call.args[0]
    assert "New message in support sessions." in sent_text
    assert "Need help with deployment" in sent_text
    assert "https://discord.com/channels/1/2/3" in sent_text


@pytest.mark.asyncio
async def test_support_session_dm_skips_other_channels(monkeypatch):
    author = FakeMember(10, "@author")
    support_member = FakeMember(11, "@support1")

    support_role = FakeRole(777, "@ITSupport", members=[support_member])
    guild = FakeGuild([support_role])
    channel = SimpleNamespace(id=999, name="general")
    message = SimpleNamespace(
        author=author,
        channel=channel,
        guild=guild,
        content="Hello",
    )

    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)
    monkeypatch.setattr(main, "IT_OPERATIONS_SUPPORT_ROLE_ID", 777)

    await main.notify_support_team_for_message(cast(discord.Message, message))

    support_member.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_support_session_thread_message_uses_parent_channel(monkeypatch):
    author = FakeMember(10, "@author")
    support_member = FakeMember(11, "@support1")

    support_role = FakeRole(777, "@ITSupport", members=[support_member])
    guild = FakeGuild([support_role])
    thread_channel = SimpleNamespace(id=12345, parent_id=888, name="ticket-thread")
    message = SimpleNamespace(
        author=author,
        channel=thread_channel,
        guild=guild,
        content="Need support in thread",
        jump_url="https://discord.com/channels/1/2/3",
    )

    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)
    monkeypatch.setattr(main, "IT_OPERATIONS_SUPPORT_ROLE_ID", 777)

    await main.notify_support_team_for_message(cast(discord.Message, message))

    support_member.send.assert_awaited_once()



@pytest.mark.asyncio
async def test_ipca_signed_requests_roles(monkeypatch):
    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=10, mention="@test-user"),
        channel=SimpleNamespace(id=888),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", 999)
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 456)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 123)
    monkeypatch.setattr(main, "ApprovalView", FakeApprovalView)
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))

    await main.handle_ipca_signed(cast(discord.Interaction, interaction), "SomeUser")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    channel.send.assert_awaited_once()
    send_kwargs = channel.send.await_args.kwargs
    assert isinstance(send_kwargs["view"], FakeApprovalView)
    assert send_kwargs["view"].dev_team_role_id == 456
    assert send_kwargs["view"].available_role_id == 123
    interaction.edit_original_response.assert_awaited_once_with(
        content="Your approval request was sent to staff for review.",
    )


@pytest.mark.asyncio
async def test_ipca_signed_assigns_dev_and_available_roles(monkeypatch):
    role_one = FakeRole(456, "<@&456>")
    role_two = FakeRole(123, "<@&123>")
    guild = SimpleNamespace(get_role=lambda role_id: role_one if role_id == 456 else role_two if role_id == 123 else None)
    member = SimpleNamespace(
        id=10,
        mention="@test-user",
        guild=guild,
        add_roles=AsyncMock(),
    )
    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        user=member,
        channel=SimpleNamespace(id=888),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", 999)
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 456)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 123)
    monkeypatch.setattr(main, "ApprovalView", FakeApprovalView)
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))

    await main.handle_ipca_signed(cast(discord.Interaction, interaction), "SomeUser")

    member.add_roles.assert_awaited_once()
    args, kwargs = member.add_roles.await_args
    assert len(args) == 2
    assert args[0].id == 456
    assert args[1].id == 123
    assert kwargs["reason"] == "IPCA signed"


@pytest.mark.asyncio
async def test_handle_offboard_user_removes_roles_and_github_memberships(monkeypatch):
    dev_role = FakeRole(456, "<@&456>")
    available_role = FakeRole(123, "<@&123>")
    guild = SimpleNamespace(get_role=lambda role_id: dev_role if role_id == 456 else available_role if role_id == 123 else None)
    member = SimpleNamespace(
        id=10,
        mention="@test-user",
        guild=guild,
        remove_roles=AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, mention="@staff"),
        channel=SimpleNamespace(id=888),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 456)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 123)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "token")
    monkeypatch.setattr(main, "GITHUB_SUPPORT_TEAM_SLUG", "support-team")
    monkeypatch.setattr(main, "GITHUB_IT_TEAM_SLUG", "it-management-team")
    org_mock = Mock(return_value={"ok": True, "message": "Removed from org"})
    team_mock = Mock(return_value={"ok": True, "message": "Removed from team"})
    monkeypatch.setattr(main, "remove_user_from_org", org_mock)
    monkeypatch.setattr(main, "remove_user_from_team", team_mock)

    await main.handle_offboard_user(cast(discord.Interaction, interaction), member, "SomeUser", team="support")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    member.remove_roles.assert_awaited_once()
    org_mock.assert_called_once_with(username="someuser", github_org="Team-Deepiri", github_pat="token")
    team_mock.assert_called_once_with(
        username="someuser",
        github_org="Team-Deepiri",
        github_pat="token",
        team_slug="support-team",
    )
    interaction.edit_original_response.assert_awaited_once_with(content="Offboarding completed for @test-user.")


@pytest.mark.asyncio
async def test_offboard_user_command_uses_handler(monkeypatch):
    called = {}

    async def fake_handler(interaction, member, github_username, *, team=None):
        called["args"] = (interaction, member, github_username, team)

    monkeypatch.setattr(main, "handle_offboard_user", fake_handler)

    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        user=SimpleNamespace(id=1, mention="@staff"),
    )

    member = SimpleNamespace(id=10, mention="@test-user")
    await main.offboard_user.callback(
        cast(discord.Interaction, interaction),
        member,
        "SomeUser",
        team="support",
    )

    assert called["args"] == (interaction, member, "SomeUser", "support")
