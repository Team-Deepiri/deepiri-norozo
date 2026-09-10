"""Plaky invite pipeline: support-ticket 'add to plaky' intent parsing, the
ordered email-resolution chain (cloud DB -> GitHub -> Plaky roster -- see
plaky_invite's module docstring for why there's no local-JSON step any
more), in-thread email asks, and routing invites through the headless
bridge."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import main
import plaky_invite


@pytest.fixture(autouse=True)
def _cloud_identity_stub(monkeypatch):
    """Default no-op cloud stubs so tests that don't care about identity
    resolution/persistence don't make real HTTP calls -- individual tests
    override these with their own mocks/assertions as needed."""
    monkeypatch.setattr(plaky_invite, "load_member_profile", AsyncMock(return_value={"email": None, "real_name": None, "github_username": None}))
    monkeypatch.setattr(plaky_invite, "save_member_identity", AsyncMock(return_value=True))


def _member(discord_id: int = 42, name: str = "jane"):
    m = Mock(spec=__import__("discord").Member)
    m.id = discord_id
    m.display_name = name
    m.global_name = name
    m.name = name
    m.bot = False
    m.get_role.return_value = None
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    return m


def _ticket_channel(thread_id: int, parent_id: int = 100, history_messages=None):
    async def _history(limit=None):
        for msg in (history_messages or []):
            yield msg

    channel = SimpleNamespace(
        id=thread_id,
        parent_id=parent_id,
        name="ticket",
        get_thread=lambda mid: None,
        fetch_message=AsyncMock(return_value=SimpleNamespace(thread=None)),
        send=AsyncMock(),
        history=_history,
    )
    return channel


def _ticket_message(member, channel, content: str):
    return SimpleNamespace(
        id=999,
        guild=SimpleNamespace(),
        channel=channel,
        thread=None,
        content=content,
        author=member,
        mentions=[],
    )


@pytest.mark.parametrize(
    "text",
    [
        "I want to add someone to the Plaky",
        "i need to be added to plaky",
        "i needed added to plaky",
        "add me to the plaky please",
        "please invite me to plaky",
        "can you add claire to plaky?",
        "I signed the IPCA, please add me to plaky",
        "I need an invite to plaky",
        "invite me to plaky joeblacky@deepiri.com",
    ],
)
def test_is_plaky_add_intent_matches_request_forms(text):
    assert main._is_plaky_add_intent(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "remove me from plaky",
        "i need to be removed from plaky",
        "offboard user x from the plaky",
        "the plaky invite worked",
        "plaky invite was sent already",
        "plaky status all good",
        "having an issue with my plaky invite",
        "can someone help with my plaky invite",
    ],
)
def test_is_plaky_add_intent_rejects_removal_and_report_noise(text):
    assert main._is_plaky_add_intent(text) is False


@pytest.mark.asyncio
async def test_remember_user_data_never_overwrites_with_none(monkeypatch):
    from plaky_invite import remember_user_data

    monkeypatch.setattr(plaky_invite, "load_member_profile", AsyncMock(return_value={"email": None, "real_name": None, "github_username": None}))
    save_identity = AsyncMock(return_value=True)
    monkeypatch.setattr(plaky_invite, "save_member_identity", save_identity)

    await remember_user_data(1, email=None, github_username="jane")

    save_identity.assert_awaited_once()
    assert save_identity.await_args.kwargs["email"] is None
    assert save_identity.await_args.kwargs["github_username"] == "jane"


@pytest.mark.asyncio
async def test_call_plaky_bridge_invite_contract(monkeypatch):
    sent = {}

    class FakeResponse:
        status_code = 200
        content = b'{"success": true, "via": "cake"}'

        def json(self):
            return {"success": True, "via": "cake"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            sent["url"] = url
            sent["json"] = json
            sent["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(plaky_invite, "PLAKY_BRIDGE_URL", "http://bridge:5009")
    monkeypatch.setattr(plaky_invite, "INTERNAL_SERVICE_SECRET", "secret-123")
    monkeypatch.setattr(plaky_invite.httpx, "AsyncClient", FakeClient)

    result = await plaky_invite.call_plaky_bridge_invite("jane@deepiri.com")

    assert result["success"] is True
    assert result["via"] == "cake"
    assert sent["url"] == "http://bridge:5009/plaky/invite"
    assert sent["json"] == {"email": "jane@deepiri.com", "role": "MEMBER"}
    assert sent["headers"]["X-Internal-Secret"] == "secret-123"


@pytest.mark.asyncio
async def test_invite_uses_explicit_email_and_persists(monkeypatch):
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    bridge_invite = AsyncMock(return_value={"success": True, "via": "cake"})
    monkeypatch.setattr(main, "call_plaky_bridge_invite", bridge_invite)
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(main, "resolve_member_email", resolve)

    status, email = await main._invite_member_to_plaky(
        discord_id=42,
        discord_username="jane",
        email="jane@deepiri.com",
    )

    assert status == "ok"
    assert email == "jane@deepiri.com"
    bridge_invite.assert_awaited_once_with("jane@deepiri.com", role="MEMBER")
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_flags_when_email_reused_from_file_not_explicitly_given(monkeypatch):
    """Real incident: a repeat 'invite me to plaky' with no email typed
    silently reused whatever was already on file, with the response looking
    identical to a genuinely fresh invite -- no way to tell it happened, and
    no chance to correct a stale/wrong address. The status must distinguish
    'explicitly given this request' from 'resolved from file'."""
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    bridge_invite = AsyncMock(return_value={"success": True, "via": "cake"})
    monkeypatch.setattr(main, "call_plaky_bridge_invite", bridge_invite)
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value="joeblacky@deepiri.com"))

    status, email = await main._invite_member_to_plaky(discord_id=42, discord_username="jane")

    assert status == "ok_from_file"
    assert email == "joeblacky@deepiri.com"
    text = main._plaky_invite_status_text(status, email, "")
    assert "joeblacky@deepiri.com" in text
    assert "already on file" in text.lower()


@pytest.mark.asyncio
async def test_invite_already_in_workspace_flags_reused_email_too(monkeypatch):
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    monkeypatch.setattr(main, "call_plaky_bridge_invite", AsyncMock(return_value={"success": False, "already": True}))
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value="joeblacky@deepiri.com"))

    status, email = await main._invite_member_to_plaky(discord_id=42, discord_username="jane")

    assert status == "already_from_file"
    text = main._plaky_invite_status_text(status, email, "")
    assert "already on file" in text.lower()


@pytest.mark.asyncio
async def test_invite_explicit_email_persists_even_when_bridge_says_already(monkeypatch):
    """Real incident, end to end: Postgres has the stale "joeblacky@deepiri.com".
    The requester explicitly corrects it to "joeblack@deepiri.com" -- even
    though the Plaky bridge reports that address as "already" a member too,
    the CORRECTION ITSELF (what's saved to Postgres for next time) must
    still go through, since the requester explicitly, deliberately gave
    this exact address."""
    monkeypatch.setattr(plaky_invite, "load_member_profile", AsyncMock(return_value={"email": "joeblacky@deepiri.com", "real_name": None, "github_username": None}))
    save_identity = AsyncMock(return_value=True)
    monkeypatch.setattr(plaky_invite, "save_member_identity", save_identity)
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    monkeypatch.setattr(main, "call_plaky_bridge_invite", AsyncMock(return_value={"success": False, "already": True}))

    status, email = await main._invite_member_to_plaky(
        discord_id=42, discord_username="jane", email="joeblack@deepiri.com",
    )

    assert status == "already"  # explicitly given, so NOT the "_from_file" variant
    assert email == "joeblack@deepiri.com"
    save_identity.assert_awaited_once()
    assert save_identity.await_args.kwargs["email"] == "joeblack@deepiri.com"


@pytest.mark.asyncio
async def test_invite_asks_when_email_unresolved(monkeypatch):
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    monkeypatch.setattr(main, "call_plaky_bridge_invite", AsyncMock())
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value=None))

    status, email = await main._invite_member_to_plaky(discord_id=42, discord_username="jane")

    assert status == "asked"
    assert email is None


@pytest.mark.asyncio
async def test_add_request_self_asks_for_email_in_thread(monkeypatch):
    member = _member()
    channel = _ticket_channel(thread_id=555)
    message = _ticket_message(member, channel, "i need to be added to plaky")
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)
    monkeypatch.setattr(main, "PLAKY_API_KEY", None)
    monkeypatch.setattr(main, "call_plaky_bridge_invite", AsyncMock())
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value=None))

    handled = await main._maybe_handle_plaky_add_request(message)

    assert handled is True
    assert "email" in "".join(str(c.args) for c in channel.send.call_args_list).lower()

    assert main.PENDING_PLAKY_EMAIL_THREADS.get(555, {}).get("discord_id") == 42


@pytest.mark.asyncio
async def test_bare_email_reply_works_even_after_a_restart_wiped_pending_state(monkeypatch):
    """Real incident: a requester replied with their corrected email hours
    after the original ask; the bot process had recycled (a deploy) in
    between, wiping PENDING_PLAKY_EMAIL_THREADS clean, and the bare-email
    reply was silently dropped. Scoped by the thread's own message history
    (a prior Norozo message mentioning "plaky") instead of in-memory state
    that a restart can't survive -- no pending entry needed here at all."""
    member = _member()
    prior_bot_message = SimpleNamespace(author=SimpleNamespace(bot=True), content="For the Plaky board invite, your email joeblacky@deepiri.com is already in the workspace...")
    channel = _ticket_channel(thread_id=555, history_messages=[prior_bot_message])
    message = _ticket_message(member, channel, "joeblack@deepiri.com")
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)
    monkeypatch.setattr(main, "PLAKY_API_KEY", None)
    bridge_invite = AsyncMock(return_value={"success": True, "via": "cake"})
    monkeypatch.setattr(main, "call_plaky_bridge_invite", bridge_invite)
    # No pending-ask entry registered anywhere -- simulating the post-restart state.

    handled = await main._maybe_handle_plaky_add_request(message)

    assert handled is True
    bridge_invite.assert_awaited_once_with("joeblack@deepiri.com", role="MEMBER")


@pytest.mark.asyncio
async def test_bare_email_in_thread_without_a_prior_plaky_ask_is_ignored(monkeypatch):
    """The other half of the fix: a bare email typed into a support-ticket
    thread for a totally unrelated reason must NOT be treated as a Plaky
    correction just because it's the only thing in the message -- only a
    thread that already had a Norozo Plaky ask qualifies."""
    member = _member()
    unrelated_bot_message = SimpleNamespace(author=SimpleNamespace(bot=True), content="We gave you access to the rest of the Discord.")
    channel = _ticket_channel(thread_id=556, history_messages=[unrelated_bot_message])
    message = _ticket_message(member, channel, "someone@example.com")
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)
    bridge_invite = AsyncMock()
    monkeypatch.setattr(main, "call_plaky_bridge_invite", bridge_invite)

    handled = await main._maybe_handle_plaky_add_request(message)

    assert handled is False
    bridge_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_from_file_result_registers_a_correction_listener(monkeypatch):
    """Real incident: "I need invited to plaky" resolved silently from a
    stored email ("already_from_file") and told the requester to "reply with
    a different one if that's wrong" -- but nothing was actually listening
    for that reply, so a genuine correction ("joeblack@deepiri.com") silently
    did nothing, with no confirmation either way. A pending-ask entry must be
    registered for the "_from_file" statuses too, not just a fresh "asked"."""
    discord = __import__("discord")
    member = Mock(spec=discord.Member)
    member.id = 42
    member.display_name = "jane"
    member.global_name = "jane"
    member.name = "jane"
    member.bot = False

    thread = Mock(spec=discord.Thread)
    thread.id = 555
    thread.guild = SimpleNamespace()
    thread.owner_id = 42
    thread.parent_id = 100
    thread.send = AsyncMock()

    first_message = SimpleNamespace(
        id=997, guild=SimpleNamespace(), channel=thread, thread=None,
        content="I need invited to plaky", author=member, mentions=[],
    )
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)
    monkeypatch.setattr(main, "PLAKY_API_KEY", None)
    monkeypatch.setattr(main, "_resolve_reply_channel", AsyncMock(return_value=thread))
    monkeypatch.setattr(main, "call_plaky_bridge_invite", AsyncMock(return_value={"success": False, "already": True}))
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value="joeblacky@deepiri.com"))

    handled = await main._maybe_handle_plaky_add_request(first_message)

    assert handled is True
    already_text = thread.send.await_args.args[0]
    assert "already on file" in already_text.lower()
    assert main.PENDING_PLAKY_EMAIL_THREADS.get(555, {}).get("discord_id") == 42

    # Requester replies with the CORRECT address in the same thread.
    bridge_invite = AsyncMock(return_value={"success": True, "via": "cake"})
    monkeypatch.setattr(main, "call_plaky_bridge_invite", bridge_invite)
    correction_message = SimpleNamespace(
        id=998, guild=SimpleNamespace(), channel=thread, thread=None,
        content="joeblack@deepiri.com", author=member, mentions=[],
    )

    handled_correction = await main._maybe_handle_plaky_pending_email_reply(correction_message)

    assert handled_correction is True
    bridge_invite.assert_awaited_once_with("joeblack@deepiri.com", role="MEMBER")
    assert 555 not in main.PENDING_PLAKY_EMAIL_THREADS
    confirmation_text = thread.send.await_args.args[0]
    assert "joeblack@deepiri.com" in confirmation_text


@pytest.mark.asyncio
async def test_pending_email_reply_completes_invite(monkeypatch):
    discord = __import__("discord")
    member = Mock(spec=discord.Member)
    member.id = 42
    member.display_name = "jane"
    member.global_name = "jane"
    member.name = "jane"
    member.bot = False

    thread = Mock(spec=discord.Thread)
    thread.id = 555
    thread.name = "ticket-555"
    thread.guild = SimpleNamespace()
    thread.owner_id = 42
    thread.parent_id = 100
    thread.send = AsyncMock()

    message = SimpleNamespace(
        id=998,
        guild=SimpleNamespace(),
        channel=thread,
        thread=None,
        content="jane@deepiri.com",
        author=member,
        mentions=[],
    )
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    bridge_invite = AsyncMock(return_value={"success": True, "via": "cake"})
    monkeypatch.setattr(main, "call_plaky_bridge_invite", bridge_invite)
    await main._pending_plaky_ask_set(555, {"discord_id": 42, "github_username": None, "role": "MEMBER", "requested_at": __import__("time").time(), "sender_id": 42})
    monkeypatch.setattr(main, "_resolve_reply_channel", AsyncMock(return_value=thread))

    handled = await main._maybe_handle_plaky_pending_email_reply(message)

    assert handled is True
    assert 555 not in main.PENDING_PLAKY_EMAIL_THREADS
    bridge_invite.assert_awaited_once_with("jane@deepiri.com", role="MEMBER")
    thread.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_request_mention_target_invites_with_cloud_email(monkeypatch):
    target = _member(discord_id=77, name="claire")
    sender = _member(discord_id=42, name="jane")
    channel = _ticket_channel(thread_id=601)
    message = _ticket_message(sender, channel, "add <@77> to the plaky please")
    message.mentions = [target]
    message.guild = SimpleNamespace(members=[target, sender])
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    bridge_invite = AsyncMock(return_value={"success": True, "via": "cake"})
    monkeypatch.setattr(main, "call_plaky_bridge_invite", bridge_invite)
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value="claire@deepiri.com"))

    handled = await main._maybe_handle_plaky_add_request(message)

    assert handled is True
    bridge_invite.assert_awaited_once_with("claire@deepiri.com", role="MEMBER")
    assert "✅" in "".join(str(c.args) for c in channel.send.call_args_list).lower() or "✅" in "".join(str(c.args) for c in channel.send.call_args_list)


@pytest.mark.asyncio
async def test_ipca_sign_asks_for_email_in_the_same_ticket_thread(monkeypatch):
    """IPCA sign with no email resolvable: the ask happens right in the same
    support-ticket thread as one combined message with the access-grant
    confirmation -- no DM, and no second separate message."""
    member = _member(discord_id=42, name="jane")
    channel = _ticket_channel(thread_id=710)
    message = _ticket_message(member, channel, "I signed the IPCA")
    message.guild = SimpleNamespace(
        get_role=lambda rid: _member() if rid in (main.DEV_TEAM_ROLE_ID, main.AVAILABLE_ROLE_ID) else None
    )
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 10)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 20)
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)
    monkeypatch.setattr(main, "PLAKY_API_KEY", None)
    monkeypatch.setattr(main, "call_plaky_bridge_invite", AsyncMock())
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_resolve_reply_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(main, "_close_ticket_thread", AsyncMock())
    member.send = AsyncMock()

    ipca_assigned = await main._maybe_auto_assign_ipca_roles(message)

    assert ipca_assigned is True
    member.send.assert_not_awaited()  # never DMs -- the ask lives in this thread
    assert main.PENDING_PLAKY_EMAIL_THREADS.get(710, {}).get("discord_id") == 42
    assert main.PENDING_PLAKY_EMAIL_THREADS.get(710, {}).get("via") == "thread"
    # One combined message: access confirmation + the Plaky email ask.
    channel.send.assert_awaited_once()
    combined = channel.send.await_args.args[0]
    assert "we gave you access to the rest of the discord" in combined.lower()
    assert "email" in combined.lower()
    assert "plaky" in combined.lower()


@pytest.mark.asyncio
async def test_ipca_sign_restart_catchup_sweep_also_triggers_plaky_invite(monkeypatch):
    """Real gap this closes: the restart catch-up sweep
    (_sweep_open_support_threads_for_ipca et al) previously only granted
    roles for IPCA signs that happened while the bot was down -- it never
    triggered the Plaky invite/ask at all, because that used to be a
    separate call only made from the live on_message handler. Now that it's
    folded into _maybe_auto_assign_ipca_roles itself, the sweep path (which
    also just calls that same function) gets it too."""
    member = _member(discord_id=43, name="alex")
    channel = _ticket_channel(thread_id=712)
    message = _ticket_message(member, channel, "I signed the IPCA")
    message.guild = SimpleNamespace(
        get_role=lambda rid: _member() if rid in (main.DEV_TEAM_ROLE_ID, main.AVAILABLE_ROLE_ID) else None
    )
    monkeypatch.setattr(main, "DEV_TEAM_ROLE_ID", 10)
    monkeypatch.setattr(main, "AVAILABLE_ROLE_ID", 20)
    monkeypatch.setattr(main, "SUPPORT_SESSIONS_CHANNEL_ID", 100)
    monkeypatch.setattr(main, "GITHUB_PROFILES_CHANNEL_ID", None)
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    bridge_invite = AsyncMock(return_value={"success": True, "via": "cake"})
    monkeypatch.setattr(main, "call_plaky_bridge_invite", bridge_invite)
    monkeypatch.setattr(main, "resolve_member_email", AsyncMock(return_value="alex@deepiri.com"))
    monkeypatch.setattr(main, "_resolve_reply_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(main, "_close_ticket_thread", AsyncMock())

    # This IS the sweep path's own call shape -- no separate plaky trigger.
    assigned = await main._maybe_auto_assign_ipca_roles(message)

    assert assigned is True
    bridge_invite.assert_awaited_once_with("alex@deepiri.com", role="MEMBER")
    combined = channel.send.await_args.args[0]
    assert "we gave you access to the rest of the discord" in combined.lower()
    assert "alex@deepiri.com" in combined


@pytest.mark.asyncio
async def test_dm_pending_email_reply_completes_invite(monkeypatch):
    discord = __import__("discord")
    member = _member(discord_id=42, name="jane")
    dm_channel = Mock(spec=discord.DMChannel)
    dm_channel.id = 42
    dm_channel.send = AsyncMock()
    message = SimpleNamespace(
        id=998,
        guild=None,
        channel=dm_channel,
        thread=None,
        content="jane@deepiri.com",
        author=member,
        mentions=[],
    )
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    bridge_invite = AsyncMock(return_value={"success": True, "via": "cake"})
    monkeypatch.setattr(main, "call_plaky_bridge_invite", bridge_invite)
    await main._pending_plaky_ask_set(
        42,
        {"discord_id": 42, "github_username": None, "role": "MEMBER", "requested_at": __import__("time").time(), "sender_id": 42, "via": "dm"},
    )

    handled = await main._maybe_handle_plaky_pending_email_reply(message)

    assert handled is True
    assert 42 not in main.PENDING_PLAKY_EMAIL_THREADS
    bridge_invite.assert_awaited_once_with("jane@deepiri.com", role="MEMBER")
    dm_channel.send.assert_awaited_once()
    dm_reply = dm_channel.send.await_args.args[0]
    assert "invite" in dm_reply.lower()
    assert "kick" not in dm_reply.lower()


@pytest.mark.asyncio
async def test_dm_pending_email_reply_ignored_for_other_member(monkeypatch):
    discord = __import__("discord")
    other = _member(discord_id=77, name="claire")
    dm_channel = Mock(spec=discord.DMChannel)
    dm_channel.id = 77
    dm_channel.send = AsyncMock()
    message = SimpleNamespace(
        id=999,
        guild=None,
        channel=dm_channel,
        thread=None,
        content="claire@deepiri.com",
        author=other,
        mentions=[],
    )
    monkeypatch.setattr(main, "PLAKY_API_KEY", "pk")
    await main._pending_plaky_ask_set(
        42,
        {"discord_id": 42, "github_username": None, "role": "MEMBER", "requested_at": __import__("time").time(), "sender_id": 42, "via": "dm"},
    )

    handled = await main._maybe_handle_plaky_pending_email_reply(message)

    assert handled is False
    assert 42 in main.PENDING_PLAKY_EMAIL_THREADS

@pytest.mark.asyncio
async def test_resolve_member_email_uses_cloud_as_the_only_durable_source(monkeypatch):
    """Real incident (root cause of the whole "correction never sticks" saga):
    a local user_data.json step used to be checked before -- or as a fallback
    after -- cloud, and could shadow a more recent, correct value in
    Postgres. That step is gone entirely now (see plaky_invite's module
    docstring): cloud is the only source resolve_member_email trusts."""
    monkeypatch.setattr(plaky_invite, "load_member_profile", AsyncMock(return_value={"email": "correct-cloud@deepiri.com", "real_name": None, "github_username": None}))

    email = await plaky_invite.resolve_member_email(1)

    assert email == "correct-cloud@deepiri.com"


@pytest.mark.asyncio
async def test_remember_user_data_writes_cloud_with_overwrite_semantics(monkeypatch):
    """overwrite=False must not clobber a value Postgres already has (one
    extra GET first); overwrite=True must always send the new value."""
    monkeypatch.setattr(plaky_invite, "load_member_profile", AsyncMock(return_value={"email": "existing@deepiri.com", "real_name": None, "github_username": None}))
    save_identity = AsyncMock(return_value=True)
    monkeypatch.setattr(plaky_invite, "save_member_identity", save_identity)

    await plaky_invite.remember_user_data(1, email="new@deepiri.com", overwrite=False)
    save_identity.assert_not_awaited()  # Postgres already has an email -- don't touch it

    await plaky_invite.remember_user_data(1, email="new@deepiri.com", overwrite=True)
    save_identity.assert_awaited_once()
    assert save_identity.await_args.kwargs["email"] == "new@deepiri.com"
