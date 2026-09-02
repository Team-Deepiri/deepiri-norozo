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


def test_truncated_handle_fully_contained_scores_near_certain():
    """Real case: 'mahlaka.' (Discord handle, trailing punctuation) fully inside
    'samimahlaka' (GitHub login) scored a middling 0.737 on raw edit-similarity
    despite being a near-certain containment match. Should score high (0.95),
    not penalized for the candidate's unrelated extra prefix content."""
    m = best_match("mahlaka.", ["samimahlaka", "someoneelse"])
    assert m is not None
    assert m.index == 0
    assert m.score >= 0.9


def test_short_containment_below_length_floor_does_not_match():
    """'al'/'an' recur constantly in real names by ordinary structure, not by
    the rare-coincidence assumption the containment score is built on -- must
    stay below the length-4 floor."""
    assert best_match("al", ["Alice Smith", "Albert Chen"]) is None
    assert best_match("an", ["Andrea K", "Anthony B"]) is None


def test_known_limitation_four_letter_word_can_coincidentally_embed():
    """Documented domain-of-validity boundary, not a bug to silently paper over:
    a real 4+ letter word can coincidentally appear inside an unrelated
    username. This is an accepted residual risk in a last-resort fallback that
    already only runs against a small, confirmed-member roster -- not
    something the length-4 floor eliminates entirely."""
    m = best_match("team", ["steampunk99"])
    assert m is not None  # documents the known false-positive shape, not desired behavior
