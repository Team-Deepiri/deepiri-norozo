from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import DiscussionsBridgeBot


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
