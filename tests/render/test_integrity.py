"""Structural integrity checks.

Driven off the shared fixtures. Where a test needs a state the fixtures do not
contain (a stale date, a judged claim), it derives it from a loaded fixture
with ``model_copy`` rather than hand-building a Briefing — the object stays
real and the streams stay convergent.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models import Briefing, JudgeResult, SectionKind, Verdict
from app.render.integrity import DefectCode, Severity, inspect

AS_OF = date(2026, 9, 19)


def codes(report) -> set[str]:
    return {d.code.value for d in report.defects}


# --------------------------------------------------------------------------
# the good fixture must come out clean, or every negative test below is noise
# --------------------------------------------------------------------------


def test_good_briefing_is_structurally_clean(briefing):
    report = inspect(briefing, today=briefing.as_of)
    assert report.is_clean, [d.detail for d in report.defects]
    assert not report.errors
    assert not report.warnings


def test_good_briefing_counts_what_it_saw(briefing):
    report = inspect(briefing, today=briefing.as_of)
    expected_claims = sum(len(s.claims) for s in briefing.sections)
    expected_cites = sum(len(c.citations) for s in briefing.sections for c in s.claims)
    assert report.claim_count == expected_claims
    assert report.citation_count == expected_cites
    assert report.judged_claim_count == 0
    assert report.unjudged_claim_count == expected_claims


# --------------------------------------------------------------------------
# the broken fixture: what the renderer can see, and what it honestly cannot
# --------------------------------------------------------------------------


def test_broken_briefing_is_not_clean(broken_briefing):
    report = inspect(broken_briefing, today=broken_briefing.as_of)
    assert not report.is_clean
    assert report.errors


def test_dangling_citation_is_detected(broken_briefing):
    """Defect 2: a citation to NCT99999999, which briefing.sources does not list."""
    report = inspect(broken_briefing, today=broken_briefing.as_of)
    dangling = [d for d in report.defects if d.code is DefectCode.DANGLING_CITATION]
    assert len(dangling) == 1
    assert dangling[0].severity is Severity.ERROR
    assert dangling[0].claim_id == "emg-1"
    assert dangling[0].citation_index is not None


def test_tier_violation_is_detected(broken_briefing):
    """Defect 3: an implications claim wearing a citation.

    This is the defect the surface exists to prevent, so it is an ERROR, not
    a stylistic note.
    """
    report = inspect(broken_briefing, today=broken_briefing.as_of)
    violations = [d for d in report.defects if d.code is DefectCode.TIER_VIOLATION]
    assert len(violations) == 1
    assert violations[0].severity is Severity.ERROR
    assert violations[0].claim_id == "imp-1"


def test_corpus_dependent_defects_are_honestly_out_of_scope(broken_briefing):
    """The other two seeded defects are invisible from the Briefing alone.

    Defect 1 shifts the offsets by 40 characters but preserves the span
    length, so the span arithmetic still adds up. Defect 4 names a company
    that appears in no record. Neither can be caught without the corpus, and
    the renderer must not pretend otherwise: it names both in the scope note
    instead of quietly passing them.
    """
    report = inspect(broken_briefing, today=broken_briefing.as_of)
    assert DefectCode.MALFORMED_SPAN.value not in codes(report)

    not_checked = " ".join(report.checks_not_performed)
    assert "offsets" in not_checked
    assert "company" in not_checked
    assert "phase and status" in not_checked


def test_scope_lists_are_non_empty_and_disjoint_in_spirit():
    """A clean banner is only honest if it states what it did not check."""
    from app.render.integrity import CHECKS_NOT_PERFORMED, CHECKS_PERFORMED

    assert CHECKS_PERFORMED and CHECKS_NOT_PERFORMED
    assert not set(CHECKS_PERFORMED) & set(CHECKS_NOT_PERFORMED)


# --------------------------------------------------------------------------
# defects the fixtures do not seed, derived from real fixture objects
# --------------------------------------------------------------------------


def _first_evidence_claim(briefing: Briefing):
    for si, section in enumerate(briefing.sections):
        if section.kind is SectionKind.EVIDENCE and section.claims:
            return si, section, section.claims[0]
    raise AssertionError("fixture has no evidence claim")


def _replace_first_claim(briefing: Briefing, **claim_updates) -> Briefing:
    si, section, claim = _first_evidence_claim(briefing)
    sections = list(briefing.sections)
    claims = list(section.claims)
    claims[0] = claim.model_copy(update=claim_updates)
    sections[si] = section.model_copy(update={"claims": claims})
    return briefing.model_copy(update={"sections": sections})


def test_uncited_evidence_claim_is_an_error(briefing):
    mutated = _replace_first_claim(briefing, citations=[])
    report = inspect(mutated, today=briefing.as_of)
    assert DefectCode.UNCITED_EVIDENCE_CLAIM.value in codes(report)


def test_malformed_span_is_detected(briefing):
    _, _, claim = _first_evidence_claim(briefing)
    broken_cite = claim.citations[0].model_copy(
        update={"end_char": claim.citations[0].end_char + 7}
    )
    mutated = _replace_first_claim(briefing, citations=[broken_cite])
    report = inspect(mutated, today=briefing.as_of)
    assert DefectCode.MALFORMED_SPAN.value in codes(report)


def test_missing_text_sha_is_a_warning_not_an_error(briefing):
    """Without a checksum the citation cannot be re-audited later, but the
    claim is still traceable today. That is a warning, not a defect."""
    _, _, claim = _first_evidence_claim(briefing)
    cite = claim.citations[0].model_copy(update={"text_sha": ""})
    mutated = _replace_first_claim(briefing, citations=[cite])
    report = inspect(mutated, today=briefing.as_of)
    missing = [d for d in report.defects if d.code is DefectCode.MISSING_TEXT_SHA]
    assert missing and missing[0].severity is Severity.WARNING


@pytest.mark.parametrize(
    ("verdict", "severity"),
    [
        (Verdict.SUPPORTED, None),
        (Verdict.PARTIAL, Severity.WARNING),
        (Verdict.NOT_SUPPORTED, Severity.ERROR),
        (Verdict.SPAN_IRRELEVANT, Severity.ERROR),
    ],
)
def test_judge_verdicts_map_to_the_right_severity(briefing, verdict, severity):
    """PARTIAL is flagged for analyst review, never silently passed."""
    judge = JudgeResult(verdict=verdict, reason="because", judge_model="claude-opus-5")
    mutated = _replace_first_claim(briefing, judge=judge)
    report = inspect(mutated, today=briefing.as_of)
    judged = [d for d in report.defects if d.code.value.startswith("judge_")]
    if severity is None:
        assert not judged
    else:
        assert len(judged) == 1
        assert judged[0].severity is severity
    assert report.judged_claim_count == 1


# --------------------------------------------------------------------------
# staleness
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [
        (0, None),
        (29, None),
        (30, DefectCode.AGING_AS_OF),
        (89, DefectCode.AGING_AS_OF),
        (90, DefectCode.STALE_AS_OF),
        (400, DefectCode.STALE_AS_OF),
    ],
)
def test_staleness_thresholds(briefing, age_days, expected):
    report = inspect(briefing, today=briefing.as_of + timedelta(days=age_days))
    found = {d.code for d in report.defects} & {
        DefectCode.AGING_AS_OF,
        DefectCode.STALE_AS_OF,
    }
    assert found == ({expected} if expected else set())


def test_stale_is_a_warning_and_aging_is_only_a_notice(briefing):
    stale = inspect(briefing, today=briefing.as_of + timedelta(days=120))
    assert any(d.code is DefectCode.STALE_AS_OF for d in stale.warnings)
    aging = inspect(briefing, today=briefing.as_of + timedelta(days=45))
    assert aging.is_clean, "aging is context, not a defect"
    assert any(d.code is DefectCode.AGING_AS_OF for d in aging.notices)


def test_today_defaults_to_the_clock_but_can_be_injected(briefing):
    """Injectable so rendering is deterministic and testable."""
    assert inspect(briefing, today=AS_OF).claim_count == inspect(briefing).claim_count


# --------------------------------------------------------------------------
# document level
# --------------------------------------------------------------------------


def test_unexplained_suppression_is_flagged(briefing):
    silent = briefing.model_copy(
        update={"implications_suppressed": True, "suppression_reason": None}
    )
    report = inspect(silent, today=briefing.as_of)
    assert DefectCode.SUPPRESSION_UNEXPLAINED.value in codes(report)

    explained = silent.model_copy(update={"suppression_reason": "Evidence base too thin."})
    assert DefectCode.SUPPRESSION_UNEXPLAINED.value not in codes(
        inspect(explained, today=briefing.as_of)
    )


def test_sparse_section_is_a_notice_not_a_defect(briefing):
    section = briefing.sections[0]
    density = section.density.model_copy(update={"is_sparse": True})
    sections = [section.model_copy(update={"density": density}), *briefing.sections[1:]]
    report = inspect(briefing.model_copy(update={"sections": sections}), today=briefing.as_of)

    sparse = [d for d in report.defects if d.code is DefectCode.SPARSE_SECTION]
    assert sparse and sparse[0].severity is Severity.NOTICE
    assert report.is_clean, "a section correctly reporting its own thinness is not broken"


def test_stopped_trial_citation_raises_a_notice(briefing):
    """Surfacing "terminated" on the chip is the renderer's contribution to the
    'terminated trial described as promising' failure mode. Only app/verify can
    prove the prose mis-describes it, so this is a notice, not an error."""
    trial_id = next(
        sid for sid, ref in briefing.sources.items() if ref.source_type.value == "trial"
    )
    sources = dict(briefing.sources)
    sources[trial_id] = sources[trial_id].model_copy(update={"detail": "TERMINATED"})
    report = inspect(briefing.model_copy(update={"sources": sources}), today=briefing.as_of)

    stopped = [d for d in report.defects if d.code is DefectCode.CITED_TRIAL_NOT_ACTIVE]
    assert stopped and all(d.severity is Severity.NOTICE for d in stopped)


def test_every_defect_code_has_a_human_label():
    from app.render.integrity import DEFECT_LABELS

    assert set(DEFECT_LABELS) == set(DefectCode)
