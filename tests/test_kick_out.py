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
