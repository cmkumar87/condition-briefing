"""Deterministic ranking of retrieved source records.

No model calls. ``rank_records`` is the entry point; everything else is exposed
so an analyst-facing "why did this rank here" view can show its work.
"""

from __future__ import annotations

from app.rank.config import DEFAULT_CONFIG, RankConfig
from app.rank.dedup import (
    CopublicationGroup,
    group_copublications,
    merged_literature_metrics,
    normalize_title,
)
from app.rank.scoring import (
    age_months,
    apply_impact,
    effective_date,
    half_life_months,
    impact_value,
    parse_pub_date,
    rank_records,
    recency_score,
    score_record,
    tier_for,
)

__all__ = [
    "DEFAULT_CONFIG",
    "CopublicationGroup",
    "RankConfig",
    "age_months",
    "apply_impact",
    "effective_date",
    "group_copublications",
    "half_life_months",
    "impact_value",
    "merged_literature_metrics",
    "normalize_title",
    "parse_pub_date",
    "rank_records",
    "recency_score",
    "score_record",
    "tier_for",
]
