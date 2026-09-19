"""Condition-adaptive recency: how fast does this field turn over?

Oncology and established cardiology do not age at the same rate. A myeloma
regimen from 2019 may be superseded; a 2019 heart failure guideline may still be
current practice. A single fixed recency decay is therefore wrong for a
general-purpose tool — it either discards live cardiology evidence or preserves
dead oncology evidence.

Velocity is derived from data already retrieved, so it costs no extra API calls.

A LIMITATION WORTH STATING PLAINLY: this is a sample-based estimate. It is only
meaningful once the corpus is large enough to show a distribution across years.
Below MIN_RECORDS_FOR_VELOCITY the default half-life is returned unchanged
rather than a confidently-wrong number derived from six records.
"""

from __future__ import annotations

import math
from datetime import date

from app.models import FieldVelocity, SourceRecord, SourceType

#: Below this many dated records in the window, the sample cannot support an
#: estimate and the default is used.
MIN_RECORDS_FOR_VELOCITY = 20

DEFAULT_HALF_LIFE_MONTHS = 36.0
MIN_HALF_LIFE_MONTHS = 18.0
MAX_HALF_LIFE_MONTHS = 60.0

#: Shapes the rate -> half-life curve. Tuned so a quiet field (a handful of
#: records a year) lands near 40 months and a crowded one near 20.
_BASE_MONTHS = 60.0
_LOG_SCALE = 0.9


def compute_velocity(
    records: list[SourceRecord],
    *,
    window_years: int = 5,
    as_of: date | None = None,
) -> FieldVelocity:
    today = as_of or date.today()
    cutoff_year = today.year - window_years

    trials = 0
    publications = 0
    for record in records:
        if record.sort_date is None or record.sort_date.year <= cutoff_year:
            continue
        if record.source_type is SourceType.TRIAL:
            trials += 1
        elif record.source_type is SourceType.LITERATURE:
            publications += 1

    trials_per_year = trials / window_years
    publications_per_year = publications / window_years

    if trials + publications < MIN_RECORDS_FOR_VELOCITY:
        return FieldVelocity(
            trials_per_year=trials_per_year,
            publications_per_year=publications_per_year,
            half_life_months=DEFAULT_HALF_LIFE_MONTHS,
        )

    return FieldVelocity(
        trials_per_year=trials_per_year,
        publications_per_year=publications_per_year,
        half_life_months=half_life_for_rate(trials_per_year + publications_per_year),
    )


def half_life_for_rate(rate_per_year: float) -> float:
    """Map a combined annual rate to a recency half-life in months.

    Logarithmic, because the interesting difference is between 10 and 100 items
    a year, not between 500 and 600. Clamped at both ends: no field should
    discard two-year-old evidence outright, and none should treat five-year-old
    evidence as current.
    """
    if rate_per_year <= 0:
        return MAX_HALF_LIFE_MONTHS
    months = _BASE_MONTHS / (1.0 + _LOG_SCALE * math.log10(1.0 + rate_per_year))
    return round(max(MIN_HALF_LIFE_MONTHS, min(MAX_HALF_LIFE_MONTHS, months)), 1)


def recency_weight(
    sort_date: date | None, velocity: FieldVelocity, as_of: date | None = None
) -> float:
    """Exponential decay on the field's own half-life. 1.0 is current.

    Provided here so ranking and any diagnostics agree on one definition.
    """
    if sort_date is None:
        # Undated evidence is not penalised to zero — it is usually a label or
        # a registry record whose value does not depend on a publication date.
        return 0.5
    today = as_of or date.today()
    months = (today.year - sort_date.year) * 12 + (today.month - sort_date.month)
    if months <= 0:
        return 1.0
    return 0.5 ** (months / max(velocity.half_life_months, 1.0))
