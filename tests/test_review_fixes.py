import hashlib
import hmac
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import main
import meetings


def _signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _webhook_request(body: bytes, headers=None):
    return SimpleNamespace(read=AsyncMock(return_value=body), headers=headers or {})


def test_webhook_signature_validators_share_supported_formats():
    body = b'{"event":"announcement"}'
    secret = "webhook-secret"
    digest = _signature(body, secret)

    assert main._is_valid_plaky_signature(body, digest, secret)
    assert main._is_valid_plaky_signature(body, f"sha256={digest}", secret)
    assert main._is_valid_announcement_signature(body, digest, secret)
    assert main._is_valid_announcement_signature(body, f"sha256={digest}", secret)
    assert not main._is_valid_announcement_signature(body, "sha256=invalid", secret)


def test_announcement_signature_fails_closed_without_secret():
    assert not main._is_valid_announcement_signature(b"body", "invalid", "")


def test_github_username_map_load_failure_is_logged(monkeypatch, tmp_path, caplog):
    invalid_map = tmp_path / "github-usernames.json"
    invalid_map.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(main, "GITHUB_USERNAME_MAP_PATH", invalid_map)

    with caplog.at_level("ERROR"):
        assert main._load_github_username_map() == {}

    assert "Failed to load GitHub username map" in caplog.text


def test_explicit_github_username_mapping_precedes_name_inference(monkeypatch, tmp_path):
    username_map = tmp_path / "github-usernames.json"
    username_map.write_text('{"42": "ExplicitUser"}', encoding="utf-8")
    monkeypatch.setattr(main, "GITHUB_USERNAME_MAP_PATH", username_map)
    member = SimpleNamespace(id=42, global_name="inferred-user", display_name="inferred-user", name="inferred-user")

    assert main._get_github_username_for_member(member) == "explicituser"


def test_meeting_role_ids_take_precedence_over_role_name_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETINGS_FILE", str(tmp_path / "meetings.json"))
    monkeypatch.setenv("MEETING_AI_ML_ROLE_IDS", "101, 202")
    service = meetings.MeetingReminderService(SimpleNamespace())
    configured_role = SimpleNamespace(id=101, name="Unrelated name", mention="<@&101>")
    guild = SimpleNamespace(get_role=lambda role_id: configured_role if role_id == 101 else None, roles=[])

    assert service._get_mentions_for_meeting("AI/ML", guild) == "<@&101> <@&202>"


def test_meetings_use_canonical_eastern_timezone():
    assert meetings.EST.zone == "America/New_York"


def test_weekly_recurrence_preserves_eastern_time_across_dst(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETINGS_FILE", str(tmp_path / "meetings.json"))
    service = meetings.MeetingReminderService(SimpleNamespace())
    before_dst_ends = datetime(2026, 10, 27, 1, 30, tzinfo=meetings.UTC)

    next_week = service._next_weekly_occurrence("AI/ML", before_dst_ends)

    assert next_week == datetime(2026, 11, 3, 2, 30, tzinfo=meetings.UTC)
    assert next_week.astimezone(meetings.EST).hour == 21


@pytest.mark.asyncio
async def test_announcement_forward_uses_async_client_and_signed_bytes(monkeypatch):
    response = SimpleNamespace(raise_for_status=Mock())
    post = AsyncMock(return_value=response)

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            assert timeout == 10

        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(main, "PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL", "https://platform.example/webhook")
    monkeypatch.setattr(main, "PLATFORM_ANNOUNCEMENTS_SECRET", "bridge-secret")
    monkeypatch.setattr(main, "format_discussion_title", lambda content: "Title")
    monkeypatch.setattr(main, "format_discussion_body", lambda message: "Body")
    message = SimpleNamespace(
        id=123,
        channel=SimpleNamespace(id=456),
        author=SimpleNamespace(id=789, __str__=lambda self: "Author"),
        content="Announcement",
        created_at=datetime.now(timezone.utc),
        jump_url="https://discord.example/message/123",
    )

    await main._forward_announcement_to_platform(message)

    post.assert_awaited_once()
    request = post.await_args
    raw = request.kwargs["content"]
    expected_signature = _signature(raw, "bridge-secret")
    assert request.kwargs["headers"]["X-Norozo-Signature"] == f"sha256={expected_signature}"
    response.raise_for_status.assert_called_once_with()


@pytest.mark.asyncio
async def test_announcement_webhook_rejects_unconfigured_authentication(monkeypatch):
    monkeypatch.setattr(main, "ANNOUNCEMENTS_INBOUND_SECRET", "")

    response = await main.platform_announcement_handler(_webhook_request(b'{}'))

    assert response.status == 503


@pytest.mark.asyncio
async def test_announcement_webhook_rejects_missing_signature(monkeypatch):
    monkeypatch.setattr(main, "ANNOUNCEMENTS_INBOUND_SECRET", "inbound-secret")

    response = await main.platform_announcement_handler(_webhook_request(b'{"title":"Important"}'))

    assert response.status == 401


@pytest.mark.asyncio
async def test_announcement_webhook_deduplicates_retries(monkeypatch, tmp_path):
    body = json.dumps({"event_id": "event-123", "title": "Important", "body": "Details"}).encode("utf-8")
    secret = "inbound-secret"
    headers = {"X-Norozo-Signature": f"sha256={_signature(body, secret)}"}
    channel = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(main, "ANNOUNCEMENTS_INBOUND_SECRET", secret)
    monkeypatch.setattr(main, "ANNOUNCEMENTS_CHANNEL_ID", 123)
    monkeypatch.setattr(main, "ANNOUNCEMENT_DEDUP_PATH", tmp_path / "announcement-events.json")
    monkeypatch.setattr(main, "_channel_from_id", AsyncMock(return_value=channel))

    first_response = await main.platform_announcement_handler(_webhook_request(body, headers))
    retry_response = await main.platform_announcement_handler(_webhook_request(body, headers))

    assert first_response.status == 200
    assert retry_response.status == 200
    assert json.loads(retry_response.body)["duplicate"] is True
    channel.send.assert_awaited_once()
