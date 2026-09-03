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


def test_real_incident_first_name_does_not_match_unrelated_full_name():
    """The actual incident this fix addresses: a termination-notice lookup for
    Discord first name "Matthew" fuzzy-matched a completely different Plaky
    user, "Mateo Sevilla" (raw ratio 0.667, well below the 0.82 floor other
    single-token comparisons already use) -- and nearly sent the wrong person
    an offboarding email. Confirmed via direct SequenceMatcher measurement,
    not assumption, before writing this threshold."""
    assert best_match("matthew", ["Mateo Sevilla", "Someone Else"]) is None
    assert best_match("matthew", ["mateo"]) is None  # leading-token retry shape (plaky.py)


def test_short_nickname_does_not_match_different_persons_full_name():
    m = best_match("matt", ["mateo sevilla", "someone else"])
    assert m is None


def test_real_first_name_still_matches_its_own_full_name():
    """Must not overcorrect: a genuine first-name query still finds its real
    person once that person is actually a roster candidate."""
    m = best_match("matthew", ["Matthew Reynolds", "Someone Else"])
    assert m is not None
    assert m.index == 0
    m2 = best_match("mateo", ["Mateo Sevilla", "Someone Else"])
    assert m2 is not None
    assert m2.index == 0


def test_longer_single_token_typos_still_match_above_the_raised_floor():
    """The raised 0.82 floor must not blanket-kill single-token typo
    tolerance -- names of 4+ characters with a small typo still clear it
    comfortably (measured: kevvin/kevin=0.909, kathryn/katheryn=0.933,
    ryan/ryann=0.889)."""
    assert best_match("kevvin", ["Kevin", "Someone Else"]) is not None
    assert best_match("kathryn", ["Katheryn", "Someone Else"]) is not None
    assert best_match("ryan", ["Ryann", "Someone Else"]) is not None


def test_three_letter_nickname_requires_more_than_ratio_to_match():
    """"jon" vs "John" (ratio 0.857) is now below the length-4 floor applied
    to the whole-candidate ratio check -- consistent with erik/kyle above,
    a 3-character query is even more collision-prone than a 4-character one
    (e.g. "jon" is just as close to "ron" or "don"), so it must go through a
    different signal (an exact-token match, a real first-name field, etc.)
    rather than raw ratio. Documents the tradeoff, not an oversight."""
    assert best_match("jon", ["John", "Someone Else"]) is None


def test_identical_edit_distance_pairs_cannot_be_distinguished_by_ratio_alone():
    """Documented domain-of-validity boundary found while calibrating this fix:
    "erik"/"eric" (same person, real typo) and "kyle"/"kyla" (two different
    people) score identically on ratio, matched-char count, AND edit distance
    (0.75 / 3 / 1, every single one) -- there is no string-similarity-only
    threshold that accepts one and rejects the other. Given this file's
    stated priority (refuse to guess rather than risk the wrong person), both
    must refuse rather than only the false one refusing by luck."""
    assert best_match("erik", ["Eric", "Someone Else"]) is None
    assert best_match("kyle", ["Kyla", "Someone Else"]) is None


def test_short_unrelated_names_do_not_match():
    assert best_match("sara", ["Mara", "Someone Else"]) is None
    assert best_match("sean", ["John", "Someone Else"]) is None


def test_real_incident_first_name_plus_bare_initial_does_not_match_unrelated_full_name():
    """The actual incident this fix addresses: a termination-notice lookup
    for GitHub real name "Joe Black" incorrectly matched a Plaky roster entry
    shaped "Joe H<surname>" at 0.9 confidence via a bare-initial shortcut
    ("H" treated as if it could only mean that one person's specific
    surname) -- sent a real offboarding email to a completely unrelated
    person. A bare initial is compatible with dozens of surnames in any real
    roster and must not stand in for a full token match."""
    assert best_match("joe h", ["Joe Hauer", "Someone Else"]) is None
    assert best_match("joe black", ["Joe Hauer", "Someone Else"]) is None


def test_full_name_typo_still_matches_via_ratio_fallback():
    """Must not overcorrect: a genuine full "First Last" typo (not an
    abbreviated initial) still matches through the whole-string ratio
    comparison, unaffected by removing the bare-initial shortcut."""
    m = best_match("jordan runyan", ["Jordan Runyon", "Someone Else"])
    assert m is not None
    assert m.index == 0
