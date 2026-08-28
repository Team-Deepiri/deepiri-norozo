import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import DiscussionsBridgeBot, _is_discord_rate_limited, _run_discord_bot


def _make_rate_limit_exception(status: int = 429, ray_id: str | None = None):
    class FakeHTTPException(Exception):
        def __init__(self):
            self.status = status
            self.response = SimpleNamespace(headers={"CF-Ray": ray_id} if ray_id else {})

    return FakeHTTPException()


@pytest.fixture
def bridge_bot(monkeypatch):
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "12345")
    bot = DiscussionsBridgeBot()
    return bot


def _make_message(*, bot_author: bool, channel_id: int, content: str = "Hello world"):
    author = SimpleNamespace(bot=bot_author)
    channel = SimpleNamespace(id=channel_id, name="announcements")
    message = SimpleNamespace(
        author=author,
        channel=channel,
        content=content,
        created_at=datetime.now(timezone.utc),
        id=999,
        attachments=[],
        add_reaction=AsyncMock(),
    )
    return message


@pytest.mark.asyncio
async def test_ignores_bot_messages(bridge_bot):
    message = _make_message(bot_author=True, channel_id=12345)
    create_fn = AsyncMock(return_value="https://example.com")

    await bridge_bot.process_bridge_message(message, create_fn)

    create_fn.assert_not_awaited()
    message.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_wrong_channel(bridge_bot):
    message = _make_message(bot_author=False, channel_id=777)
    create_fn = AsyncMock(return_value="https://example.com")

    await bridge_bot.process_bridge_message(message, create_fn)

    create_fn.assert_not_awaited()
    message.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_creates_discussion_for_valid_message(bridge_bot):
    message = _make_message(bot_author=False, channel_id=12345, content="Launch\nDetails here")
    create_fn = AsyncMock(return_value="https://github.com/org/repo/discussions/7")

    await bridge_bot.process_bridge_message(message, create_fn)

    create_fn.assert_awaited_once()
    message.add_reaction.assert_awaited_once_with("✅")


def test_is_discord_rate_limited_detects_429():
    class FakeRateLimit(Exception):
        status = 429

    assert _is_discord_rate_limited(FakeRateLimit()) is True
    assert _is_discord_rate_limited(Exception()) is False


def test_run_discord_bot_retries_after_rate_limit(monkeypatch):
    calls = {"count": 0}

    class FakeRateLimit(Exception):
        status = 429

    def fake_run(_self, _token):
        calls["count"] += 1
        if calls["count"] == 1:
            raise FakeRateLimit()

    monkeypatch.setattr("bot.time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("bot.DiscussionsBridgeBot.run", fake_run)

    bot = DiscussionsBridgeBot()
    _run_discord_bot(bot, "token", max_attempts=3)

    assert calls["count"] == 2


def test_main_retry_helpers_handle_repeated_429s(monkeypatch):
    import main

    attempts = {"count": 0}
    closes = {"count": 0}
    sleeps = []

    class FakeRateLimit(Exception):
        status = 429
        response = SimpleNamespace(headers={"CF-Ray": "abc123"})

    async def fake_start(_token):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise FakeRateLimit()
        return None

    async def fake_close():
        closes["count"] += 1

    async def fake_sleep(_delay):
        sleeps.append(_delay)

    monkeypatch.setattr(main.bot, "start", fake_start)
    monkeypatch.setattr(main.bot, "close", fake_close)
    monkeypatch.setattr(main.bot, "is_closed", lambda: False)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    async def run():
        await main._connect_discord_with_retry("token")

    import asyncio
    asyncio.run(run())

    assert attempts["count"] == 3
    assert closes["count"] >= 2
    assert sleeps
    assert all(delay > 0 for delay in sleeps)


def test_main_retry_helpers_clean_shutdown_on_cancel(monkeypatch):
    import main

    closed = {"count": 0}

    async def fake_start(_token):
        raise asyncio.CancelledError()

    async def fake_close():
        closed["count"] += 1

    monkeypatch.setattr(main.bot, "start", fake_start)
    monkeypatch.setattr(main.bot, "close", fake_close)
    monkeypatch.setattr(main.bot, "is_closed", lambda: False)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await main._connect_discord_with_retry("token")

    asyncio.run(run())
    assert closed["count"] == 0
