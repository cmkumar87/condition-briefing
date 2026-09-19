"""Live end-to-end retrieval. Marked `network`; excluded from `make check`.

These hit the real APIs, so they assert on invariants that must hold for any
condition rather than on specific records, which change daily.
"""

from __future__ import annotations

import httpx
import pytest

from app.corpus.store import CorpusStore
from app.corpus.velocity import compute_velocity
from app.models import ConditionProfile, SourceRecord, SourceType
from app.resolve.mesh import MeshClient
from app.resolve.resolver import ConditionResolver
from app.resolve.vocab import ICD10Client, RxNormClient
from app.sources.ct_gov import ClinicalTrialsClient
from app.sources.dedup import detect_copublications
from app.sources.icite import ICiteClient
from app.sources.openfda import OpenFDAClient
from app.sources.pubmed import GUIDELINES, PubMedClient

pytestmark = pytest.mark.network


@pytest.fixture
async def http():
    async with httpx.AsyncClient(
        timeout=45.0,
        headers={"User-Agent": "condition-briefing/0.1 (tests)"},
        follow_redirects=True,
    ) as client:
        yield client


async def test_lay_query_resolves_to_the_right_mesh_concept(http):
    resolver = ConditionResolver(
        mesh=MeshClient(http), icd10=ICD10Client(http), rxnorm=RxNormClient(http)
    )
    profile = await resolver.resolve("heart attack", suggest_synonyms=False)

    assert profile.mesh_descriptor == "Myocardial Infarction"
    assert any("heart attack" in t.casefold() for t in profile.entry_terms)
    assert any(c.startswith("I21") or c.startswith("I25") for c in profile.icd10_codes)


async def test_full_retrieval_path_end_to_end(http, tmp_path):
    """resolve -> retrieve -> enrich -> dedup -> velocity -> immutable snapshot."""
    resolver = ConditionResolver(
        mesh=MeshClient(http), icd10=ICD10Client(http), rxnorm=RxNormClient(http)
    )
    profile = await resolver.resolve("multiple myeloma", suggest_synonyms=False)

    trials = await ClinicalTrialsClient(http).search(profile, max_records=25)
    pubmed = PubMedClient(http)
    literature = await pubmed.search(profile, pub_type_filter=GUIDELINES, max_records=15)
    await ICiteClient(http).enrich(literature)
    labels = await OpenFDAClient(http).search_by_indication(profile, limit=5)

    records = [*trials, *literature, *labels]
    assert trials and literature, "core sources must return something for myeloma"

    detect_copublications(records)
    velocity = compute_velocity(records)

    store = CorpusStore(tmp_path / "corpus.sqlite")
    snapshot = store.build_and_save(
        query="multiple myeloma", profile=profile, records=records, velocity=velocity
    )

    restored = store.load(snapshot.run_id)
    assert len(restored.records) == len(records)
    for r in restored.records:
        assert r.text and r.text_sha == SourceRecord.sha(r.text)


async def test_retrieval_includes_dead_trials(http):
    """The staleness gate can only catch what retrieval actually fetched.

    If this ever returns only live trials, the deterministic check for
    'terminated trial described as promising' silently stops working.
    """
    profile = ConditionProfile(query="multiple myeloma", mesh_descriptor="Multiple Myeloma")
    trials = await ClinicalTrialsClient(http).search(profile, max_records=150)

    statuses = {t.trial.overall_status for t in trials if t.trial}
    assert statuses & {"TERMINATED", "WITHDRAWN", "SUSPENDED", "COMPLETED"}, (
        f"retrieved only live trials: {statuses}"
    )


async def test_openfda_returns_empty_rather_than_raising_for_a_rare_condition(http):
    """404 from openFDA means 'no approved labels', a normal answer."""
    profile = ConditionProfile(query="zzzz nonexistent condition xyzzy")
    assert await OpenFDAClient(http).search_by_indication(profile, limit=3) == []


async def test_pubmed_rate_limit_is_respected(http):
    """Three sequential batches must not trip NCBI's 3 req/s ceiling."""
    profile = ConditionProfile(query="heart failure", mesh_descriptor="Heart Failure")
    pubmed = PubMedClient(http)
    for _ in range(3):
        ids = await pubmed.search_ids(profile, retmax=3)
        assert ids


async def test_sparse_condition_degrades_without_error(http):
    """A rare disease must return a small corpus, not an exception."""
    profile = ConditionProfile(
        query="Niemann-Pick disease type C", mesh_descriptor="Niemann-Pick Disease, Type C"
    )
    trials = await ClinicalTrialsClient(http).search(profile, max_records=25)
    records = list(trials)
    velocity = compute_velocity(records)

    assert velocity.half_life_months > 0
    assert all(r.source_type is SourceType.TRIAL for r in records)
