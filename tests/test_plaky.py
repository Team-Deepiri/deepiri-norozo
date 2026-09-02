"""find_user_email_by_name's fuzzy matching, including the leading-name-token
fallback for Discord/Plaky account handles that carry random suffixes."""

from unittest.mock import patch

from plaky import find_user_email, find_user_email_by_name


def _fake_response(users):
    class _Resp:
        status_code = 200

        def json(self):
            return users

    return _Resp()


def test_discord_handle_with_random_suffix_matches_via_leading_name_fallback():
    """Real case: a Discord username like 'wren.h._83898' vs the only
    'Wren.m.2h35' in the whole Plaky roster. Strict full-token matching fails
    (the random suffixes never line up); the leading-name-token fallback
    should still find the unique 'wren' match."""
    users = [
        {"name": "Wren.m.2h35", "email": "wren.m.2h35@gmail.com"},
        {"name": "Zak B.", "email": "zak@example.com"},
    ]
    with patch("plaky._request_with_rate_limit_retry", return_value=_fake_response(users)):
        email = find_user_email_by_name("wren.h._83898", "fake-key")
    assert email == "wren.m.2h35@gmail.com"


def test_two_people_sharing_leading_name_stays_ambiguous():
    """The fallback must not undo the ambiguity refusal -- two real people with
    the same first name should still refuse rather than guess."""
    users = [
        {"name": "Wren.h.111", "email": "a1@example.com"},
        {"name": "Wren.k.222", "email": "a2@example.com"},
    ]
    with patch("plaky._request_with_rate_limit_retry", return_value=_fake_response(users)):
        email = find_user_email_by_name("wren.z._99999", "fake-key")
    assert email is None


def test_clean_full_name_still_prefers_strict_match_first():
    """A real full-name query that already resolves strictly shouldn't need the
    fallback at all -- exact full-name match wins outright."""
    users = [
        {"name": "Taylor Nguyen", "email": "taylor@example.com"},
        {"name": "Taylor Smith", "email": "tsmith@example.com"},
    ]
    with patch("plaky._request_with_rate_limit_retry", return_value=_fake_response(users)):
        email = find_user_email_by_name("Taylor Nguyen", "fake-key")
    assert email == "taylor@example.com"


def _fake_paginated_response(data, has_more=False, status_code=200):
    class _Resp:
        pass

    r = _Resp()
    r.status_code = status_code
    r.text = ""
    r.json = lambda: {"data": data, "hasMore": has_more}
    return r


def test_real_response_shape_data_key_not_users_key():
    """Real Plaky API wraps results as {"data": [...], "hasMore": bool}, not a
    bare list or a "users" key -- this shape mismatch silently returned zero
    users regardless of the (separately broken) base URL/header at the time."""
    response = _fake_paginated_response([{"name": "Ricardo Beale", "email": "1lulricco@gmail.com"}])
    with patch("plaky._request_with_rate_limit_retry", return_value=response):
        email = find_user_email_by_name("Ricardo Beale", "fake-key")
    assert email == "1lulricco@gmail.com"


def test_pagination_follows_has_more_across_pages():
    page1 = _fake_paginated_response([{"name": "Someone Else", "email": "se@example.com"}], has_more=True)
    page2 = _fake_paginated_response([{"name": "Ricardo Beale", "email": "1lulricco@gmail.com"}], has_more=False)
    responses = [page1, page2]

    def fake_request(method, url, headers=None, params=None):
        return responses.pop(0)

    with patch("plaky._request_with_rate_limit_retry", side_effect=fake_request):
        email = find_user_email_by_name("Ricardo Beale", "fake-key")
    assert email == "1lulricco@gmail.com"


def test_uses_x_api_key_header_not_bearer():
    """Real API rejects Authorization: Bearer entirely with a generic 401 --
    confirmed against the live API that it expects X-API-Key instead."""
    import plaky

    headers = plaky._headers("some-key")
    assert headers.get("X-API-Key") == "some-key"
    assert "Authorization" not in headers


def test_find_user_email_exact_email_match_wins_over_any_name_fuzz():
    """A GitHub-public email matching a Plaky user's email exactly is about as
    certain as identity resolution gets -- must be checked and returned before
    any fuzzy name matching even runs."""
    users = [
        {"name": "Totally Unrelated Name", "email": "ricco@realaddress.com"},
        {"name": "Ricardo Beale", "email": "different@example.com"},
    ]
    with patch("plaky._request_with_rate_limit_retry", return_value=_fake_response(users)):
        email = find_user_email(["Some Discord Handle"], "fake-key", known_emails=["RICCO@realaddress.com"])
    assert email == "ricco@realaddress.com"


def test_find_user_email_picks_best_scoring_candidate_not_first():
    """A later candidate that scores higher must win, not whichever candidate
    happened to be tried first."""
    users = [{"name": "Ricardo Beale", "email": "ricco@example.com"}]
    with patch("plaky._request_with_rate_limit_retry", return_value=_fake_response(users)):
        # "xyz" clears no threshold at all; "Ricardo Beale" is an exact match.
        # Order shouldn't matter -- the best-scoring one wins regardless of position.
        email = find_user_email(["xyz-unrelated", "Ricardo Beale"], "fake-key")
    assert email == "ricco@example.com"


def test_find_user_email_all_signals_real_ricco_case():
    """The actual case that motivated this: GitHub real name, GitHub login, and
    two different Discord identifiers thrown at Plaky together."""
    users = [
        {"name": "Ricardo Beale", "email": "1lulricco@gmail.com"},
        {"name": "Someone Else", "email": "se@example.com"},
    ]
    with patch("plaky._request_with_rate_limit_retry", return_value=_fake_response(users)):
        email = find_user_email(["Ricardo Beale", "RiccoWrld", "Ricco", "riccorx"], "fake-key")
    assert email == "1lulricco@gmail.com"
