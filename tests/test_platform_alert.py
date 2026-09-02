"""Inbound /alerts/webhook handler: critical alerts must @ the Security &
Operations Support role directly in the #it-notifications channel post, not
just via individual DMs -- so anyone watching the channel sees it, not only
the people who happen to get a DM."""

import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main


def _signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _webhook_request(body: bytes, secret: str):
    sig = _signature(body, secret)
    return SimpleNamespace(read=AsyncMock(return_value=body), headers={"X-Norozo-Signature": sig})


def _setup(monkeypatch, *, severity: str):
    secret = "alert-secret"
    monkeypatch.setattr(main, "ANNOUNCEMENTS_INBOUND_SECRET", secret)
    monkeypatch.setattr(main, "STAFF_CHANNEL_ID", 12345)
    monkeypatch.setattr(main, "IT_OPERATIONS_SUPPORT_ROLE_ID", 999888777)
    channel = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))
    monkeypatch.setattr(main, "_dm_role_members", AsyncMock(return_value=0))
    body = (
        b'{"title":"Something broke","message":"details here","severity":"' + severity.encode() + b'"}'
    )
    return channel, _webhook_request(body, secret)


@pytest.mark.asyncio
async def test_critical_alert_mentions_role_directly_in_channel_post(monkeypatch):
    channel, request = _setup(monkeypatch, severity="critical")

    response = await main.platform_alert_handler(request)

    assert response.status == 200
    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "<@&999888777>"


@pytest.mark.asyncio
async def test_non_critical_alert_does_not_mention_role(monkeypatch):
    channel, request = _setup(monkeypatch, severity="warning")

    await main.platform_alert_handler(request)

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] is None


@pytest.mark.asyncio
async def test_critical_alert_still_dms_role_members_in_addition_to_mention(monkeypatch):
    channel, request = _setup(monkeypatch, severity="critical")
    dm_mock = AsyncMock(return_value=3)
    monkeypatch.setattr(main, "_dm_role_members", dm_mock)

    response = await main.platform_alert_handler(request)

    dm_mock.assert_awaited_once()
    data = response.body if hasattr(response, "body") else None
    assert data is not None
