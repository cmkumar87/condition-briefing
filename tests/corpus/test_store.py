"""Corpus snapshot store: immutability and round-trip fidelity."""

from __future__ import annotations

from datetime import date

import pytest

from app.corpus.store import CorpusStore, SnapshotExistsError
from app.corpus.velocity import (
    DEFAULT_HALF_LIFE_MONTHS,
    MAX_HALF_LIFE_MONTHS,
    MIN_HALF_LIFE_MONTHS,
    compute_velocity,
    half_life_for_rate,
    recency_weight,
)
from app.models import ConditionProfile, FieldVelocity
from tests.fixtures import load_corpus


@pytest.fixture
def store(tmp_path):
    return CorpusStore(tmp_path / "corpus.sqlite")


@pytest.fixture
def profile():
    return ConditionProfile(query="multiple myeloma", mesh_descriptor="Multiple Myeloma")


def test_round_trip_preserves_every_record_exactly(store, profile):
    records = load_corpus("multiple_myeloma")
    saved = store.build_and_save(query="multiple myeloma", profile=profile, records=records)

    loaded = store.load(saved.run_id)
    assert {r.source_id for r in loaded.records} == {r.source_id for r in records}

    by_id = loaded.by_id()
    for original in records:
        restored = by_id[original.source_id]
        # Citable text and its sha must survive storage byte-for-byte, or every
        # citation offset taken against this corpus becomes wrong.
        assert restored.text == original.text
        assert restored.text_sha == original.text_sha
        assert restored.payload == original.payload


def test_snapshots_are_immutable(store, profile):
    records = load_corpus("multiple_myeloma")
    saved = store.build_and_save(query="mm", profile=profile, records=records)

    with pytest.raises(SnapshotExistsError):
        store.save(saved)


def test_run_ids_are_distinct_across_runs(store, profile):
    records = load_corpus("multiple_myeloma")
    a = store.build_and_save(query="mm", profile=profile, records=records)
    b = store.build_and_save(query="mm", profile=profile, records=records)
    assert a.run_id != b.run_id
    assert len(store.list_runs()) == 2


def test_single_record_lookup_avoids_full_load(store, profile):
    records = load_corpus("multiple_myeloma")
    saved = store.build_and_save(query="mm", profile=profile, records=records)

    target = records[0]
    got = store.load_record(saved.run_id, target.source_id)
    assert got.text == target.text

    with pytest.raises(KeyError):
        store.load_record(saved.run_id, "clinicaltrials.gov:NCT00000000")


def test_velocity_survives_the_round_trip(store, profile):
    records = load_corpus("heart_failure")
    velocity = FieldVelocity(
        trials_per_year=12.0, publications_per_year=30.0, half_life_months=24.0
    )
    saved = store.build_and_save(
        query="heart failure", profile=profile, records=records, velocity=velocity
    )
    assert store.load(saved.run_id).velocity == velocity


def test_loading_an_unknown_run_raises(store):
    with pytest.raises(KeyError):
        store.load("run-does-not-exist")


# --------------------------------------------------------------------------
# Velocity
# --------------------------------------------------------------------------


def test_faster_fields_get_shorter_half_lives():
    slow = half_life_for_rate(5)
    medium = half_life_for_rate(50)
    fast = half_life_for_rate(500)
    assert MIN_HALF_LIFE_MONTHS <= fast < medium < slow <= MAX_HALF_LIFE_MONTHS


def test_half_life_is_clamped_at_both_ends():
    assert half_life_for_rate(0) == MAX_HALF_LIFE_MONTHS
    assert half_life_for_rate(10_000_000) == MIN_HALF_LIFE_MONTHS


def test_small_samples_fall_back_rather_than_guess():
    """Six fixture records cannot support a velocity estimate, and pretending
    otherwise would produce a confidently wrong half-life."""
    records = load_corpus("multiple_myeloma")
    assert len(records) < 20
    velocity = compute_velocity(records, as_of=date(2026, 9, 19))
    assert velocity.half_life_months == DEFAULT_HALF_LIFE_MONTHS


def test_velocity_uses_the_window(tmp_path):
    """Records outside the window must not inflate the rate."""
    records = load_corpus("multiple_myeloma")
    recent = compute_velocity(records, window_years=5, as_of=date(2026, 9, 19))
    ancient = compute_velocity(records, window_years=5, as_of=date(2099, 1, 1))
    assert ancient.trials_per_year == 0.0
    assert recent.trials_per_year > 0.0


def test_recency_weight_decays_on_the_fields_own_half_life():
    fast = FieldVelocity(half_life_months=18.0)
    slow = FieldVelocity(half_life_months=60.0)
    old = date(2022, 9, 1)
    today = date(2026, 9, 1)

    assert recency_weight(today, fast, as_of=today) == 1.0
    # The same four-year-old paper is heavily discounted in a fast field and
    # still substantially alive in a slow one. That contrast is the point.
    assert recency_weight(old, fast, as_of=today) < 0.25
    assert recency_weight(old, slow, as_of=today) > 0.55


def test_undated_records_are_not_zeroed():
    """Labels and registry records carry value independent of a publish date."""
    assert recency_weight(None, FieldVelocity()) == 0.5
