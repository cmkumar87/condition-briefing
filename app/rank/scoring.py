"""Deterministic record scoring.

``list[SourceRecord] + FieldVelocity -> list[RecordScore]``. No model calls, no
network, no randomness: the same corpus always ranks the same way, and every
number in ``RecordScore.components`` is reproducible by hand.

The age tier is the part that matters
-------------------------------------
A naive ``credibility x recency`` blend that also folds in citation metrics
fails on exactly the records a strategy team most needs to see. The 2026 CCS
acute-heart-failure guideline in the fixtures has ``rcr=None`` and zero
citations because it was published months ago, not because it is weak. Score it
against a two-year-old paper on a blend that reads ``rcr=None`` as zero impact
and the older paper wins — on a signal that measures nothing but elapsed time.

So records are tiered by age:

* Younger than ``age_tier_months``  -> ``PROVENANCE_ONLY``. Citation metrics are
  not scored at all. They do not appear in ``components``, and the credibility
  weight that would have gone to them stays on provenance.
* Older                             -> ``PROVENANCE_AND_IMPACT``. RCR and
  clinical citations activate.

``rcr=None`` is never coerced to ``0.0`` anywhere, in either tier. In the impact
tier a missing metric is an *absent signal* — the remaining impact weight is
renormalized over whatever metrics did accrue — because a paper old enough to
have citations but missing from iCite is an indexing gap, not a zero.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import date

from app.models import (
    FieldVelocity,
    RecordScore,
    ScoreTier,
    SourceRecord,
    SourceType,
)
from app.rank.config import DEFAULT_CONFIG, RankConfig
from app.rank.dedup import (
    CopublicationGroup,
    group_copublications,
    merged_literature_metrics,
)

_MONTH_NAMES = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
_PUB_DATE = re.compile(r"^\s*(\d{4})(?:\s+([A-Za-z]{3,9}))?(?:[\s-]+(\d{1,2}))?")
_SYNTHESIS_TYPES = ("systematic review", "meta-analysis")
_WORDS = re.compile(r"[a-z0-9]+")


def _matches_guideline_body(title: str, config: RankConfig) -> bool:
    """Whether a title names a major guideline-issuing body.

    Matched on word boundaries, never as a substring: ``ada`` occurs inside
    "adapted", ``who`` inside "washout" and ``acc`` inside "accelerated", and
    substring matching would hand the major-society bonus to any paper whose
    abstract happens to contain one of them.
    """
    tokens = _WORDS.findall(title.lower())
    joined = " ".join(tokens)
    return any(
        (f" {body} " in f" {joined} ") if " " in body else (body in tokens)
        for body in config.guideline_bodies
    )


# --------------------------------------------------------------------------
# Dates and age
# --------------------------------------------------------------------------


def parse_pub_date(pub_date: str | None) -> date | None:
    """Parse a PubMed publication date to the finest precision it carries.

    PubMed emits ``2026 Apr``, ``2026 Jul 28``, ``2026 Jun 9`` or bare ``2026``.
    ``SourceRecord.sort_date`` flattens literature to 1 January of its year,
    which is too coarse for an 18-month boundary, so the month is recovered
    here when it is available.
    """
    if not pub_date:
        return None
    m = _PUB_DATE.match(pub_date)
    if not m:
        return None
    year = int(m.group(1))
    month = _MONTH_NAMES.get((m.group(2) or "")[:3].lower(), 1)
    day = int(m.group(3) or 1)
    try:
        return date(year, month, min(day, 28))
    except ValueError:  # pragma: no cover - guards malformed upstream dates
        return None


def effective_date(record: SourceRecord) -> date | None:
    """Best-precision date for a record: publication date, else sort date."""
    if record.literature:
        parsed = parse_pub_date(record.literature.pub_date)
        if parsed is not None:
            return parsed
    return record.sort_date


def age_months(record: SourceRecord, as_of: date) -> float | None:
    """Age in months, or None when the record carries no usable date."""
    when = effective_date(record)
    if when is None:
        return None
    return (as_of - when).days / 30.44


def tier_for(age: float | None, config: RankConfig) -> ScoreTier:
    """Unknown age tiers as PROVENANCE_ONLY — we cannot assert it is old."""
    if age is None or age < config.age_tier_months:
        return ScoreTier.PROVENANCE_ONLY
    return ScoreTier.PROVENANCE_AND_IMPACT


# --------------------------------------------------------------------------
# Corpus-level signals
# --------------------------------------------------------------------------


class _CorpusContext:
    """Signals that depend on the rest of the corpus, computed once."""

    def __init__(self, records: Sequence[SourceRecord]) -> None:
        self.synthesis_texts: list[str] = [
            r.text
            for r in records
            if r.literature and any(t.lower() in _SYNTHESIS_TYPES for t in r.literature.pub_types)
        ]
        self.interventions: set[str] = set()
        for r in records:
            if r.trial:
                for iv in r.trial.interventions:
                    name = iv.split(":", 1)[-1].strip().lower()
                    # Single short tokens ("placebo") are noise, not linkage.
                    if len(name) >= 6 and name != "placebo":
                        self.interventions.add(name)

    def is_corroborated(self, record: SourceRecord) -> bool:
        """Cited by a systematic review or meta-analysis already in the corpus."""
        lit = record.literature
        if not lit:
            return False
        needles = [n for n in (lit.pmid, lit.doi) if n]
        return any(
            any(n in text for n in needles)
            for text in self.synthesis_texts
            if text is not record.text
        )

    def links_to_trial(self, record: SourceRecord) -> bool:
        """Names an intervention that is under active registered study."""
        if not record.literature:
            return False
        haystack = record.text.lower()
        return any(iv in haystack for iv in self.interventions)


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def _apply_bonus(score: float, bonus: float) -> float:
    """Accumulate a bonus with diminishing returns, staying inside [0, 1].

    Additive bonuses with a hard ``min(1.0)`` clamp destroy exactly the
    discrimination they were added for: every major-society guideline pins at
    1.0 and ties, and the tie then falls to whatever the sort key happens to be.
    Spending a fraction of the remaining headroom instead keeps each signal
    visible in the final number while it can never push a record out of range.
    """
    return score + bonus * (1.0 - score)


def _literature_provenance(
    record: SourceRecord, ctx: _CorpusContext, config: RankConfig
) -> tuple[float, dict[str, float], list[str]]:
    lit = record.literature
    assert lit is not None
    components: dict[str, float] = {}
    notes: list[str] = []
    types = [t.lower() for t in lit.pub_types]

    best = max(
        (config.pub_type_scores.get(t, config.pub_type_default) for t in types),
        default=config.pub_type_default,
    )
    best_type = max(
        types,
        key=lambda t: config.pub_type_scores.get(t, config.pub_type_default),
        default="",
    )
    components["pub_type"] = best
    score = best

    def add(name: str, bonus: float, note: str | None = None) -> None:
        nonlocal score
        before = score
        score = _apply_bonus(score, bonus)
        components[name] = round(score - before, 6)
        if note:
            notes.append(note)

    if _matches_guideline_body(record.title, config):
        add("guideline_body", config.guideline_body_bonus, "major-society guideline")

    if any(t in _SYNTHESIS_TYPES for t in types if t != best_type):
        add(
            "evidence_synthesis",
            config.secondary_synthesis_bonus,
            "documents a systematic evidence review",
        )

    if lit.medline_indexed:
        add("medline_indexed", config.medline_indexed_bonus)

    if ctx.is_corroborated(record):
        add(
            "corroboration",
            config.corroboration_bonus,
            "cited by a systematic review or meta-analysis in corpus",
        )

    if ctx.links_to_trial(record):
        add("trial_linkage", config.trial_linkage_bonus)

    if lit.is_preprint:
        capped = min(score, config.preprint_credibility_cap)
        if capped < score:
            notes.append(
                f"preprint: credibility capped at {config.preprint_credibility_cap}; "
                "may corroborate a claim, never carry one alone"
            )
            components["preprint_cap"] = round(capped - score, 6)
        score = capped

    return score, components, notes


def _trial_provenance(
    record: SourceRecord, config: RankConfig
) -> tuple[float, dict[str, float], list[str]]:
    trial = record.trial
    assert trial is not None
    notes: list[str] = []

    phase_key = "|".join(sorted(trial.phases)) if trial.phases else "NA"
    phase = config.phase_scores.get(phase_key, config.phase_default)

    status_key = (trial.overall_status or "").upper()
    status = config.status_scores.get(status_key, config.status_default)
    if trial.is_terminated:
        notes.append(
            f"{status_key.lower() or 'inactive'} trial: must not be described as promising"
        )

    sponsor = config.sponsor_scores.get(trial.lead_sponsor_class, 0.40)

    if trial.enrollment and trial.enrollment > 0:
        enrollment = min(1.0, math.log10(trial.enrollment) / config.enrollment_log_saturation)
    else:
        enrollment = 0.0

    if trial.has_results:
        maturity = config.maturity_results_posted
        notes.append("results posted")
    elif status_key == "COMPLETED":
        maturity = config.maturity_completed_no_results
        notes.append("completed but no results posted")
    else:
        maturity = config.maturity_in_progress

    components = {
        "phase": phase,
        "status": status,
        "sponsor_class": sponsor,
        "enrollment": enrollment,
        "evidence_maturity": maturity,
    }
    score = (
        config.trial_weight_phase * phase
        + config.trial_weight_status * status
        + config.trial_weight_sponsor * sponsor
        + config.trial_weight_enrollment * enrollment
        + config.trial_weight_maturity * maturity
    )
    return min(score, 1.0), components, notes


def _provenance(
    record: SourceRecord, ctx: _CorpusContext, config: RankConfig
) -> tuple[float, dict[str, float], list[str]]:
    match record.source_type:
        case SourceType.LITERATURE | SourceType.GUIDELINE:
            if record.literature:
                return _literature_provenance(record, ctx, config)
            return config.pub_type_scores["guideline"], {"pub_type": 1.0}, []
        case SourceType.TRIAL:
            return _trial_provenance(record, config)
        case SourceType.DRUG_LABEL:
            return (
                config.drug_label_provenance,
                {"regulatory_label": config.drug_label_provenance},
                ["approved FDA label"],
            )
    return config.pub_type_default, {}, []  # pragma: no cover


# --------------------------------------------------------------------------
# Impact  (only ever consulted in the PROVENANCE_AND_IMPACT tier)
# --------------------------------------------------------------------------


def impact_value(rcr: float | None, clinical_citations: int | None, config: RankConfig) -> float:
    """Map citation metrics onto 0..1. At least one must be present."""
    parts: list[tuple[float, float]] = []
    if rcr is not None:
        v = max(0.0, rcr)
        parts.append((config.weight_rcr, v / (v + config.rcr_midpoint)))
    if clinical_citations is not None:
        v = max(0.0, float(clinical_citations))
        parts.append(
            (config.weight_clinical_citations, v / (v + config.clinical_citation_midpoint))
        )
    total_weight = sum(w for w, _ in parts)
    return sum(w * v for w, v in parts) / total_weight


def apply_impact(provenance: float, impact: float, config: RankConfig) -> float:
    """Modulate provenance by accrued influence, above and below neutral.

    Impact modulates provenance instead of averaging against it. Averaging
    punishes exactly the records that should be safest: a flagship guideline
    whose provenance is 0.87 and whose citation numbers are merely good comes
    out *below* the same guideline with no numbers at all, because the mean
    drags it toward the weaker signal. Here, influence above the neutral point
    buys a share of the remaining headroom and influence below it discounts
    proportionally, so the impact weight bounds how far citations can move a
    record without ever letting them overturn a large provenance gap.
    """
    neutral = config.impact_neutral
    if impact >= neutral:
        gain = config.weight_impact * (impact - neutral) / max(1e-9, 1.0 - neutral)
        return provenance + gain * (1.0 - provenance)
    penalty = config.weight_impact * (neutral - impact) / max(1e-9, neutral)
    return provenance * (1.0 - penalty)


def _impact(
    rcr: float | None, clinical_citations: int | None, config: RankConfig
) -> tuple[float | None, dict[str, float], list[str]]:
    """Impact sub-score, or None when nothing accrued.

    A metric that is ``None`` is an absent signal, never a zero. Its weight is
    redistributed across the metrics that are present; when none are present the
    whole impact term drops out and credibility falls back to provenance.
    """
    components: dict[str, float] = {}
    notes: list[str] = []

    if rcr is not None:
        v = max(0.0, rcr)
        components["rcr"] = v / (v + config.rcr_midpoint)
    else:
        notes.append("rcr not accrued; impact scored on the metrics that exist")

    if clinical_citations is not None:
        v = max(0.0, float(clinical_citations))
        components["clinical_citations"] = v / (v + config.clinical_citation_midpoint)

    if not components:
        return None, {}, ["no citation metrics accrued; scored on provenance alone"]

    return impact_value(rcr, clinical_citations, config), components, notes


# --------------------------------------------------------------------------
# Recency
# --------------------------------------------------------------------------


def half_life_months(velocity: FieldVelocity | None, config: RankConfig) -> float:
    if velocity is None or velocity.half_life_months <= 0:
        return config.default_half_life_months
    return velocity.half_life_months


def recency_score(age: float | None, half_life: float, config: RankConfig) -> float:
    """Exponential decay on a condition-adaptive half-life.

    The half-life comes from ``FieldVelocity``, so a fast-moving oncology corpus
    discounts a three-year-old paper far harder than stable cardiology does.
    """
    if age is None:
        return config.unknown_date_recency
    return 0.5 ** (max(0.0, age) / half_life)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def score_record(
    record: SourceRecord,
    *,
    ctx: _CorpusContext,
    velocity: FieldVelocity | None,
    config: RankConfig,
    as_of: date,
    rcr: float | None = None,
    clinical_citations: int | None = None,
    copublished_with: Sequence[str] = (),
) -> RecordScore:
    """Score one record. Prefer :func:`rank_records`, which supplies context."""
    age = age_months(record, as_of)
    tier = tier_for(age, config)

    provenance, components, notes = _provenance(record, ctx, config)
    components = {f"provenance.{k}": v for k, v in components.items()}
    components["provenance"] = provenance

    credibility = provenance
    if tier is ScoreTier.PROVENANCE_AND_IMPACT:
        impact, impact_components, impact_notes = _impact(rcr, clinical_citations, config)
        components.update({f"impact.{k}": v for k, v in impact_components.items()})
        notes.extend(impact_notes)
        if impact is not None:
            components["impact"] = impact
            credibility = apply_impact(provenance, impact, config)
    else:
        notes.append(
            f"under {config.age_tier_months:.0f} months old: citation metrics ignored, "
            "not scored as zero"
        )

    half_life = half_life_months(velocity, config)
    recency = recency_score(age, half_life, config)
    components["recency"] = recency
    components["age_months"] = round(age, 2) if age is not None else -1.0
    components["half_life_months"] = half_life

    total = config.weight_credibility * credibility + config.weight_recency * recency

    if copublished_with:
        notes.append("co-published as " + ", ".join(copublished_with))

    return RecordScore(
        source_id=record.source_id,
        total=round(total, 6),
        credibility=round(credibility, 6),
        recency=round(recency, 6),
        tier=tier,
        components={k: round(v, 6) for k, v in components.items()},
        notes=notes,
    )


def rank_records(
    records: Sequence[SourceRecord],
    velocity: FieldVelocity | None = None,
    *,
    config: RankConfig | None = None,
    as_of: date | None = None,
) -> list[RecordScore]:
    """Score and order a corpus, best first.

    Every input record gets exactly one ``RecordScore``. Co-publications are
    collapsed: the canonical record carries the merged citation metrics and the
    duplicates are marked with ``superseded_by`` and sorted to the end, so a
    guideline published in two journals contributes one vote, not two.
    """
    config = config or DEFAULT_CONFIG
    as_of = as_of or date.today()
    ctx = _CorpusContext(records)

    groups: list[CopublicationGroup] = group_copublications(records)
    scores: list[RecordScore] = []

    for group in groups:
        canonical = group.canonical
        if group.duplicates:
            rcr, ccc = merged_literature_metrics(group)
        else:
            lit = canonical.literature
            rcr = lit.rcr if lit else None
            ccc = lit.clinical_citation_count if lit else None

        scores.append(
            score_record(
                canonical,
                ctx=ctx,
                velocity=velocity,
                config=config,
                as_of=as_of,
                rcr=rcr,
                clinical_citations=ccc,
                copublished_with=[d.source_id for d in group.duplicates],
            )
        )
        for dup in group.duplicates:
            lit = dup.literature
            dup_score = score_record(
                dup,
                ctx=ctx,
                velocity=velocity,
                config=config,
                as_of=as_of,
                rcr=lit.rcr if lit else None,
                clinical_citations=lit.clinical_citation_count if lit else None,
            )
            dup_score.superseded_by = canonical.source_id
            dup_score.notes.append(
                f"collapsed into {canonical.source_id}: same document, different journal"
            )
            scores.append(dup_score)

    scores.sort(key=lambda s: (s.superseded_by is not None, -s.total, s.source_id))
    return scores
