"""Contract tests — the convergence guarantee.

These run in every worktree, on every stream, as part of `make check`. They do
not test any stream's implementation; they assert that the shared data contract
and the shared fixtures still mean what all three streams assume they mean.

If one of these breaks, a stream has drifted from the contract and integration
would have failed later and more expensively.
"""

from __future__ import annotations

import pytest

from app.models import CONTRACT_VERSION, SectionKind, SourceRecord, SourceType
from tests.fixtures import CONDITIONS, all_records, load_corpus

# --------------------------------------------------------------------------
# Source records
# --------------------------------------------------------------------------


def test_every_fixture_record_validates(corpus):
    assert len(corpus) >= 20, "fixture corpus unexpectedly small"
    assert all(isinstance(r, SourceRecord) for r in corpus)


def test_source_ids_are_unique_and_well_formed(corpus):
    ids = [r.source_id for r in corpus]
    assert len(ids) == len(set(ids)), "duplicate source_id in fixtures"
    for r in corpus:
        assert r.source_id == f"{r.provider}:{r.native_id}"


def test_citable_text_integrity(corpus):
    """text_sha must match text.

    Citation offsets index into `text`. If a stream changes how text is built
    without changing the sha, every stored citation silently becomes wrong.
    """
    for r in corpus:
        assert r.text, f"{r.source_id} has empty citable text"
        assert r.text_sha == SourceRecord.sha(r.text), f"{r.source_id} sha/text mismatch"


def test_each_record_has_its_typed_metadata(corpus):
    for r in corpus:
        match r.source_type:
            case SourceType.TRIAL:
                assert r.trial is not None and r.trial.nct_id
            case SourceType.LITERATURE:
                assert r.literature is not None and r.literature.pmid
            case SourceType.DRUG_LABEL:
                assert r.drug_label is not None and r.drug_label.spl_id
            case SourceType.GUIDELINE:
                pass


def test_raw_payload_is_retained_for_audit(corpus):
    """Snapshots must be regenerable months later from what we stored."""
    for r in corpus:
        assert r.payload, f"{r.source_id} dropped its upstream payload"


@pytest.mark.parametrize("condition", CONDITIONS)
def test_both_gold_conditions_present(condition):
    records = load_corpus(condition)
    kinds = {r.source_type for r in records}
    assert SourceType.TRIAL in kinds
    assert SourceType.LITERATURE in kinds


# --------------------------------------------------------------------------
# The distinction the ranking tier depends on
# --------------------------------------------------------------------------


def test_uncited_recent_literature_exists_in_fixtures():
    """The fixtures must contain a recent, authoritative, ZERO-citation record.

    This is the case that breaks a naive credibility x recency blend: a major
    society guideline months old has rcr=None and citation_count=0 not because
    it is weak but because it has not had time to accrue anything. Ranking must
    tier on age and IGNORE citation metrics here rather than score them as zero.

    If this assertion ever fails, the fixtures no longer exercise the case and
    app/rank/ can regress silently.
    """
    lit = [r.literature for r in all_records() if r.literature]
    uncited_recent = [m for m in lit if m.rcr is None and (m.year or 0) >= 2025]
    assert uncited_recent, "fixtures no longer contain a recent zero-citation guideline"


def test_rcr_none_is_distinguishable_from_zero():
    """None means 'not yet accrued'. It must never be coerced to 0.0."""
    lit = [r.literature for r in all_records() if r.literature]
    assert any(m.rcr is None for m in lit)
    assert any(m.rcr is not None and m.rcr > 0 for m in lit)


def test_copublications_exist_to_exercise_dedup():
    """Guidelines co-published across journals must be collapsed by ranking.

    Two journal records of one guideline would otherwise double-count as
    corroboration — the strongest replication signal in the credibility score.
    """
    titles = [
        (r.title or "").lower().strip()
        for r in all_records()
        if r.source_type is SourceType.LITERATURE
    ]
    shared_prefixes = {
        t[:60]
        for t in titles
        if titles.count(t) > 1 or sum(1 for o in titles if o[:60] == t[:60]) > 1
    }
    assert shared_prefixes, "fixtures no longer contain a co-published guideline pair"


# --------------------------------------------------------------------------
# Briefing contract
# --------------------------------------------------------------------------


def test_briefing_validates_and_is_versioned(briefing):
    assert briefing.contract_version == CONTRACT_VERSION
    assert briefing.sections


def test_every_citation_slices_back_to_its_cited_text(briefing, corpus):
    """The citation-validity gate, asserted on the good fixture.

    app/verify/ implements the production version of exactly this check. It is
    duplicated here so the FIXTURE is known-good independently of any stream.
    """
    by_id = {r.source_id: r for r in corpus}
    checked = 0
    for section in briefing.sections:
        for claim in section.claims:
            for c in claim.citations:
                rec = by_id.get(c.source_id)
                assert rec is not None, f"citation to unknown source {c.source_id}"
                assert rec.text[c.start_char : c.end_char] == c.cited_text
                assert c.text_sha == rec.text_sha
                checked += 1
    assert checked >= 6, "briefing fixture has too few citations to be useful"


def test_evidence_claims_are_cited_and_implications_are_not(briefing):
    """The two-tier contract: sourced fact vs labeled analysis, never mixed."""
    for section in briefing.sections:
        for claim in section.claims:
            if section.kind is SectionKind.EVIDENCE:
                assert claim.citations, f"uncited evidence claim {claim.claim_id}"
            else:
                assert not claim.citations, (
                    f"implications claim {claim.claim_id} carries citations; analysis "
                    "must never be presented as sourced fact"
                )


def test_renderer_needs_no_corpus_access(briefing):
    """Every cited source must be resolvable from briefing.sources alone."""
    cited = {c.source_id for s in briefing.sections for cl in s.claims for c in cl.citations}
    assert cited <= set(briefing.sources), "briefing.sources is missing a cited source"


# --------------------------------------------------------------------------
# The negative fixture
# --------------------------------------------------------------------------


def test_broken_fixture_actually_violates_every_gate(broken_briefing, corpus):
    """A verifier that passes both fixtures is not a verifier.

    Stream B's deterministic gates must FAIL on this briefing. Asserted here so
    the negative fixture cannot silently rot into a valid one.
    """
    by_id = {r.source_id: r for r in corpus}

    bad_offsets = unknown_source = smuggled_citation = 0
    for section in broken_briefing.sections:
        for claim in section.claims:
            for c in claim.citations:
                rec = by_id.get(c.source_id)
                if rec is None:
                    unknown_source += 1
                elif rec.text[c.start_char : c.end_char] != c.cited_text:
                    bad_offsets += 1
            if section.kind is SectionKind.IMPLICATIONS and claim.citations:
                smuggled_citation += 1

    assert bad_offsets >= 1, "seeded offset defect disappeared"
    assert unknown_source >= 1, "seeded phantom-source defect disappeared"
    assert smuggled_citation >= 1, "seeded tier-violation defect disappeared"


def test_broken_fixture_contains_an_entity_in_no_record(broken_briefing, corpus):
    """The unsupported-entity gate: an invented company named in prose."""
    known = set()
    for r in corpus:
        known |= r.entities()
    text = " ".join(cl.text for s in broken_briefing.sections for cl in s.claims)
    assert "Vantrix Therapeutics" in text
    assert not any("Vantrix" in e for e in known), "seeded phantom entity leaked into fixtures"
