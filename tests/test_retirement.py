"""Voluntary retirement flow: staff mentioning "retiring" in chat DMs the
named (or ticket-creator-fallback) person a confirmation prompt; only that
person's own confirmation click actually kicks them + removes GitHub access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

import main


def _member(id_=1, display_name="Ricco", is_bot=False):
    m = Mock(spec=discord.Member)
    m.id = id_
    m.display_name = display_name
    m.mention = f"<@{id_}>"
    m.bot = is_bot
    m.send = AsyncMock()
    m.kick = AsyncMock()
    return m


@pytest.mark.asyncio
async def test_no_target_resolvable_asks_for_clarification(monkeypatch):
    monkeypatch.setattr(main, "_is_staff_or_security_ops", lambda member: True)
    author = _member(id_=1)
    channel = SimpleNamespace(id=50, name="general")
    message = SimpleNamespace(guild=SimpleNamespace(), author=author, content="retiring soon", channel=channel, mentions=[])
    reply_channel = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(main, "_resolve_reply_channel", AsyncMock(return_value=reply_channel))

    handled = await main._maybe_handle_retirement_announcement(message)

    assert handled is True
    reply_channel.send.assert_awaited_once()
    assert "Couldn't tell who's retiring" in reply_channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_non_staff_cannot_trigger_retirement(monkeypatch):
    monkeypatch.setattr(main, "_is_staff_or_security_ops", lambda member: False)
    author = _member(id_=1)
    message = SimpleNamespace(guild=SimpleNamespace(), author=author, content="someone is retiring", channel=SimpleNamespace(id=50), mentions=[])

    handled = await main._maybe_handle_retirement_announcement(message)

    assert handled is False


@pytest.mark.asyncio
async def test_mentioned_member_gets_dm_with_confirm_view(monkeypatch):
    monkeypatch.setattr(main, "_is_staff_or_security_ops", lambda member: True)
    author = _member(id_=1, display_name="Staffer")
    target = _member(id_=2, display_name="Departing")
    channel = SimpleNamespace(id=50, name="ticket-thread")
    message = SimpleNamespace(guild=SimpleNamespace(), author=author, content="<@2> is retiring", channel=channel, mentions=[target])
    reply_channel = SimpleNamespace(id=50, send=AsyncMock())
    monkeypatch.setattr(main, "_resolve_reply_channel", AsyncMock(return_value=reply_channel))

    handled = await main._maybe_handle_retirement_announcement(message)

    assert handled is True
    target.send.assert_awaited_once()
    assert "Are you sure you want to retire" in target.send.await_args.args[0]
    assert isinstance(target.send.await_args.kwargs["view"], main.RetirementConfirmView)
    reply_channel.send.assert_awaited_once()
    assert "Sent" in reply_channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_falls_back_to_ticket_thread_creator_when_no_mention(monkeypatch):
    monkeypatch.setattr(main, "_is_staff_or_security_ops", lambda member: True)
    author = _member(id_=1)
    owner = _member(id_=3, display_name="TicketOwner")
    guild = SimpleNamespace(get_member=lambda uid: owner if uid == 3 else None)
    thread = Mock(spec=discord.Thread)
    thread.id = 77
    thread.owner_id = 3
    message = SimpleNamespace(guild=guild, author=author, content="retiring as well", channel=thread, mentions=[])
    reply_channel = SimpleNamespace(id=77, send=AsyncMock())
    monkeypatch.setattr(main, "_resolve_reply_channel", AsyncMock(return_value=reply_channel))

    handled = await main._maybe_handle_retirement_announcement(message)

    assert handled is True
    owner.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_button_rejects_non_target_clicker(monkeypatch):
    view = main.RetirementConfirmView(target_id=2, origin_channel_id=None)
    interaction = SimpleNamespace(user=SimpleNamespace(id=999), response=SimpleNamespace(send_message=AsyncMock()))

    await view.confirm.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert "isn't yours" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_confirm_button_executes_offboarding_for_target(monkeypatch):
    target_member = _member(id_=2, display_name="Departing")
    guild = SimpleNamespace(get_member=lambda uid: target_member if uid == 2 else None)
    monkeypatch.setattr(main, "_get_primary_guild", AsyncMock(return_value=guild))
    execute_mock = AsyncMock(return_value="summary text")
    monkeypatch.setattr(main, "_execute_retirement", execute_mock)

    view = main.RetirementConfirmView(target_id=2, origin_channel_id=None)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        message=None,
    )

    await view.confirm.callback(interaction)

    execute_mock.assert_awaited_once_with(target_member, guild)
    interaction.followup.send.assert_awaited_once()
    assert "Retirement confirmed" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_cancel_button_does_not_execute_offboarding(monkeypatch):
    execute_mock = AsyncMock()
    monkeypatch.setattr(main, "_execute_retirement", execute_mock)

    view = main.RetirementConfirmView(target_id=2, origin_channel_id=None)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=2),
        response=SimpleNamespace(send_message=AsyncMock()),
        message=None,
    )

    await view.cancel.callback(interaction)

    execute_mock.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with("No changes made.")
