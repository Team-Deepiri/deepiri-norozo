"""Raw Discord mention syntax (<@id>, <@&roleid>, <#channelid>) must be resolved
to readable @name/#channel-name text before content leaves Discord -- anything
consuming it outside a real Discord client (platform.deepiri.com, a GitHub
Discussion body) has no way to resolve a bare snowflake ID on its own."""

from types import SimpleNamespace

from bot import format_discussion_body, resolve_discord_mentions


def _user(id_, name):
    return SimpleNamespace(id=id_, display_name=name, name=name)


def _role(id_, name):
    return SimpleNamespace(id=id_, name=name)


def _channel(id_, name):
    return SimpleNamespace(id=id_, name=name)


def test_user_mention_resolved_to_display_name():
    message = SimpleNamespace(mentions=[_user(123, "Ricco")], role_mentions=[], channel_mentions=[])
    result = resolve_discord_mentions(message, "hey <@123> check this")
    assert result == "hey @Ricco check this"


def test_user_mention_with_nickname_bang_syntax_also_resolved():
    message = SimpleNamespace(mentions=[_user(123, "Ricco")], role_mentions=[], channel_mentions=[])
    result = resolve_discord_mentions(message, "hey <@!123> check this")
    assert result == "hey @Ricco check this"


def test_role_mention_resolved_to_role_name():
    message = SimpleNamespace(mentions=[], role_mentions=[_role(456, "Security & Operations Support")], channel_mentions=[])
    result = resolve_discord_mentions(message, "<@&456> meeting is canceled")
    assert result == "@Security & Operations Support meeting is canceled"


def test_channel_mention_resolved_to_channel_name():
    message = SimpleNamespace(mentions=[], role_mentions=[], channel_mentions=[_channel(789, "support-tickets")])
    result = resolve_discord_mentions(message, "see <#789> for details")
    assert result == "see #support-tickets for details"


def test_multiple_mentions_all_resolved():
    message = SimpleNamespace(
        mentions=[_user(1, "Ricco")],
        role_mentions=[_role(2, "QA"), _role(3, "AI Engineer")],
        channel_mentions=[_channel(4, "general")],
    )
    result = resolve_discord_mentions(message, "<@&2> <@&3> meeting canceled, msg <@1> or see <#4>")
    assert result == "@QA @AI Engineer meeting canceled, msg @Ricco or see #general"


def test_format_discussion_body_resolves_mentions_in_content():
    role = _role(456, "AI Engineer")
    message = SimpleNamespace(
        content="<@&456> standup moved to 3pm",
        author="joeblack101",
        attachments=[],
        mentions=[],
        role_mentions=[role],
        channel_mentions=[],
        id=999,
        created_at=SimpleNamespace(tzinfo=None, replace=lambda **kw: SimpleNamespace(astimezone=lambda tz: SimpleNamespace(isoformat=lambda: "2026-01-01T00:00:00+00:00"))),
    )
    body = format_discussion_body(message)
    assert "@AI Engineer standup moved to 3pm" in body
    assert "<@&456>" not in body
