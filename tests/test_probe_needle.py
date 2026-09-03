"""Staff-only "probe needle" diagnostic: dumps every bot-authored message in a
ticket thread (content, embeds, component custom_ids, reactions) via DM, so
Needle's real ticket-closing mechanism can be found from observed data instead
of guessed at again -- both prior guesses ("/close" as text, raw thread
archiving) caused real production problems."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

import main


def _button(label="Close Ticket", custom_id="needle_close_123", style="danger"):
    b = Mock()
    b.label = label
    b.custom_id = custom_id
    b.style = style
    b.url = None
    return b


def _action_row(children):
    row = Mock()
    row.children = children
    return row


def _bot_message(id_=1, author_id=999, content="", embeds=None, components=None, reactions=None):
    author = SimpleNamespace(bot=True, id=author_id, __str__=lambda self: "Needle#0001")
    return SimpleNamespace(
        id=id_,
        author=author,
        content=content,
        embeds=embeds or [],
        components=components or [],
        reactions=reactions or [],
    )


class _AsyncHistory:
    def __init__(self, messages):
        self._messages = messages

    def __aiter__(self):
        self._iter = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _thread(messages, id_=555, name="ticket-yousif"):
    thread = Mock(spec=discord.Thread)
    thread.id = id_
    thread.name = name
    thread.history = Mock(return_value=_AsyncHistory(messages))
    thread.send = AsyncMock()
    return thread


def _staff_author(id_=1):
    m = Mock(spec=discord.Member)
    m.id = id_
    m.mention = f"<@{id_}>"
    m.send = AsyncMock()
    return m


@pytest.mark.asyncio
async def test_ignores_non_thread_channels(monkeypatch):
    message = SimpleNamespace(guild=SimpleNamespace(), channel=SimpleNamespace(), content="probe needle", author=_staff_author())
    handled = await main._maybe_handle_probe_needle_command(message)
    assert handled is False


@pytest.mark.asyncio
async def test_requires_exact_trigger_phrase(monkeypatch):
    monkeypatch.setattr(main, "_is_staff_or_security_ops", lambda m: True)
    thread = _thread([])
    message = SimpleNamespace(guild=SimpleNamespace(), channel=thread, content="probe needle please", author=_staff_author())
    handled = await main._maybe_handle_probe_needle_command(message)
    assert handled is False


@pytest.mark.asyncio
async def test_non_staff_cannot_probe(monkeypatch):
    monkeypatch.setattr(main, "_is_staff_or_security_ops", lambda m: False)
    thread = _thread([])
    message = SimpleNamespace(guild=SimpleNamespace(), channel=thread, content="probe needle", author=_staff_author())
    handled = await main._maybe_handle_probe_needle_command(message)
    assert handled is False


@pytest.mark.asyncio
async def test_dumps_bot_message_content_embeds_and_component_custom_ids(monkeypatch):
    monkeypatch.setattr(main, "_is_staff_or_security_ops", lambda m: True)
    embed = Mock()
    embed.title = "Ticket Opened"
    embed.description = "A staff member will be with you shortly."
    embed.fields = []
    needle_msg = _bot_message(
        id_=10,
        content="",
        embeds=[embed],
        components=[_action_row([_button(label="Close Ticket", custom_id="needle_close_555")])],
    )
    human_msg = SimpleNamespace(id=11, author=SimpleNamespace(bot=False, id=2), content="I signed the IPCA", embeds=[], components=[], reactions=[])
    thread = _thread([needle_msg, human_msg])
    author = _staff_author()
    message = SimpleNamespace(guild=SimpleNamespace(), channel=thread, content="probe needle", author=author)

    handled = await main._maybe_handle_probe_needle_command(message)

    assert handled is True
    author.send.assert_awaited()
    dumped = "".join(call.args[0] for call in author.send.await_args_list)
    assert "needle_close_555" in dumped
    assert "Close Ticket" in dumped
    assert "Ticket Opened" in dumped
    assert "I signed the IPCA" not in dumped  # human messages aren't dumped
    thread.send.assert_awaited_once()
    assert "Sent you" in thread.send.await_args.args[0]


@pytest.mark.asyncio
async def test_reports_when_no_bot_messages_found(monkeypatch):
    monkeypatch.setattr(main, "_is_staff_or_security_ops", lambda m: True)
    human_msg = SimpleNamespace(id=1, author=SimpleNamespace(bot=False, id=2), content="hi", embeds=[], components=[], reactions=[])
    thread = _thread([human_msg])
    author = _staff_author()
    message = SimpleNamespace(guild=SimpleNamespace(), channel=thread, content="probe needle", author=author)

    handled = await main._maybe_handle_probe_needle_command(message)

    assert handled is True
    dumped = "".join(call.args[0] for call in author.send.await_args_list)
    assert "no bot-authored messages" in dumped


@pytest.mark.asyncio
async def test_falls_back_to_thread_notice_when_dm_blocked(monkeypatch):
    monkeypatch.setattr(main, "_is_staff_or_security_ops", lambda m: True)
    thread = _thread([])
    author = _staff_author()
    author.send = AsyncMock(side_effect=discord.Forbidden(Mock(status=403), "cannot send"))
    message = SimpleNamespace(guild=SimpleNamespace(), channel=thread, content="probe needle", author=author)

    handled = await main._maybe_handle_probe_needle_command(message)

    assert handled is True
    thread.send.assert_awaited_once()
    assert "Couldn't DM" in thread.send.await_args.args[0]
