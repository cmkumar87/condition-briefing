"""Parity: production parsers must reproduce the committed record fixtures.

This is Stream A's spec, and it is executable rather than a matter of opinion.
``tests/fixtures/_capture.py`` holds an independent reference implementation
written against the live APIs; ``app/sources/`` holds the production one. Fed
the same raw bytes, they must agree exactly.

Two independent implementations that must agree is a much stronger check than
one shared implementation — which is why the production renderers in
app/sources/text.py are deliberately NOT imported by the capture script.

``retrieved_at`` is excluded: fixtures freeze it so they stay byte-stable across
regeneration, while production stamps wall-clock time.
"""

from __future__ import annotations

import json

import pytest

from app.models import SourceRecord
from app.sources.ct_gov import parse_study
from tests.fixtures import CONDITIONS, load_raw, load_records

COMPARE_EXCLUDE = {"retrieved_at"}


def _comparable(record: SourceRecord) -> dict:
    return json.loads(record.model_dump_json(exclude=COMPARE_EXCLUDE))


@pytest.mark.parametrize("condition", CONDITIONS)
def test_ctgov_parser_reproduces_fixture_records(condition: str) -> None:
    raw = load_raw(f"{condition}__ctgov")
    expected = load_records(f"{condition}__trials")
    studies = raw["studies"]

    assert len(studies) == len(expected), "fixture raw/record counts diverged"

    for study, want in zip(studies, expected, strict=True):
        got = parse_study(study)
        assert _comparable(got) == _comparable(want), f"parser drift on {want.source_id}"


@pytest.mark.parametrize("condition", CONDITIONS)
def test_citable_text_is_byte_identical(condition: str) -> None:
    """The offsets-are-load-bearing check, stated on its own.

    Every Citation's character offsets index into SourceRecord.text. If the
    production renderer drifts from the reference by even one character, every
    previously stored citation points at the wrong span while still looking
    valid. The sha is what makes that detectable rather than silent.
    """
    raw = load_raw(f"{condition}__ctgov")
    expected = {r.native_id: r for r in load_records(f"{condition}__trials")}

    for study in raw["studies"]:
        got = parse_study(study)
        want = expected[got.native_id]
        assert got.text == want.text, f"citable text drift on {got.source_id}"
        assert got.text_sha == want.text_sha


def test_unknown_sponsor_class_does_not_lose_the_record() -> None:
    """CT.gov adds sponsor classes over time; an unseen value must degrade."""
    raw = load_raw("multiple_myeloma__ctgov")
    study = json.loads(json.dumps(raw["studies"][0]))
    sponsors = study["protocolSection"]["sponsorCollaboratorsModule"]
    sponsors["leadSponsor"]["class"] = "NEW_CLASS_2027"

    record = parse_study(study)
    assert record.trial is not None
    assert record.trial.lead_sponsor_class.value == "UNKNOWN"
    assert record.trial.lead_sponsor, "sponsor name must survive an unknown class"


def test_terminated_trials_are_representable() -> None:
    """The staleness gate depends on terminated trials being IN the corpus.

    Retrieval deliberately does not filter them out: a trial that was never
    retrieved cannot be checked, so excluding dead trials would disable the gate
    that catches 'terminated trial described as promising'.
    """
    raw = load_raw("multiple_myeloma__ctgov")
    study = json.loads(json.dumps(raw["studies"][0]))
    study["protocolSection"]["statusModule"]["overallStatus"] = "TERMINATED"

    record = parse_study(study)
    assert record.trial is not None
    assert record.trial.is_terminated
    assert "Status: TERMINATED" in record.text, "status must be a citable span"


# --------------------------------------------------------------------------
# PubMed + iCite
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition", CONDITIONS)
def test_pubmed_and_icite_reproduce_fixture_records(condition: str) -> None:
    """Parse then enrich, matching how production runs the two passes."""
    from app.sources.icite import apply_enrichment
    from app.sources.pubmed import parse_abstracts, parse_article

    raw = load_raw(f"{condition}__pubmed")
    expected = load_records(f"{condition}__literature")

    abstracts = parse_abstracts(raw["efetch_xml"])
    results = raw["esummary"]["result"]

    got = [
        parse_article(pmid, results[pmid], abstracts.get(pmid))
        for pmid in raw["pmids"]
        if pmid in results
    ]
    apply_enrichment(got, raw["icite"].get("data") or [])

    assert len(got) == len(expected)
    for g, want in zip(got, expected, strict=True):
        assert _comparable(g) == _comparable(want), f"parser drift on {want.source_id}"


def test_icite_never_coerces_a_missing_metric_to_zero() -> None:
    """None means 'not yet accrued'. Zero would mean 'measured, no impact'.

    Conflating them lets a three-month-old major-society guideline be scored as
    though the field had evaluated and ignored it.
    """
    from app.sources.icite import apply_enrichment
    from app.sources.pubmed import parse_article

    raw = load_raw("heart_failure__pubmed")
    results = raw["esummary"]["result"]
    pmids = raw["pmids"]
    records = [parse_article(p, results[p], None) for p in pmids if p in results]
    apply_enrichment(records, raw["icite"].get("data") or [])

    metas = [r.literature for r in records if r.literature]
    assert any(m.rcr is None for m in metas), "fixture no longer exercises the case"
    assert all(m.rcr is None or m.rcr > 0 for m in metas)
    # Enrichment happened even where metrics are absent, so a consumer can tell
    # "looked up, nothing there yet" from "never looked up".
    assert all(m.icite_enriched for m in metas)


def test_enrichment_does_not_disturb_citable_text() -> None:
    """iCite metrics are metadata, not citable content.

    If enrichment changed `text`, every citation offset taken before enrichment
    would shift — so this is a guard against a whole class of silent corruption.
    """
    from app.sources.icite import apply_enrichment
    from app.sources.pubmed import parse_article

    raw = load_raw("multiple_myeloma__pubmed")
    results = raw["esummary"]["result"]
    records = [parse_article(p, results[p], None) for p in raw["pmids"] if p in results]

    before = [(r.text, r.text_sha) for r in records]
    apply_enrichment(records, raw["icite"].get("data") or [])
    assert [(r.text, r.text_sha) for r in records] == before


# --------------------------------------------------------------------------
# openFDA
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition", CONDITIONS)
def test_openfda_parser_reproduces_fixture_records(condition: str) -> None:
    from app.sources.openfda import parse_label

    raw = load_raw(f"{condition}__openfda")
    expected = load_records(f"{condition}__labels")
    got = parse_label(raw["results"][0])
    assert _comparable(got) == _comparable(expected[0])


def test_boxed_warning_is_a_citable_span() -> None:
    """Boxed warnings drive operational decisions — monitoring capacity, staff
    training, site of care — so they must be citable, not just stored."""
    from app.sources.openfda import parse_label

    raw = load_raw("multiple_myeloma__openfda")
    record = parse_label(raw["results"][0])
    assert record.drug_label is not None
    assert record.drug_label.boxed_warning
    assert "BOXED WARNING:" in record.text
    assert "CYTOKINE RELEASE SYNDROME" in record.text.upper()
