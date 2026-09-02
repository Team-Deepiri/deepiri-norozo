"""PR staleness escalation: identity resolution (GitHub PR author -> Discord
member), the one-time 2-week/1-month tiers, and the recurring author/QA-reviewer
DM cooldown cadence (weekly -> every 3 days at 3 weeks -> daily at 1 month+)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

import main


def _pr(repo="Team-Deepiri/foo", number=1, days_old=15, author="someone", draft=False):
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
    return {
        "repo": repo,
        "number": number,
        "title": "Some PR",
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "created_at": created,
        "author_login": author,
        "draft": draft,
    }


def _member(id_=1, display_name="Ricco"):
    m = Mock(spec=discord.Member)
    m.id = id_
    m.display_name = display_name
    m.mention = f"<@{id_}>"
    m.send = AsyncMock()
    m.get_role = Mock(return_value=None)
    return m


def _default_state(**overrides):
    state = {
        "notified_2week": False,
        "notified_1month": False,
        "resolved_discord_id": None,
        "last_author_dm_at": None,
        "reviewer_dm_state": {},
    }
    state.update(overrides)
    return state


def _make_state_store():
    """In-memory fake of load_pr_staleness/save_pr_staleness for scan tests."""
    saved_state = {}

    async def fake_load(repo, number):
        return dict(saved_state.get((repo, number), _default_state()))

    async def fake_save(repo, number, **kwargs):
        current = saved_state.setdefault((repo, number), _default_state())
        for k, v in kwargs.items():
            if v is not None:
                current[k] = v
        return True

    return saved_state, fake_load, fake_save


@pytest.mark.asyncio
async def test_reverse_mapping_hit_resolves_instantly(monkeypatch):
    member = _member(id_=42)
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 42 else None, members=[member])
    monkeypatch.setattr(main, "_load_github_username_map", lambda: {"42": "riccowrld"})

    result = await main._resolve_discord_member_for_github_login("RiccoWrld", guild)

    assert result is member


@pytest.mark.asyncio
async def test_falls_back_to_name_fuzzy_match(monkeypatch):
    monkeypatch.setattr(main, "_load_github_username_map", lambda: {})
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "get_user_profile", lambda login, pat: {"name": "Ricardo Beale", "email": None})
    monkeypatch.setattr(main, "PLAKY_API_KEY", None)
    remember_mock = Mock()
    monkeypatch.setattr(main, "_remember_github_username", remember_mock)

    member = _member(id_=99, display_name="Ricardo Beale")
    guild = SimpleNamespace(get_member=lambda uid: member, members=[member])

    result = await main._resolve_discord_member_for_github_login("RiccoWrld", guild)

    assert result is member
    remember_mock.assert_called_once_with(99, "RiccoWrld")


@pytest.mark.asyncio
async def test_falls_back_to_plaky_email_reverse_lookup(monkeypatch):
    monkeypatch.setattr(main, "_load_github_username_map", lambda: {})
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "get_user_profile", lambda login, pat: {"name": "Unmatchable Name Xyz", "email": None})
    monkeypatch.setattr(main, "PLAKY_API_KEY", "fake-plaky-key")
    monkeypatch.setattr(main, "find_user_email", lambda names, key: "found@example.com")
    monkeypatch.setattr(main, "find_discord_id_by_email", AsyncMock(return_value="123"))
    remember_mock = Mock()
    monkeypatch.setattr(main, "_remember_github_username", remember_mock)

    member = _member(id_=123, display_name="Totally Different Display Name")
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 123 else None, members=[])

    result = await main._resolve_discord_member_for_github_login("someuser", guild)

    assert result is member
    remember_mock.assert_called_once_with(123, "someuser")


@pytest.mark.asyncio
async def test_no_confident_match_returns_none(monkeypatch):
    monkeypatch.setattr(main, "_load_github_username_map", lambda: {})
    monkeypatch.setattr(main, "GITHUB_PAT", None)
    monkeypatch.setattr(main, "PLAKY_API_KEY", None)
    guild = SimpleNamespace(get_member=lambda uid: None, members=[])

    result = await main._resolve_discord_member_for_github_login("nobody", guild)

    assert result is None


def test_dm_cadence_tightens_with_age():
    assert main._pr_stale_dm_cadence_days(14) == 7
    assert main._pr_stale_dm_cadence_days(20) == 7
    assert main._pr_stale_dm_cadence_days(21) == 3
    assert main._pr_stale_dm_cadence_days(29) == 3
    assert main._pr_stale_dm_cadence_days(30) == 1
    assert main._pr_stale_dm_cadence_days(90) == 1


def _scan_common_mocks(monkeypatch, pr, *, member=None, qa_reviewers=None):
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "list_open_prs", lambda org, pat: [pr])
    monkeypatch.setattr(main, "_resolve_discord_member_for_github_login", AsyncMock(return_value=member))
    monkeypatch.setattr(main, "_resolve_pr_qa_reviewers", AsyncMock(return_value=qa_reviewers or []))
    monkeypatch.setattr(main, "_pr_already_reviewed_by", AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_2week_qa_channel_fires_once_with_no_qa_assigned(monkeypatch):
    pr = _pr(days_old=15)
    _scan_common_mocks(monkeypatch, pr, qa_reviewers=[])
    saved_state, fake_load, fake_save = _make_state_store()
    monkeypatch.setattr(main, "load_pr_staleness", fake_load)
    monkeypatch.setattr(main, "save_pr_staleness", fake_save)
    qa_post = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", qa_post)
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", AsyncMock())

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    qa_post.assert_awaited_once_with(pr, [])
    assert saved_state[(pr["repo"], pr["number"])]["notified_2week"] is True

    qa_post.reset_mock()
    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))
    qa_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_2week_qa_channel_includes_assigned_qa(monkeypatch):
    pr = _pr(days_old=15)
    qa_member = _member(id_=7, display_name="QA Person")
    _scan_common_mocks(monkeypatch, pr, qa_reviewers=[("qalogin", qa_member)])
    _, fake_load, fake_save = _make_state_store()
    monkeypatch.setattr(main, "load_pr_staleness", fake_load)
    monkeypatch.setattr(main, "save_pr_staleness", fake_save)
    qa_post = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", qa_post)
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", AsyncMock())

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    qa_post.assert_awaited_once_with(pr, [("qalogin", qa_member)])


@pytest.mark.asyncio
async def test_author_dm_starts_at_2_5_weeks_and_recurs_on_cooldown(monkeypatch):
    """An 18-day-old PR (past the 2.5-week DM start) should DM the author; a
    second scan the same day should not re-DM (cooldown not elapsed), but once
    the cooldown window has passed it should DM again -- this is the core
    behavior change from v1's one-time 2.5-week DM."""
    pr = _pr(days_old=18)
    member = _member(id_=55)
    guild = SimpleNamespace(get_member=lambda uid: None)
    _scan_common_mocks(monkeypatch, pr, member=member, qa_reviewers=[])
    saved_state, fake_load, fake_save = _make_state_store()
    monkeypatch.setattr(main, "load_pr_staleness", fake_load)
    monkeypatch.setattr(main, "save_pr_staleness", fake_save)
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", AsyncMock())
    dm_mock = AsyncMock()
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", dm_mock)

    await main._scan_stale_prs(guild)
    dm_mock.assert_awaited_once_with(member, pr, as_reviewer=False)

    # Same day again -- cooldown (7 days at this age) not elapsed, no re-DM.
    dm_mock.reset_mock()
    await main._scan_stale_prs(guild)
    dm_mock.assert_not_awaited()

    # Fast-forward the recorded last DM past the 7-day cooldown -- should DM again.
    key = (pr["repo"], pr["number"])
    saved_state[key]["last_author_dm_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    await main._scan_stale_prs(guild)
    dm_mock.assert_awaited_once_with(member, pr, as_reviewer=False)


@pytest.mark.asyncio
async def test_author_dm_does_not_start_before_2_5_weeks(monkeypatch):
    """At exactly 2 weeks (only the QA-channel tier), the author should not be
    DMed yet -- that starts at 2.5 weeks."""
    pr = _pr(days_old=15)
    member = _member(id_=56)
    _scan_common_mocks(monkeypatch, pr, member=member, qa_reviewers=[])
    _, fake_load, fake_save = _make_state_store()
    monkeypatch.setattr(main, "load_pr_staleness", fake_load)
    monkeypatch.setattr(main, "save_pr_staleness", fake_save)
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", AsyncMock())
    dm_mock = AsyncMock()
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", dm_mock)

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    dm_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_qa_reviewer_gets_dmed_when_not_yet_reviewed(monkeypatch):
    pr = _pr(days_old=18)
    author = _member(id_=1, display_name="Author")
    reviewer = _member(id_=2, display_name="Reviewer")
    _scan_common_mocks(monkeypatch, pr, member=author, qa_reviewers=[("reviewerlogin", reviewer)])
    monkeypatch.setattr(main, "_pr_already_reviewed_by", AsyncMock(return_value=False))
    _, fake_load, fake_save = _make_state_store()
    monkeypatch.setattr(main, "load_pr_staleness", fake_load)
    monkeypatch.setattr(main, "save_pr_staleness", fake_save)
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", AsyncMock())
    dm_mock = AsyncMock()
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", dm_mock)

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    dm_mock.assert_any_await(reviewer, pr, as_reviewer=True)


@pytest.mark.asyncio
async def test_qa_reviewer_not_dmed_once_they_have_reviewed(monkeypatch):
    pr = _pr(days_old=18)
    reviewer = _member(id_=2, display_name="Reviewer")
    _scan_common_mocks(monkeypatch, pr, member=None, qa_reviewers=[("reviewerlogin", reviewer)])
    monkeypatch.setattr(main, "_pr_already_reviewed_by", AsyncMock(return_value=True))
    _, fake_load, fake_save = _make_state_store()
    monkeypatch.setattr(main, "load_pr_staleness", fake_load)
    monkeypatch.setattr(main, "save_pr_staleness", fake_save)
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", AsyncMock())
    dm_mock = AsyncMock()
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", dm_mock)

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    for call in dm_mock.await_args_list:
        assert call.args[0] is not reviewer


@pytest.mark.asyncio
async def test_1month_announcement_fires_once_and_never_again(monkeypatch):
    """Explicit requirement: the #announcements tier (now 2.5 months / 75 days)
    must never repeat, unlike the recurring author/reviewer DMs."""
    pr = _pr(days_old=80)
    member = _member(id_=9)
    _scan_common_mocks(monkeypatch, pr, member=member, qa_reviewers=[])
    saved_state, fake_load, fake_save = _make_state_store()
    monkeypatch.setattr(main, "load_pr_staleness", fake_load)
    monkeypatch.setattr(main, "save_pr_staleness", fake_save)
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", AsyncMock())
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", AsyncMock())
    announce_mock = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_1month", announce_mock)
    claim_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "claim_pr_staleness_1month", claim_mock)

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))
    announce_mock.assert_awaited_once()

    # Even after fast-forwarding well past any DM cooldown, the announcement never re-fires
    # -- the in-memory state also reflects notified_1month=True now, same as a real gateway would.
    key = (pr["repo"], pr["number"])
    saved_state[key]["notified_1month"] = True
    saved_state[key]["last_author_dm_at"] = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    announce_mock.reset_mock()
    claim_mock.reset_mock()
    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))
    announce_mock.assert_not_awaited()
    claim_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_1month_announcement_not_posted_if_claim_denied(monkeypatch):
    """If the gateway's atomic claim says another caller already claimed this
    PR's announcement slot (claimed=False), Norozo must not post -- this is
    the actual fix for the observed double-post (two overlapping scan loops
    racing on a blind read-then-write)."""
    pr = _pr(days_old=80)
    member = _member(id_=10)
    _scan_common_mocks(monkeypatch, pr, member=member, qa_reviewers=[])
    _, fake_load, fake_save = _make_state_store()
    monkeypatch.setattr(main, "load_pr_staleness", fake_load)
    monkeypatch.setattr(main, "save_pr_staleness", fake_save)
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", AsyncMock())
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", AsyncMock())
    announce_mock = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_1month", announce_mock)
    monkeypatch.setattr(main, "claim_pr_staleness_1month", AsyncMock(return_value=False))

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    announce_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_announcement_does_not_fire_before_2_5_months(monkeypatch):
    """A 40-day-old PR (past the old 1-month threshold, but well under the new
    2.5-month/75-day one) must not trigger the #announcements tier."""
    pr = _pr(days_old=40)
    member = _member(id_=11)
    _scan_common_mocks(monkeypatch, pr, member=member, qa_reviewers=[])
    monkeypatch.setattr(main, "load_pr_staleness", AsyncMock(return_value=_default_state()))
    monkeypatch.setattr(main, "save_pr_staleness", AsyncMock())
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", AsyncMock())
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", AsyncMock())
    announce_mock = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_1month", announce_mock)
    claim_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "claim_pr_staleness_1month", claim_mock)

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    announce_mock.assert_not_awaited()
    claim_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_skips_bot_and_draft_prs(monkeypatch):
    bot_pr = _pr(number=1, days_old=40, author="deepiri-cascade[bot]")
    draft_pr = _pr(number=2, days_old=40, draft=True)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "list_open_prs", lambda org, pat: [bot_pr, draft_pr])
    monkeypatch.setattr(main, "load_pr_staleness", AsyncMock())
    qa_post = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", qa_post)

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    qa_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_skips_excluded_repos_for_everyone(monkeypatch):
    """deepiri-demo and .github never count toward staleness for any author."""
    demo_pr = _pr(repo="Team-Deepiri/deepiri-demo", number=1, days_old=40, author="anyone")
    github_pr = _pr(repo="Team-Deepiri/.github", number=2, days_old=40, author="anyone")
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "list_open_prs", lambda org, pat: [demo_pr, github_pr])
    monkeypatch.setattr(main, "load_pr_staleness", AsyncMock())
    qa_post = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", qa_post)

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    qa_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_skips_jrb00013_in_diva_only(monkeypatch):
    """jrb00013's PRs in diva are excluded, but other authors' PRs in diva
    (and jrb00013's PRs in other repos) are still tracked normally."""
    excluded = _pr(repo="Team-Deepiri/diva", number=1, days_old=15, author="jrb00013")
    other_author_in_diva = _pr(repo="Team-Deepiri/diva", number=2, days_old=15, author="someoneelse")
    jrb_in_other_repo = _pr(repo="Team-Deepiri/foo", number=3, days_old=15, author="jrb00013")
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "list_open_prs", lambda org, pat: [excluded, other_author_in_diva, jrb_in_other_repo])
    monkeypatch.setattr(main, "load_pr_staleness", AsyncMock(return_value=_default_state()))
    monkeypatch.setattr(main, "_resolve_discord_member_for_github_login", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_resolve_pr_qa_reviewers", AsyncMock(return_value=[]))
    qa_post = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", qa_post)

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    posted_for = {call.args[0]["number"] for call in qa_post.await_args_list}
    assert posted_for == {2, 3}


@pytest.mark.asyncio
async def test_scan_does_not_fire_tiers_not_yet_reached(monkeypatch):
    """A PR only 10 days old shouldn't fire any tier at all."""
    pr = _pr(days_old=10)
    monkeypatch.setattr(main, "GITHUB_ORG", "Team-Deepiri")
    monkeypatch.setattr(main, "GITHUB_PAT", "fake")
    monkeypatch.setattr(main, "list_open_prs", lambda org, pat: [pr])
    monkeypatch.setattr(main, "load_pr_staleness", AsyncMock(return_value=_default_state()))
    qa_post = AsyncMock()
    monkeypatch.setattr(main, "_post_pr_staleness_qa_channel", qa_post)
    dm_mock = AsyncMock()
    monkeypatch.setattr(main, "_dm_pr_staleness_nudge", dm_mock)

    await main._scan_stale_prs(SimpleNamespace(get_member=lambda uid: None))

    qa_post.assert_not_awaited()
    dm_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_pr_qa_reviewers_filters_by_role(monkeypatch):
    qa_member = _member(id_=1)
    qa_member.get_role = Mock(side_effect=lambda rid: object() if rid == main.QA_ROLE_ID else None)
    non_qa_member = _member(id_=2)
    non_qa_member.get_role = Mock(return_value=None)

    monkeypatch.setattr(
        main, "get_pull_request",
        lambda repo, number, pat: {"requested_reviewers": [{"login": "qaguy"}, {"login": "randomguy"}]},
    )

    async def fake_resolve(login, guild):
        return {"qaguy": qa_member, "randomguy": non_qa_member}.get(login)

    monkeypatch.setattr(main, "_resolve_discord_member_for_github_login", fake_resolve)

    pr = _pr()
    result = await main._resolve_pr_qa_reviewers(pr, SimpleNamespace())

    assert result == [("qaguy", qa_member)]


@pytest.mark.asyncio
async def test_pr_already_reviewed_by_checks_review_authors(monkeypatch):
    monkeypatch.setattr(
        main, "get_pull_request_reviews",
        lambda repo, number, pat: [{"user": {"login": "SomeReviewer"}, "state": "APPROVED"}],
    )
    pr = _pr()

    assert await main._pr_already_reviewed_by(pr, "somereviewer") is True
    assert await main._pr_already_reviewed_by(pr, "nobody") is False
