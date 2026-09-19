"""Layer 1 gate tests.

The governing assertion is the pair: these gates must FAIL on
``briefing__broken.json`` and PASS on ``briefing__multiple_myeloma.json``. A
verifier that passes both is not a verifier, and one that fails both is a
different kind of useless.

The broken fixture seeds four defects and exercises three gates. The two gates
it does not reach — NCT IDs in prose, and trial phase/status staleness — are
exercised by mutating a loaded fixture: one field of one real record or claim
moves and everything else stays as captured. Asserting a gate only where a
fixture happens to trip it would leave the most expensive check in the system,
"terminated trial described as promising", never once observed to fire.
"""

from __future__ import annotations

import pytest

from app.models import Briefing, SectionKind
from app.verify import (
    Gate,
    Severity,
    extract_named_entities,
    extract_phases,
    verify_deterministic,
)


def mutate_claim(briefing: Briefing, claim_id: str, **changes) -> Briefing:
    """Deep-copy a fixture briefing with one claim altered."""
    copy = briefing.model_copy(deep=True)
    for section in copy.sections:
        for claim in section.claims:
            if claim.claim_id == claim_id:
                for field, value in changes.items():
                    setattr(claim, field, value)
                return copy
    raise AssertionError(f"no claim {claim_id} in fixture")


def claim_text(briefing: Briefing, claim_id: str) -> str:
    return next(c.text for s in briefing.sections for c in s.claims if c.claim_id == claim_id)


# --------------------------------------------------------------------------
# The governing pair
# --------------------------------------------------------------------------


def test_good_briefing_passes_every_gate(briefing, corpus):
    report = verify_deterministic(briefing, corpus)
    assert report.passed, "\n".join(str(f) for f in report.findings)
    assert not report.findings


def test_broken_briefing_fails(broken_briefing, corpus):
    report = verify_deterministic(broken_briefing, corpus)
    assert not report.passed
    assert len(report.errors) >= 4


def test_the_gates_do_not_pass_both_fixtures(briefing, broken_briefing, corpus):
    assert verify_deterministic(briefing, corpus).passed
    assert not verify_deterministic(broken_briefing, corpus).passed


def test_report_proves_the_gates_actually_looked(briefing, corpus):
    """A gate that checks nothing passes everything."""
    report = verify_deterministic(briefing, corpus)
    assert report.claims_checked == sum(len(s.claims) for s in briefing.sections)
    assert report.citations_checked >= 6
    assert report.entities_checked >= 10


# --------------------------------------------------------------------------
# One assertion per seeded defect
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim_id", "gate"),
    [
        ("soc-1", Gate.CITATION_INTEGRITY),  # offsets moved off the quoted span
        ("emg-1", Gate.CITATION_INTEGRITY),  # citation to a phantom NCT record
        ("ply-2", Gate.ENTITY_SUPPORT),  # invented company
        ("imp-1", Gate.TIER_SEPARATION),  # implications claim smuggling a citation
    ],
)
def test_each_seeded_defect_is_caught_by_its_gate(broken_briefing, corpus, claim_id, gate):
    by_claim = verify_deterministic(broken_briefing, corpus).by_claim()
    assert claim_id in by_claim, f"{claim_id} produced no finding"
    assert gate in {f.gate for f in by_claim[claim_id]}
    assert all(f.severity is Severity.ERROR for f in by_claim[claim_id])


def test_bad_offsets_report_what_they_actually_sliced(broken_briefing, corpus):
    """An analyst must be able to see the mismatch, not just be told of one."""
    finding = next(
        f
        for f in verify_deterministic(broken_briefing, corpus).findings
        if f.gate is Gate.CITATION_INTEGRITY and f.claim_id == "soc-1"
    )
    assert finding.detail["sliced"] != finding.detail["cited_text"]
    assert finding.detail["start_char"] == 2460


def test_phantom_source_names_the_source_it_could_not_find(broken_briefing, corpus):
    finding = next(
        f for f in verify_deterministic(broken_briefing, corpus).findings if f.claim_id == "emg-1"
    )
    assert finding.source_id == "clinicaltrials.gov:NCT99999999"


# --------------------------------------------------------------------------
# Citation integrity
# --------------------------------------------------------------------------


def test_offsets_must_slice_back_to_the_cited_text(briefing, corpus):
    copy = briefing.model_copy(deep=True)
    citation = copy.sections[0].claims[0].citations[0]
    citation.start_char += 3
    citation.end_char += 3
    report = verify_deterministic(copy, corpus)
    assert Gate.CITATION_INTEGRITY in report.gates_failed()


def test_a_citation_taken_against_different_text_is_rejected(briefing, corpus):
    """text_sha is the version stamp that makes an offset meaningful."""
    copy = briefing.model_copy(deep=True)
    copy.sections[0].claims[0].citations[0].text_sha = "0" * 16
    report = verify_deterministic(copy, corpus)
    assert any(
        "different version" in f.message for f in report.errors if f.gate is Gate.CITATION_INTEGRITY
    )


def test_verifying_against_the_wrong_snapshot_fails_loudly(briefing, heart_failure_corpus):
    """The myeloma briefing against the cardiology corpus resolves nothing."""
    report = verify_deterministic(briefing, heart_failure_corpus)
    assert not report.passed
    assert Gate.CITATION_INTEGRITY in report.gates_failed()


# --------------------------------------------------------------------------
# NCT IDs in prose  (not reached by the broken fixture)
# --------------------------------------------------------------------------


def test_an_nct_id_in_prose_must_exist_in_the_corpus(briefing, corpus):
    original = claim_text(briefing, "emg-1")
    invented = mutate_claim(briefing, "emg-1", text=f"{original} The study is NCT99999999.")
    report = verify_deterministic(invented, corpus)
    assert Gate.TRIAL_REFERENCE in report.gates_failed()
    assert any("NCT99999999" in f.message for f in report.errors)


def test_a_real_nct_id_in_prose_passes(briefing, corpus):
    original = claim_text(briefing, "emg-1")
    real = mutate_claim(briefing, "emg-1", text=f"{original} The study is NCT07285239.")
    report = verify_deterministic(real, corpus)
    assert Gate.TRIAL_REFERENCE not in report.gates_failed()


# --------------------------------------------------------------------------
# Trial state  (not reached by the broken fixture)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A Phase 3 trial is comparing", {"PHASE3"}),
        ("a phase III study", {"PHASE3"}),
        ("the Phase II/III programme", {"PHASE2", "PHASE3"}),
        ("a Phase 1/2 dose-finding study", {"PHASE1", "PHASE2"}),
        ("Phase 0 exploratory work", {"EARLY_PHASE1"}),
        ("no phase mentioned here", set()),
    ],
)
def test_phase_language_normalizes_to_ctgov_spelling(text, expected):
    assert extract_phases(text) == expected


def test_a_misstated_phase_is_caught(briefing, corpus):
    """emg-1 cites a PHASE3 trial. Calling it Phase 1 must not pass."""
    text = claim_text(briefing, "emg-1").replace("Phase 3", "Phase 1")
    report = verify_deterministic(mutate_claim(briefing, "emg-1", text=text), corpus)
    assert Gate.TRIAL_STATE in report.gates_failed()
    assert any("PHASE1" in f.message for f in report.errors)


def test_a_misstated_recruitment_status_is_caught(briefing, corpus):
    """The cited trial is RECRUITING. Saying it has reported must not pass."""
    text = claim_text(briefing, "emg-2").replace(
        "is enrolling 750 participants in", "completed and reported results from"
    )
    report = verify_deterministic(mutate_claim(briefing, "emg-2", text=text), corpus)
    assert Gate.TRIAL_STATE in report.gates_failed()


def test_a_terminated_trial_described_as_live_is_caught(briefing, corpus):
    """The single most expensive briefing error, asserted directly.

    No fixture trial is terminated, so the cited record is copied with its
    status changed. The prose is untouched — it still reads as a live,
    enrolling study, which is exactly the failure.
    """
    cited = "clinicaltrials.gov:NCT07764978"
    snapshot = []
    for record in corpus:
        if record.source_id == cited:
            record = record.model_copy(deep=True)
            record.trial.overall_status = "TERMINATED"
        snapshot.append(record)

    report = verify_deterministic(briefing, snapshot)
    assert Gate.TRIAL_STATE in report.gates_failed()
    assert any("terminated" in f.message.lower() for f in report.errors)
    assert any(f.source_id == cited for f in report.errors)


def test_a_terminated_trial_is_fine_once_the_prose_says_so(briefing, corpus):
    """Disclosure, not silence, is what the gate wants."""
    cited = "clinicaltrials.gov:NCT07764978"
    snapshot = []
    for record in corpus:
        if record.source_id == cited:
            record = record.model_copy(deep=True)
            record.trial.overall_status = "TERMINATED"
        snapshot.append(record)

    text = claim_text(briefing, "emg-2").replace(
        "is enrolling 750 participants in", "terminated a 750-participant"
    )
    report = verify_deterministic(mutate_claim(briefing, "emg-2", text=text), snapshot)
    stale = [f for f in report.errors if f.gate is Gate.TRIAL_STATE and f.claim_id == "emg-2"]
    assert not stale, [str(f) for f in stale]


# --------------------------------------------------------------------------
# Entity support
# --------------------------------------------------------------------------


def test_every_name_in_the_good_briefing_is_vouched_for(briefing, corpus):
    """Precision matters as much as recall: a blocking gate that cries wolf blocks."""
    report = verify_deterministic(briefing, corpus)
    assert Gate.ENTITY_SUPPORT not in report.gates_failed()
    assert report.entities_checked >= 10, "extraction found too little to be meaningful"


def test_extraction_finds_the_names_a_briefing_actually_carries(briefing):
    """Drugs by INN stem, brands in capitals, companies and institutions."""
    names = {
        n.lower()
        for s in briefing.sections
        for c in s.claims
        for n in extract_named_entities(c.text)
    }
    for expected in (
        "tecvayli",  # brand, capitalised
        "daratumumab",  # -mab stem
        "bortezomib",  # -zomib stem
        "azd0120",  # sponsor development code
        "astrazeneca",  # internally capitalised company
        "glaxosmithkline",
        "university of leeds",  # proper-noun phrase, no corporate suffix
        "johnson & johnson",
    ):
        assert expected in names, f"extraction missed {expected}"


def test_an_invented_company_is_unsupported_even_with_a_valid_citation(broken_briefing, corpus):
    """ply-2's citation is genuinely valid; the sentence around it is not.

    This is the case a citation-only verifier misses entirely: real offsets into
    a real record, quoting a real span, attached to a sentence naming a company
    that does not exist.
    """
    report = verify_deterministic(broken_briefing, corpus)
    entity_errors = [f for f in report.errors if f.gate is Gate.ENTITY_SUPPORT]
    assert [f.claim_id for f in entity_errors] == ["ply-2"]
    assert "Vantrix Therapeutics" in entity_errors[0].message

    integrity = [
        f for f in report.errors if f.gate is Gate.CITATION_INTEGRITY and f.claim_id == "ply-2"
    ]
    assert not integrity, "ply-2's citation is valid; only its prose is fabricated"


# --------------------------------------------------------------------------
# Tier separation
# --------------------------------------------------------------------------


def test_an_uncited_evidence_claim_is_an_error(briefing, corpus):
    stripped = mutate_claim(briefing, "soc-1", citations=[])
    report = verify_deterministic(stripped, corpus)
    assert Gate.TIER_SEPARATION in report.gates_failed()


def test_implications_claims_are_expected_to_be_uncited(briefing, corpus):
    implications = [s for s in briefing.sections if s.kind is SectionKind.IMPLICATIONS]
    assert implications, "fixture no longer exercises the implications tier"
    report = verify_deterministic(briefing, corpus)
    assert Gate.TIER_SEPARATION not in report.gates_failed()


def test_suppressed_implications_must_actually_be_absent(briefing, corpus):
    """The flag and the content have to agree, or the flag is the lie."""
    copy = briefing.model_copy(deep=True)
    copy.implications_suppressed = True
    copy.suppression_reason = "sparse evidence"
    report = verify_deterministic(copy, corpus)
    assert any("implications_suppressed" in f.message for f in report.errors)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_findings_are_addressable_by_claim_for_the_renderer(broken_briefing, corpus):
    """Stream C flags defective claims in place rather than dropping them."""
    by_claim = verify_deterministic(broken_briefing, corpus).by_claim()
    assert set(by_claim) == {"soc-1", "emg-1", "ply-2", "imp-1"}
    assert all(f.claim_id == cid for cid, fs in by_claim.items() for f in fs)


def test_summary_states_the_outcome(briefing, broken_briefing, corpus):
    assert verify_deterministic(briefing, corpus).summary().startswith("PASS")
    assert verify_deterministic(broken_briefing, corpus).summary().startswith("FAIL")
