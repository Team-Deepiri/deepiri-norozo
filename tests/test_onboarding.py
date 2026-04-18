from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import discord
import pytest

import main


class FakeInteraction:
    def __init__(self):
        self.user = SimpleNamespace(mention="@test-user")
        self.response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
        self.edit_original_response = AsyncMock()


class FakeChannel:
    def __init__(self, *, side_effect=None):
        self.send = AsyncMock(side_effect=side_effect)


@pytest.mark.asyncio
async def test_ipca_signed_acknowledges_before_posting(monkeypatch):
    interaction = FakeInteraction()
    channel = FakeChannel()

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", 123)
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 456)
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))

    await main.handle_ipca_signed(cast(discord.Interaction, interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    channel.send.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once_with(
        content="Your approval request was sent to staff."
    )


@pytest.mark.asyncio
async def test_ipca_signed_reports_staff_post_failure(monkeypatch):
    interaction = FakeInteraction()
    channel = FakeChannel(side_effect=RuntimeError("no permission"))

    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", 123)
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 456)
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))

    await main.handle_ipca_signed(cast(discord.Interaction, interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    channel.send.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once_with(
        content="I could not send your approval request to the staff channel."
    )