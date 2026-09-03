"""POST /debug/test-email -- exercises the real identity-resolution + email
path (same as kick-out/retirement) for one member WITHOUT kicking them from
Discord or removing them from the GitHub org. Used to verify SMTP delivery
is actually working without touching membership."""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main


def _signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _webhook_request(body: bytes, secret: str):
    return SimpleNamespace(read=AsyncMock(return_value=body), headers={"X-Norozo-Signature": _signature(body, secret)})


@pytest.mark.asyncio
async def test_rejects_missing_signature(monkeypatch):
    monkeypatch.setattr(main, "ANNOUNCEMENTS_INBOUND_SECRET", "shh")
    request = SimpleNamespace(read=AsyncMock(return_value=b'{"discord_id": "1"}'), headers={})

    response = await main.test_email_debug_handler(request)

    assert response.status == 401


@pytest.mark.asyncio
async def test_requires_discord_id(monkeypatch):
    monkeypatch.setattr(main, "ANNOUNCEMENTS_INBOUND_SECRET", "shh")
    response = await main.test_email_debug_handler(_webhook_request(b"{}", "shh"))
    assert response.status == 400


@pytest.mark.asyncio
async def test_404_when_member_not_in_guild(monkeypatch):
    monkeypatch.setattr(main, "ANNOUNCEMENTS_INBOUND_SECRET", "shh")
    guild = SimpleNamespace(get_member=lambda uid: None, fetch_member=AsyncMock(side_effect=main.discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "not found")))
    monkeypatch.setattr(main, "_get_primary_guild", AsyncMock(return_value=guild))

    response = await main.test_email_debug_handler(_webhook_request(b'{"discord_id": "123"}', "shh"))

    assert response.status == 404


@pytest.mark.asyncio
async def test_sends_test_email_without_kicking_or_removing(monkeypatch):
    """The core guarantee: this must never call kick() or remove_user_from_org --
    only the identity+email path."""
    monkeypatch.setattr(main, "ANNOUNCEMENTS_INBOUND_SECRET", "shh")
    monkeypatch.setattr(main, "GITHUB_ORG", None)  # skip org-membership re-verification
    monkeypatch.setattr(main, "GITHUB_PAT", None)

    member = SimpleNamespace(id=709930474252009473, display_name="Joe Black", kick=AsyncMock(), send=AsyncMock())
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 709930474252009473 else None)
    monkeypatch.setattr(main, "_get_primary_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(main, "_get_github_username_for_member", lambda m: "jrb00013")
    remove_mock = AsyncMock()
    monkeypatch.setattr(main, "remove_user_from_org", remove_mock)
    notice_mock = AsyncMock(return_value="emailed to jrb00013@example.com")
    monkeypatch.setattr(main, "_send_offboarding_notice", notice_mock)

    response = await main.test_email_debug_handler(_webhook_request(b'{"discord_id": "709930474252009473"}', "shh"))

    assert response.status == 200
    body = json.loads(response.body)
    assert body["ok"] is True
    assert body["github_username"] == "jrb00013"
    assert body["outcome"] == "emailed to jrb00013@example.com"
    member.kick.assert_not_awaited()
    remove_mock.assert_not_called()
    notice_mock.assert_awaited_once()
    call = notice_mock.await_args
    assert call.args[0] is member
    assert call.args[1] == "jrb00013"
    assert call.kwargs["subject"] == "Norozo Test Email"
