import hashlib
import hmac
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
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


def test_discord_proxy_kwargs_empty_when_unset(monkeypatch):
    monkeypatch.setattr(main, "DISCORD_PROXY_URL", None)
    assert main._discord_proxy_kwargs() == {}


def test_discord_proxy_kwargs_parses_url_and_auth(monkeypatch):
    monkeypatch.setattr(main, "DISCORD_PROXY_URL", "http://user:pass@1.2.3.4:8888")
    kwargs = main._discord_proxy_kwargs()
    assert kwargs["proxy"] == "http://1.2.3.4:8888"
    assert kwargs["proxy_auth"].login == "user"
    assert kwargs["proxy_auth"].password == "pass"


@pytest.mark.asyncio
async def test_maybe_auto_assign_ipca_roles_assigns_when_missing(monkeypatch):
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 10)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 20)
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)

    dev_role = SimpleNamespace(id=10)
    available_role = SimpleNamespace(id=20)
    guild = SimpleNamespace(get_role=lambda rid: {10: dev_role, 20: available_role}.get(rid))

    author = Mock(spec=discord.Member)
    author.get_role = Mock(return_value=None)
    author.add_roles = AsyncMock()

    thread = Mock(spec=discord.Thread)
    thread.id = 555
    thread.send = AsyncMock()
    thread.edit = AsyncMock()
    channel = SimpleNamespace(id=100, parent_id=None, send=AsyncMock())

    message = Mock(spec=discord.Message)
    message.channel = channel
    message.content = "I signed the IPCA"
    message.author = author
    message.guild = guild
    message.add_reaction = AsyncMock()
    message.thread = thread

    assigned = await main._maybe_auto_assign_ipca_roles(message)

    assert assigned is True
    author.add_roles.assert_awaited_once_with(available_role, dev_role, reason="IPCA signed auto-assign")
    message.add_reaction.assert_awaited_once_with("✅")
    # support-tickets auto-thread: confirmation must land in the companion
    # thread (message.thread), not back in the parent channel via reply().
    assert "We gave you access to the rest of the Discord." in thread.send.call_args_list[0].args[0]
    channel.send.assert_not_awaited()
    # Ticket resolved -> the companion thread gets archived (closed), same as
    # Needle's own "Archive thread" button would do.
    thread.edit.assert_awaited_once_with(archived=True, locked=False, reason="Ticket resolved")


@pytest.mark.asyncio
async def test_maybe_auto_assign_ipca_roles_posts_to_channel_when_no_companion_thread(monkeypatch):
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 10)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 20)
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)

    dev_role = SimpleNamespace(id=10)
    available_role = SimpleNamespace(id=20)
    guild = SimpleNamespace(get_role=lambda rid: {10: dev_role, 20: available_role}.get(rid))

    author = Mock(spec=discord.Member)
    author.get_role = Mock(return_value=None)
    author.add_roles = AsyncMock()

    channel = SimpleNamespace(id=100, parent_id=None, send=AsyncMock())

    message = Mock(spec=discord.Message)
    message.channel = channel
    message.content = "I signed the IPCA"
    message.author = author
    message.guild = guild
    message.add_reaction = AsyncMock()
    message.thread = None

    assigned = await main._maybe_auto_assign_ipca_roles(message)

    assert assigned is True
    channel.send.assert_awaited_once()
    assert "We gave you access to the rest of the Discord." in channel.send.call_args.args[0]


@pytest.mark.asyncio
async def test_maybe_auto_assign_ipca_roles_archives_current_thread_when_message_posted_inside_one(monkeypatch):
    """A follow-up message sent directly inside an already-open ticket thread:
    message.thread is None (only set on the starter message), but
    message.channel IS the thread — that thread should still get archived."""
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 10)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 20)
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)

    dev_role = SimpleNamespace(id=10)
    available_role = SimpleNamespace(id=20)
    guild = SimpleNamespace(get_role=lambda rid: {10: dev_role, 20: available_role}.get(rid))

    author = Mock(spec=discord.Member)
    author.get_role = Mock(return_value=None)
    author.add_roles = AsyncMock()

    thread_channel = Mock(spec=discord.Thread)
    thread_channel.id = 100
    thread_channel.parent_id = 999
    thread_channel.send = AsyncMock()
    thread_channel.edit = AsyncMock()

    message = Mock(spec=discord.Message)
    message.channel = thread_channel
    message.content = "I signed the IPCA"
    message.author = author
    message.guild = guild
    message.add_reaction = AsyncMock()
    message.thread = None

    assigned = await main._maybe_auto_assign_ipca_roles(message)

    assert assigned is True
    thread_channel.edit.assert_awaited_once_with(archived=True, locked=False, reason="Ticket resolved")


@pytest.mark.asyncio
async def test_maybe_auto_assign_ipca_roles_skips_when_already_has_both(monkeypatch):
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 10)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 20)
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)

    dev_role = SimpleNamespace(id=10)
    available_role = SimpleNamespace(id=20)
    guild = SimpleNamespace(get_role=lambda rid: {10: dev_role, 20: available_role}.get(rid))

    author = Mock(spec=discord.Member)
    author.get_role = Mock(side_effect=lambda rid: {10: dev_role, 20: available_role}.get(rid))
    author.add_roles = AsyncMock()

    message = Mock(spec=discord.Message)
    message.channel = SimpleNamespace(id=100, parent_id=None)
    message.content = "IPCA signed"
    message.author = author
    message.guild = guild

    assigned = await main._maybe_auto_assign_ipca_roles(message)

    assert assigned is False
    author.add_roles.assert_not_awaited()


def test_create_and_register_bot_registers_all_global_slash_commands(monkeypatch):
    """Regression test: _create_and_register_bot()'s new_bot previously never got
    github-invite-request/ipca-signed/offboard-user/plaky-request/plaky-status/poll
    registered on its own tree — they were bound to the discarded module-level `bot`
    at import time and silently never synced, leaving only the meetings commands."""
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 1)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 2)
    new_bot = main._create_and_register_bot()
    command_names = {cmd.name for cmd in new_bot.tree.get_commands()}
    assert {
        "github-invite-request",
        "ipca-signed",
        "offboard-user",
        "discord-kick",
        "plaky-request",
        "plaky-status",
        "poll",
        "schedule-meeting",
        "list-meetings",
        "cancel-meeting",
    }.issubset(command_names)


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
        embeds=[],
    )

    await main._forward_announcement_to_platform(message)

    post.assert_awaited_once()
    payload = json.loads(post.await_args.kwargs["content"])
    assert payload["color"] is None


@pytest.mark.asyncio
async def test_announcement_forward_includes_embed_color(monkeypatch):
    """A bot-posted embed (e.g. the 1-month PR-staleness red alert) should carry
    its color through to the platform page, not get flattened to plain text."""
    response = SimpleNamespace(raise_for_status=Mock())
    post = AsyncMock(return_value=response)

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(main, "PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL", "https://platform.example/webhook")
    monkeypatch.setattr(main, "PLATFORM_ANNOUNCEMENTS_SECRET", "bridge-secret")
    monkeypatch.setattr(main, "format_discussion_title", lambda content: "Title")
    monkeypatch.setattr(main, "format_discussion_body", lambda message: "Body")
    embed = SimpleNamespace(color=discord.Color.red())
    message = SimpleNamespace(
        id=124,
        channel=SimpleNamespace(id=456),
        author=SimpleNamespace(id=789, __str__=lambda self: "Author"),
        content="Announcement",
        created_at=datetime.now(timezone.utc),
        jump_url="https://discord.example/message/124",
        embeds=[embed],
    )

    await main._forward_announcement_to_platform(message)

    payload = json.loads(post.await_args.kwargs["content"])
    assert payload["color"] == f"#{discord.Color.red().value:06x}"
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
