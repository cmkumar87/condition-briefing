"""Ranking tests. Every one drives off tests/fixtures — real captured API data.

Where a case the fixtures do not contain has to be exercised (there is no
terminated trial and no preprint in the captured corpus), the test *mutates a
loaded fixture record* rather than hand-building a SourceRecord. The record's
text, sha, payload and every other field stay exactly as captured; one field
moves. That keeps the test anchored to real data while still proving the branch
fires.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.models import FieldVelocity, ScoreTier, SourceType
from app.rank import (
    DEFAULT_CONFIG,
    group_copublications,
    normalize_title,
    parse_pub_date,
    rank_records,
)
from tests.rank._naive import naive_scores, rank_of

#: The date the fixtures were captured. Pinned so ranking tests do not start
#: failing as wall-clock time drifts past the 18-month tier boundary.
AS_OF = date(2026, 9, 19)

#: The 2026 CCS/CPCA acute heart failure guideline. Months old, rcr=None, zero
#: citations. The record the whole age-tier design exists for.
CCS_GUIDELINE = "pubmed:41951283"
#: KDIGO HF conference conclusions: same age, same document class, but it has
#: accrued rcr=3.39 and 5 citations.
KDIGO_HF = "pubmed:41793402"
#: 2026 AHA/ACC/ADA/ASN CKM guideline, co-published in JACC and Circulation.
AHA_CKM_CIRCULATION = "pubmed:42263157"
AHA_CKM_JACC = "pubmed:42265997"
#: IMWG solitary plasmacytoma recommendations: newest myeloma guideline, rcr=None.
IMWG = "pubmed:42485589"
#: NCCN myeloma guideline: 8 months older, rcr=15.7, 26 citations.
NCCN = "pubmed:41671464"


def live(scores):
    """Ranked records that were not collapsed into a co-publication."""
    return [s for s in scores if s.superseded_by is None]


def by_id(scores):
    return {s.source_id: s for s in scores}


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_every_record_is_scored_exactly_once(heart_failure_corpus):
    scores = rank_records(heart_failure_corpus, as_of=AS_OF)
    assert len(scores) == len(heart_failure_corpus)
    assert {s.source_id for s in scores} == {r.source_id for r in heart_failure_corpus}


def test_scores_are_ordered_best_first_with_collapsed_records_last(myeloma_corpus):
    scores = rank_records(myeloma_corpus, as_of=AS_OF)
    kept = live(scores)
    assert [s.total for s in kept] == sorted((s.total for s in kept), reverse=True)
    assert all(s.superseded_by for s in scores[len(kept) :])


def test_ranking_is_deterministic(heart_failure_corpus):
    a = rank_records(heart_failure_corpus, as_of=AS_OF)
    b = rank_records(list(reversed(heart_failure_corpus)), as_of=AS_OF)
    assert [s.source_id for s in a] == [s.source_id for s in b]
    assert [s.total for s in a] == [s.total for s in b]


def test_components_explain_every_score(corpus):
    """An analyst asking "why did this rank here" must get a real answer."""
    for score in rank_records(corpus, as_of=AS_OF):
        assert "provenance" in score.components
        assert "recency" in score.components
        assert "age_months" in score.components
        assert "half_life_months" in score.components
        expected = (
            DEFAULT_CONFIG.weight_credibility * score.credibility
            + DEFAULT_CONFIG.weight_recency * score.recency
        )
        assert score.total == pytest.approx(expected, abs=1e-6)
        assert 0.0 <= score.total <= 1.0


# --------------------------------------------------------------------------
# The age tier — the crux
# --------------------------------------------------------------------------


def test_recent_records_tier_on_provenance_only(heart_failure_corpus):
    scores = rank_records(heart_failure_corpus, as_of=AS_OF)
    assert all(s.tier is ScoreTier.PROVENANCE_ONLY for s in scores), (
        "every fixture record is months old; none should reach the impact tier"
    )
    for s in scores:
        assert not [k for k in s.components if k.startswith("impact")], (
            f"{s.source_id} scored a citation metric inside the provenance-only tier"
        )
        assert s.credibility == s.components["provenance"]


def test_zero_citation_guideline_is_not_outranked_for_want_of_citations(
    heart_failure_corpus,
):
    """The case the fixtures were captured to prove.

    The 2026 CCS acute heart failure guideline has rcr=None and zero citations
    because it is months old. Under a naive blend that reads rcr=None as zero
    impact it falls behind papers that beat it on nothing but elapsed time.
    Under age-tiered scoring it does not.
    """
    tiered = live(rank_records(heart_failure_corpus, as_of=AS_OF))
    naive = naive_scores(heart_failure_corpus, as_of=AS_OF)

    tiered_rank = rank_of(tiered, CCS_GUIDELINE)
    naive_rank = rank_of(naive, CCS_GUIDELINE)
    assert tiered_rank < naive_rank, (
        "the age tier must improve the standing of a recent zero-citation "
        f"guideline; tiered={tiered_rank} naive={naive_rank}"
    )

    # Specifically: it must not lose to the same-age, same-document-class
    # conference report whose only advantage is accrued citations.
    assert rank_of(tiered, CCS_GUIDELINE) < rank_of(tiered, KDIGO_HF)
    assert rank_of(naive, CCS_GUIDELINE) > rank_of(naive, KDIGO_HF), (
        "fixture no longer exercises the failure mode: the naive blend used to "
        "put the cited paper first"
    )


def test_newest_guideline_beats_an_older_heavily_cited_one(myeloma_corpus):
    """Same failure mode, oncology: IMWG (rcr=None, newest) vs NCCN (rcr=15.7)."""
    tiered = live(rank_records(myeloma_corpus, as_of=AS_OF))
    naive = naive_scores(myeloma_corpus, as_of=AS_OF)

    assert rank_of(tiered, IMWG) < rank_of(tiered, NCCN)
    assert rank_of(naive, NCCN) < rank_of(naive, IMWG), (
        "fixture no longer exercises the failure mode"
    )


def test_tier_flips_once_records_are_old_enough_to_have_accrued_citations(
    myeloma_corpus,
):
    """Past the boundary the impact signals activate — they are not dead code."""
    later = date(2028, 9, 19)  # every fixture record is then > 18 months old
    scores = by_id(rank_records(myeloma_corpus, velocity=None, config=None, as_of=later))

    nccn = scores[NCCN]
    assert nccn.tier is ScoreTier.PROVENANCE_AND_IMPACT
    assert nccn.components["impact.rcr"] > 0
    assert nccn.credibility > nccn.components["provenance"], (
        "a heavily cited paper past the boundary should gain from its citations"
    )


def test_missing_rcr_is_an_absent_signal_not_a_zero(myeloma_corpus):
    """rcr=None must never be coerced to 0.0, in either tier.

    Past the age boundary the IMWG guideline still has no rcr. Its credibility
    must fall back to provenance untouched — a zero would drag it down by a
    quarter of the credibility weight and manufacture a quality judgement out of
    an indexing gap.
    """
    later = date(2028, 9, 19)
    scores = by_id(rank_records(myeloma_corpus, as_of=later))

    imwg = scores[IMWG]
    assert imwg.tier is ScoreTier.PROVENANCE_AND_IMPACT
    assert "impact.rcr" not in imwg.components
    assert imwg.credibility == pytest.approx(imwg.components["provenance"], abs=1e-9)
    assert any("not accrued" in n or "no citation metrics" in n for n in imwg.notes)

    # And the zero it must not have been given would have cost it real ground.
    would_have_been = (DEFAULT_CONFIG.weight_provenance * imwg.components["provenance"]) / (
        DEFAULT_CONFIG.weight_provenance + DEFAULT_CONFIG.weight_impact
    )
    assert imwg.credibility > would_have_been


def test_partial_metrics_renormalize_rather_than_zero_fill(myeloma_corpus):
    """A record with clinical citations but no rcr scores on what it has."""
    later = date(2028, 9, 19)
    scores = by_id(rank_records(myeloma_corpus, as_of=later))
    partial = [
        s
        for s in scores.values()
        if "impact.clinical_citations" in s.components and "impact.rcr" not in s.components
    ]
    if not partial:
        pytest.skip("fixtures contain no record with clinical citations but no rcr")
    for s in partial:
        assert s.components["impact"] == pytest.approx(
            s.components["impact.clinical_citations"], abs=1e-9
        )


# --------------------------------------------------------------------------
# Co-publication collapse
# --------------------------------------------------------------------------


def test_copublished_guideline_counts_once(heart_failure_corpus):
    scores = by_id(rank_records(heart_failure_corpus, as_of=AS_OF))
    pair = {AHA_CKM_CIRCULATION, AHA_CKM_JACC}
    superseded = {sid for sid in pair if scores[sid].superseded_by}
    assert len(superseded) == 1, "exactly one of a co-published pair survives"
    survivor = (pair - superseded).pop()
    assert scores[superseded.pop()].superseded_by == survivor
    assert any("co-published" in n for n in scores[survivor].notes)


def test_collapse_does_not_merge_genuinely_different_documents(myeloma_corpus):
    """The two ASCO living guidelines share a title prefix but are not one paper."""
    groups = {
        g.canonical.source_id: [d.source_id for d in g.duplicates]
        for g in group_copublications(myeloma_corpus)
    }
    assert all(not dups for dups in groups.values()), (
        "no myeloma fixture record is a co-publication of another"
    )


def test_merged_metrics_take_the_max_never_the_sum(heart_failure_corpus):
    """Summing a guideline's two journal citation counts would double-count it."""
    later = date(2028, 9, 19)
    scores = by_id(rank_records(heart_failure_corpus, as_of=later))
    survivor = next(
        s
        for sid, s in scores.items()
        if sid in {AHA_CKM_CIRCULATION, AHA_CKM_JACC} and not s.superseded_by
    )
    lits = {
        r.source_id: r.literature
        for r in heart_failure_corpus
        if r.source_id in {AHA_CKM_CIRCULATION, AHA_CKM_JACC}
    }
    rcrs = [m.rcr for m in lits.values() if m.rcr is not None]
    best = max(rcrs)
    expected = best / (best + DEFAULT_CONFIG.rcr_midpoint)
    assert survivor.components["impact.rcr"] == pytest.approx(expected, abs=1e-6)


def test_normalize_title_is_case_and_punctuation_insensitive(heart_failure_corpus):
    titles = {r.source_id: r.title for r in heart_failure_corpus}
    assert titles["pubmed:41793402"] != titles["pubmed:41791738"]
    assert normalize_title(titles["pubmed:41793402"]) == normalize_title(titles["pubmed:41791738"])


# --------------------------------------------------------------------------
# Credibility signals
# --------------------------------------------------------------------------


def test_major_society_guidelines_outrank_other_practice_guidelines(
    heart_failure_corpus,
):
    """The over-retrieved paediatric oncology paper is a Practice Guideline too.

    It is not a heart failure guideline and not from a cardiology body, and it
    must not sit above the ones that are.
    """
    scores = by_id(rank_records(heart_failure_corpus, as_of=AS_OF))
    off_topic = scores["pubmed:42315465"]  # paediatric leukaemia cardiotoxicity
    assert "provenance.guideline_body" not in off_topic.components
    for sid in (CCS_GUIDELINE, KDIGO_HF, AHA_CKM_CIRCULATION):
        assert "provenance.guideline_body" in scores[sid].components
        assert scores[sid].credibility > off_topic.credibility


def test_guideline_body_matching_does_not_fire_on_substrings(myeloma_corpus):
    """`ada` lives inside "adapted", `acc` inside "accelerated"."""
    scores = by_id(rank_records(myeloma_corpus, as_of=AS_OF))
    leeds = scores["clinicaltrials.gov:NCT07649525"]  # "...response- and fitness-adapted"
    assert "provenance.guideline_body" not in leeds.components


def test_recruiting_trial_does_not_reach_guideline_credibility(heart_failure_corpus):
    """A trial that has reported nothing is evidence of a question, not an answer."""
    scores = by_id(rank_records(heart_failure_corpus, as_of=AS_OF))
    trials = [
        s
        for r in heart_failure_corpus
        if r.source_type is SourceType.TRIAL
        for s in [scores[r.source_id]]
    ]
    assert trials
    best_trial = max(t.credibility for t in trials)
    assert best_trial < scores[AHA_CKM_CIRCULATION].credibility
    for t in trials:
        assert t.components["provenance.evidence_maturity"] == pytest.approx(
            DEFAULT_CONFIG.maturity_in_progress
        )


def test_larger_later_phase_trials_outrank_small_early_ones(heart_failure_corpus):
    scores = by_id(rank_records(heart_failure_corpus, as_of=AS_OF))
    big_phase3 = scores["clinicaltrials.gov:NCT07761117"]  # PHASE3, n=6950
    small_phase23 = scores["clinicaltrials.gov:NCT06405555"]  # PHASE2/3, n=56
    assert big_phase3.credibility > small_phase23.credibility
    assert (
        big_phase3.components["provenance.phase"] > (small_phase23.components["provenance.phase"])
    )


def test_terminated_trial_is_demoted_and_flagged(heart_failure_corpus):
    """A terminated trial described as promising is the costliest briefing error.

    No fixture trial is terminated, so one captured record is copied with its
    status changed — every other field, including its text and sha, untouched.
    """
    original = next(
        r for r in heart_failure_corpus if r.source_id == "clinicaltrials.gov:NCT07761117"
    )
    terminated = original.model_copy(deep=True)
    terminated.trial.overall_status = "TERMINATED"

    corpus = [r for r in heart_failure_corpus if r.source_id != original.source_id]
    before = by_id(rank_records([*corpus, original], as_of=AS_OF))[original.source_id]
    after = by_id(rank_records([*corpus, terminated], as_of=AS_OF))[original.source_id]

    assert after.credibility < before.credibility
    assert after.components["provenance.status"] <= DEFAULT_CONFIG.status_scores["TERMINATED"]
    assert any("must not be described as promising" in n for n in after.notes)


def test_preprints_are_admitted_but_capped(myeloma_corpus):
    """Preprints may corroborate a claim; they may never top the ranking."""
    original = next(r for r in myeloma_corpus if r.source_id == IMWG)
    preprint = original.model_copy(deep=True)
    preprint.literature.is_preprint = True

    corpus = [r for r in myeloma_corpus if r.source_id != IMWG]
    scores = by_id(rank_records([*corpus, preprint], as_of=AS_OF))
    scored = scores[IMWG]
    assert scored.credibility <= DEFAULT_CONFIG.preprint_credibility_cap
    assert any("preprint" in n for n in scored.notes)
    assert scored.total < max(s.total for s in scores.values() if s.source_id != IMWG)


# --------------------------------------------------------------------------
# Recency
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026 Apr", date(2026, 4, 1)),
        ("2026 Jul 28", date(2026, 7, 28)),
        ("2026 Jun 9", date(2026, 6, 9)),
        ("2025 Oct 1", date(2025, 10, 1)),
        ("2026", date(2026, 1, 1)),
        (None, None),
        ("", None),
    ],
)
def test_pub_dates_parse_to_their_finest_precision(raw, expected):
    assert parse_pub_date(raw) == expected


def test_pub_date_beats_the_coarse_sort_date(heart_failure_corpus):
    """sort_date flattens literature to 1 January, too coarse for an 18-month line."""
    ccs = next(r for r in heart_failure_corpus if r.source_id == CCS_GUIDELINE)
    assert ccs.sort_date == date(2026, 1, 1)
    assert parse_pub_date(ccs.literature.pub_date) == date(2026, 4, 1)


def test_fast_moving_fields_discount_older_evidence_harder(myeloma_corpus):
    """Half-life comes from FieldVelocity, so oncology decays faster than cardiology."""
    oncology = FieldVelocity(
        trials_per_year=180.0, publications_per_year=4200.0, half_life_months=12.0
    )
    cardiology = FieldVelocity(
        trials_per_year=60.0, publications_per_year=1500.0, half_life_months=48.0
    )

    fast = by_id(rank_records(myeloma_corpus, oncology, as_of=AS_OF))
    slow = by_id(rank_records(myeloma_corpus, cardiology, as_of=AS_OF))

    oldest = "pubmed:40884158"  # 2025 Oct, ~11.6 months old
    assert fast[oldest].recency < slow[oldest].recency
    assert fast[oldest].components["half_life_months"] == 12.0

    # And the spread between newest and oldest is wider in the fast field.
    newest = IMWG
    assert (fast[newest].recency - fast[oldest].recency) > (
        slow[newest].recency - slow[oldest].recency
    )
