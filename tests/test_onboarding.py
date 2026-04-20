from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

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


class FakeGuild:
    def __init__(self, roles):
        self.roles = {role.id: role for role in roles}
        self.id = 999

    def get_role(self, role_id: int):
        return self.roles.get(role_id)


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


@pytest.mark.asyncio
async def test_ipca_signed_acknowledges_before_posting(monkeypatch):
    channel = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(mention="@test-user"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", 999)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 123)
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 456)
    monkeypatch.setattr(main, "ApprovalView", FakeApprovalView)
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))

    await main.handle_ipca_signed(cast(discord.Interaction, interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    channel.send.assert_awaited_once()
    send_kwargs = channel.send.await_args.kwargs
    assert isinstance(send_kwargs["view"], FakeApprovalView)
    assert send_kwargs["view"].available_role_id == 123
    assert send_kwargs["view"].dev_team_role_id == 456
    interaction.edit_original_response.assert_awaited_once_with(
        content="Your approval request was sent to staff for review.",
    )


@pytest.mark.asyncio
async def test_ipca_signed_reports_staff_post_failure(monkeypatch):
    channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("no permission")))
    interaction = SimpleNamespace(
        user=SimpleNamespace(mention="@test-user"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", 999)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 123)
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 456)
    monkeypatch.setattr(main, "ApprovalView", FakeApprovalView)
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))

    await main.handle_ipca_signed(cast(discord.Interaction, interaction))

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