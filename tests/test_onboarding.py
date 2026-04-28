from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

import main
import onboarding


class FakeInviteView:
    def __init__(self, github_org: str, github_pat: str):
        self.github_org = github_org
        self.github_pat = github_pat


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
async def test_github_invite_request_acknowledges_before_posting(monkeypatch):
    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=10, mention="@test-user"),
        channel=SimpleNamespace(id=888),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", 999)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "token")
    monkeypatch.setattr(main, "GitHubInviteApprovalView", FakeInviteView)
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)

    await main.handle_github_invite_request(cast(discord.Interaction, interaction), "HTTPS://github.com/SomeUser")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    channel.send.assert_awaited_once()
    send_kwargs = channel.send.await_args.kwargs
    assert isinstance(send_kwargs["view"], FakeInviteView)
    assert send_kwargs["view"].github_org == "Team-Deepiri"
    assert send_kwargs["view"].github_pat == "token"
    assert send_kwargs["embed"].fields[0].value == "someuser"
    interaction.edit_original_response.assert_awaited_once_with(
        content="Your GitHub invite request was sent to staff for review.",
    )


@pytest.mark.asyncio
async def test_github_invite_request_reports_staff_post_failure(monkeypatch):
    channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("no permission")))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=10, mention="@test-user"),
        channel=SimpleNamespace(id=888),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", 999)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "token")
    monkeypatch.setattr(main, "GitHubInviteApprovalView", FakeInviteView)
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 888)

    await main.handle_github_invite_request(cast(discord.Interaction, interaction), "SomeUser")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    channel.send.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once_with(
        content="I could not send your approval request to the staff channel."
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
async def test_github_invite_approval_view_invites_and_disables_button(monkeypatch):
    requester = FakeMember(42, "@requester")
    staff = FakeStaffMember(11, "@staff")
    guild = FakeGuildWithMember(requester)
    message = SimpleNamespace(
        embeds=[
            SimpleNamespace(
                fields=[
                    SimpleNamespace(name="GitHub Username", value="SomeUser"),
                    SimpleNamespace(name="Discord User ID", value="42"),
                ]
            )
        ],
        edit=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild=guild,
        user=staff,
        message=message,
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    seen = {}

    def fake_invite_user(*, username: str, github_org: str, github_pat: str):
        seen["username"] = username
        seen["github_org"] = github_org
        seen["github_pat"] = github_pat
        return {"ok": True, "status": 201, "message": "Invite sent"}

    monkeypatch.setattr(onboarding.discord, "Member", FakeStaffMember)
    monkeypatch.setattr(onboarding, "invite_user", fake_invite_user)

    view = onboarding.GitHubInviteApprovalView(github_org="Team-Deepiri", github_pat="token")

    approve_button = view.children[0]
    await approve_button.callback(cast(discord.Interaction, interaction))

    assert seen["username"] == "SomeUser"
    assert seen["github_org"] == "Team-Deepiri"
    assert seen["github_pat"] == "token"
    interaction.response.send_message.assert_awaited_once()
    message.edit.assert_awaited_once()


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