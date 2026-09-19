"""Co-publication collapse."""

from __future__ import annotations

from app.models import SourceType
from app.sources.dedup import detect_copublications, normalize_title
from tests.fixtures import load_corpus, load_records


def test_detects_the_known_copublished_guidelines() -> None:
    """Heart failure fixtures contain two real co-published guideline pairs."""
    records = load_records("heart_failure__literature")
    mapping = detect_copublications(records)

    assert mapping, "no co-publications found in a fixture that contains two pairs"

    for dup_id, canonical_id in mapping.items():
        dup = next(r for r in records if r.source_id == dup_id)
        canonical = next(r for r in records if r.source_id == canonical_id)
        assert dup.source_id != canonical.source_id
        assert dup.literature is not None
        assert dup.literature.copublication_of == canonical_id
        # Same work, different journals — that is the whole signature.
        assert dup.literature.journal != canonical.literature.journal


def test_canonical_copy_is_the_most_cited() -> None:
    records = load_records("heart_failure__literature")
    mapping = detect_copublications(records)
    by_id = {r.source_id: r for r in records}

    for dup_id, canonical_id in mapping.items():
        dup_cites = (by_id[dup_id].literature.citation_count) or 0
        canon_cites = (by_id[canonical_id].literature.citation_count) or 0
        assert canon_cites >= dup_cites


def test_trials_and_labels_are_never_collapsed() -> None:
    """Two trials with similar titles are two trials."""
    records = load_corpus("multiple_myeloma")
    mapping = detect_copublications(records)
    non_literature = {r.source_id for r in records if r.source_type is not SourceType.LITERATURE}
    assert not (set(mapping) & non_literature)


def test_title_normalisation_survives_case_and_punctuation() -> None:
    """The real KDIGO pair differs only in capitalisation across journals."""
    a = "Kidney Disease and Heart Failure: Recent Advances and Current Challenges"
    b = "Kidney disease and heart failure: recent advances and current challenges"
    assert normalize_title(a) == normalize_title(b)


def test_is_idempotent() -> None:
    records = load_records("heart_failure__literature")
    first = detect_copublications(records)
    second = detect_copublications(records)
    assert first == second
