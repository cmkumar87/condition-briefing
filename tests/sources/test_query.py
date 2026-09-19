"""ClinicalTrials.gov query construction.

Unit-tested separately because this API fails QUIETLY. A malformed filter does
not return an error — it returns zero studies with HTTP 200. A typo here would
ship as empty briefings rather than as a stack trace, so the query string gets
asserted directly rather than only exercised through a live call.
"""

from __future__ import annotations

from app.models import ConditionProfile
from app.sources.ct_gov import (
    DEFAULT_STATUSES,
    build_search_params,
    condition_expression,
)

PROFILE = ConditionProfile(
    query="heart attack",
    mesh_descriptor="Myocardial Infarction",
    entry_terms=["Heart Attack", "STEMI"],
)


def test_phases_are_space_separated_not_concatenated():
    """Regression: `phase:23` silently matches zero studies.

    Caught only by a live call, because the API returns 200 with an empty list.
    """
    params = build_search_params(PROFILE, phases=("2", "3"))
    assert params["aggFilters"] == "phase:2 3"
    assert "phase:23" not in params["aggFilters"]


def test_early_phase_is_appended_not_substituted():
    params = build_search_params(PROFILE, phases=("2", "3"), include_early_phase=True)
    assert params["aggFilters"] == "phase:2 3 1"


def test_dead_trial_statuses_are_retrieved_by_default():
    """Excluding them would silently disable the staleness gate: a trial that
    was never retrieved cannot be checked against its registry status."""
    params = build_search_params(PROFILE)
    statuses = set(params["filter.overallStatus"].split("|"))
    assert {"TERMINATED", "WITHDRAWN", "SUSPENDED"} <= statuses
    assert "RECRUITING" in statuses


def test_expansion_terms_are_ored_and_multiword_terms_quoted():
    expr = condition_expression(PROFILE)
    assert '"Myocardial Infarction"' in expr
    assert '"Heart Attack"' in expr
    assert "STEMI" in expr and '"STEMI"' not in expr  # single word needs no quotes
    assert expr.count(" OR ") == len(PROFILE.search_terms()) - 1


def test_an_unresolved_condition_still_produces_a_query():
    expr = condition_expression(ConditionProfile(query="some novel syndrome"))
    assert expr == '"some novel syndrome"'


def test_page_size_is_clamped_to_the_api_maximum():
    assert build_search_params(PROFILE, page_size=99_999)["pageSize"] == "1000"


def test_default_statuses_are_not_accidentally_narrowed():
    assert len(DEFAULT_STATUSES) >= 8
