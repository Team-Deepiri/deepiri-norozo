"""find_user_email_by_name's fuzzy matching, including the leading-name-token
fallback for Discord/Plaky account handles that carry random suffixes."""

from unittest.mock import patch

from plaky import find_user_email_by_name


def _fake_response(users):
    class _Resp:
        status_code = 200

        def json(self):
            return users

    return _Resp()


def test_discord_handle_with_random_suffix_matches_via_leading_name_fallback():
    """Real case: 'austin.h._83898' (Discord username) vs the only 'Austin.m.2h35'
    in the whole Plaky roster. Strict full-token matching fails (the random
    suffixes never line up); the leading-name-token fallback should still find
    the unique 'austin' match."""
    users = [
        {"name": "Austin.m.2h35", "email": "austin.m.2h35@gmail.com"},
        {"name": "Zak B.", "email": "zak@example.com"},
    ]
    with patch("plaky._request_with_rate_limit_retry", return_value=_fake_response(users)):
        email = find_user_email_by_name("austin.h._83898", "fake-key")
    assert email == "austin.m.2h35@gmail.com"


def test_two_people_sharing_leading_name_stays_ambiguous():
    """The fallback must not undo the ambiguity refusal -- two real people with
    the same first name should still refuse rather than guess."""
    users = [
        {"name": "Austin.h.111", "email": "a1@example.com"},
        {"name": "Austin.k.222", "email": "a2@example.com"},
    ]
    with patch("plaky._request_with_rate_limit_retry", return_value=_fake_response(users)):
        email = find_user_email_by_name("austin.z._99999", "fake-key")
    assert email is None


def test_clean_full_name_still_prefers_strict_match_first():
    """A real full-name query that already resolves strictly shouldn't need the
    fallback at all -- exact full-name match wins outright."""
    users = [
        {"name": "Jordan Runyon", "email": "jordan@example.com"},
        {"name": "Jordan Smith", "email": "jsmith@example.com"},
    ]
    with patch("plaky._request_with_rate_limit_retry", return_value=_fake_response(users)):
        email = find_user_email_by_name("Jordan Runyon", "fake-key")
    assert email == "jordan@example.com"
