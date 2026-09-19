"""The naive scorer that app/rank/ exists to not be.

This is a deliberate anti-pattern, kept in the test tree so the ranking tests
can assert a *difference* rather than an absolute number. It is the obvious
implementation: blend credibility and recency, fold citation metrics straight
in, and read ``rcr=None`` as zero impact because ``None or 0.0`` is one
keystroke shorter than thinking about what ``None`` means.

It is never imported by anything under ``app/``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.models import RecordScore, ScoreTier, SourceRecord
from app.rank.config import DEFAULT_CONFIG, RankConfig
from app.rank.scoring import (
    _CorpusContext,
    _provenance,
    age_months,
    apply_impact,
    half_life_months,
    impact_value,
    recency_score,
)


def naive_scores(
    records: Sequence[SourceRecord],
    *,
    config: RankConfig | None = None,
    as_of: date | None = None,
) -> list[RecordScore]:
    """Same provenance and recency as the real scorer; no age tier.

    The single difference from ``rank_records`` is the two lines marked below:
    impact is always active and a missing metric is read as zero. Holding
    everything else constant is the point — it isolates the age tier as the
    cause of any ranking difference the tests observe.
    """
    config = config or DEFAULT_CONFIG
    as_of = as_of or date.today()
    ctx = _CorpusContext(records)
    out: list[RecordScore] = []

    for record in records:
        provenance, components, _ = _provenance(record, ctx, config)
        lit = record.literature

        # >>> the anti-pattern, in two lines <<<
        rcr = (lit.rcr if lit else None) or 0.0
        clinical = (lit.clinical_citation_count if lit else None) or 0

        impact = impact_value(rcr, clinical, config)
        credibility = apply_impact(provenance, impact, config)

        age = age_months(record, as_of)
        recency = recency_score(age, half_life_months(None, config), config)
        out.append(
            RecordScore(
                source_id=record.source_id,
                total=round(
                    config.weight_credibility * credibility + config.weight_recency * recency, 6
                ),
                credibility=round(credibility, 6),
                recency=round(recency, 6),
                tier=ScoreTier.PROVENANCE_AND_IMPACT,
                components={**{f"provenance.{k}": v for k, v in components.items()}},
            )
        )

    out.sort(key=lambda s: (-s.total, s.source_id))
    return out


def rank_of(scores: Sequence[RecordScore], source_id: str) -> int:
    """0-based position of a record in a ranked list."""
    return [s.source_id for s in scores].index(source_id)
