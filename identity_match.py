"""Fuzzy name matching for resolving a Discord display name against a roster of
candidate names (Plaky users, GitHub profiles, etc.) — same philosophy as
deepiri-boardman's boardman/assignment/person_match.py: score every candidate,
require a clear winner, and refuse to guess on a near-tie rather than silently
picking the wrong person for something as consequential as an offboarding notice
or a GitHub org removal.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional, Sequence

MIN_SCORE = 0.62
AMBIGUITY_MARGIN = 0.08


def _norm_ws_casefold(s: str) -> str:
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", s)
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " ".join(t.split()).casefold()


def _name_tokens(name: str) -> list:
    n = _norm_ws_casefold(name)
    if not n:
        return []
    return re.findall(r"[\w']+", n)


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class NameMatch:
    index: int
    score: float
    reason: str


def _containment_score(q: str, v: str) -> float:
    """Full, unbroken containment of the shorter string inside the longer one is a
    fundamentally stronger signal than generic edit-similarity gives it credit for.
    SequenceMatcher's length-normalized ratio (2M / (|q|+|v|)) treats the longer
    string's extra characters as "dissimilarity" -- but when the whole shorter
    string matches as one contiguous run, that extra content is just surrounding
    context, not evidence against a match (e.g. Discord handle "mahlaka" fully
    inside GitHub login "samimahlaka" scores a middling 0.78 on raw ratio despite
    being a near-certain match). Guarded to length >= 4: short substrings ("al",
    "an") recur constantly through the ordinary structure of real names, not
    through the rare-coincidence assumption this score is built on -- the same
    floor already used independently elsewhere in this module and in
    deepiri-boardman's person_match.py.
    """
    # Strip anything that isn't a letter/digit -- q and v are already casefolded
    # by the caller, but a Discord handle carries trailing/separator punctuation
    # ("mahlaka.") that has no bearing on whether the *name* is contained; only
    # the alphanumeric content should participate in the containment check.
    qa, va = re.sub(r"[^a-z0-9]", "", q), re.sub(r"[^a-z0-9]", "", v)
    shorter, longer = (qa, va) if len(qa) <= len(va) else (va, qa)
    if len(shorter) >= 4 and shorter in longer:
        return 0.95
    return 0.0


def _score_one(query: str, candidate: str) -> tuple:
    q = _norm_ws_casefold(query)
    v = _norm_ws_casefold(candidate)
    if not q or not v:
        return 0.0, ""

    if q == v:
        return 1.0, "exact match"

    q_tokens, v_tokens = _name_tokens(query), _name_tokens(candidate)
    multi_token = len(q_tokens) > 1

    # len(q) >= 2 guards against a bare initial ("L") spuriously matching anyone
    # whose name happens to include that single-letter token (e.g. "Luke L") —
    # an initial isn't a first name, it's an abbreviation of the surname.
    if len(q) >= 2 and q in v_tokens:
        return 0.93, f"first-name match in {candidate!r}"

    # Exact token match only -- no longer accepts a bare initial ("H") as
    # standing in for any full token starting with that letter ("Hauer",
    # "Harrison", "Henderson", ...). That shortcut was a real false-positive
    # incident: "Joe Black" (github_real_name candidate) vs a Plaky roster
    # entry shaped like "Joe H<something>" scored 0.9 -- confident-looking,
    # but a bare initial is compatible with dozens of unrelated surnames in
    # any real-size roster, and best_match's ambiguity check only catches the
    # collision when a second same-shaped candidate happens to also be
    # present, not when it's the only person with that first name + initial.
    # An abbreviated "Firstname L." query still gets a chance via the
    # whole-string ratio comparison below (gated at 0.82), which is far more
    # conservative since it also weighs the overall length difference.
    if multi_token and all(t in v_tokens for t in q_tokens):
        return 0.9, f"all parts of {query!r} match {candidate!r}"

    if not multi_token:
        containment = _containment_score(q, v)
        if containment > 0:
            return containment, f"{q!r} fully contained in {candidate!r}"
        # 0.82, not 0.5: comparing a single bare token against a whole
        # "First Last"-shaped candidate is exactly the same shape of risk as
        # the multi-token full-name-typo case below (e.g. "Matthew" vs "Mateo
        # Sevilla" scores 0.667 on raw ratio -- two different people who
        # happen to share a couple of letters), so it gets the same guard
        # rather than a laxer one just because fewer tokens were supplied.
        # len(q) >= 4: below this, ratio/edit-distance stops being a usable
        # signal at all -- "erik"/"eric" (same person, real typo) and
        # "kyle"/"kyla" (different people) score *identically* on ratio,
        # matched-char count, and edit distance (0.75 / 3 / 1, every one).
        # No threshold here can tell them apart, so a query this short must
        # go through the token loop's own guard (or another signal) instead
        # of this whole-candidate check -- matching the length floor already
        # applied there, which this check was inconsistently missing.
        if len(q) >= 4:
            ratio = _similar(q, v)
            if ratio >= 0.82:
                return ratio, f"{int(ratio * 100)}% similar to {candidate!r}"
        for token in v_tokens:
            if len(token) < 4 or len(q) < 4:
                continue
            tok_ratio = _similar(q, token) * 0.97
            if tok_ratio >= 0.82:
                return tok_ratio, f"close to {token!r} in {candidate!r}"
    else:
        # Full "First Last"-shaped typo (e.g. "Jordan Runyan" -> "Jordan Runyon").
        # Guarded high (0.82) so a shared-surname-only pair like "John San"/"Sean San"
        # (which scores ~0.63-0.67 on raw ratio) can't slip through as a false match —
        # that's the exact failure mode deepiri-boardman's person_match documents.
        ratio = _similar(q, v)
        if ratio >= 0.82:
            return ratio, f"{int(ratio * 100)}% similar to {candidate!r}"

    return 0.0, ""


def best_match(query: str, candidates: Sequence[str], *, min_score: float = MIN_SCORE) -> Optional[NameMatch]:
    """Highest-confidence candidate for a name, or None when unsure — never guesses
    on a near-tie between two different candidates."""
    q = (query or "").strip()
    if not q or not candidates:
        return None

    scored = []
    for i, candidate in enumerate(candidates):
        s, why = _score_one(q, candidate)
        if s > 0:
            scored.append((s, why, i))
    if not scored:
        return None

    scored.sort(key=lambda row: row[0], reverse=True)
    top_score, top_why, top_index = scored[0]
    if top_score < min_score:
        return None
    if len(scored) > 1:
        runner_score = scored[1][0]
        if (top_score - runner_score) < AMBIGUITY_MARGIN:
            return None
    return NameMatch(index=top_index, score=round(top_score, 3), reason=top_why)
