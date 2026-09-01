"""Fuzzy name matching used to resolve a Discord display name against a roster
(Plaky users, etc.) — mirrors deepiri-boardman's person_match test philosophy:
a clear winner or nothing, never a coin-flip guess."""

from identity_match import best_match


def test_exact_match():
    m = best_match("Jordan Runyon", ["Jordan Runyon", "Someone Else"])
    assert m is not None
    assert m.index == 0


def test_first_name_only_match():
    m = best_match("jordan", ["Jordan Runyon", "Someone Else"])
    assert m is not None
    assert m.index == 0


def test_typo_still_matches():
    m = best_match("Jordan Runyan", ["Jordan Runyon"])
    assert m is not None
    assert m.index == 0


def test_ambiguous_first_name_refuses_to_guess():
    m = best_match("chris", ["Chris Adams", "Chris Baker"])
    assert m is None


def test_unrelated_name_no_match():
    m = best_match("Zzyzx Qwerty", ["Jordan Runyon", "Someone Else"])
    assert m is None


def test_empty_query_no_match():
    assert best_match("", ["Jordan Runyon"]) is None


def test_empty_candidates_no_match():
    assert best_match("Jordan", []) is None


def test_bare_initial_does_not_spuriously_match_someone_with_that_initial():
    # "L" must not match "Luke L" just because "L" is one of its tokens (an
    # initial isn't a first name) -- found via a real-roster stress test.
    m = best_match("L", ["Luke L", "Li Ho"])
    assert m is None
