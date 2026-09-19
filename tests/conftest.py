"""Pytest fixtures available to every stream. CONTRACT-OWNED."""

from __future__ import annotations

import pytest

from tests.fixtures import all_records, load_briefing, load_broken_briefing, load_corpus


@pytest.fixture(scope="session")
def myeloma_corpus():
    """Fast-moving oncology: crowded late-phase pipeline."""
    return load_corpus("multiple_myeloma")


@pytest.fixture(scope="session")
def heart_failure_corpus():
    """Stable cardiology: dense guideline base, includes zero-citation recent guidelines."""
    return load_corpus("heart_failure")


@pytest.fixture(scope="session")
def corpus():
    return all_records()


@pytest.fixture(scope="session")
def briefing():
    return load_briefing()


@pytest.fixture(scope="session")
def broken_briefing():
    return load_broken_briefing()
