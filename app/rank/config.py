"""Ranking weights and thresholds.

Every number the scorer uses lives here, not in the scoring code, so an analyst
argument about *how much* a Phase III trial should outweigh a Phase I one is a
config change reviewed on its own, not a code change buried in a diff.

Nothing here is a model call; ranking is fully deterministic.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models import SponsorClass


class RankConfig(BaseModel):
    """Tunable ranking parameters. Frozen so a config cannot drift mid-run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ---------------------------------------------------------------- tiering
    #: Records younger than this score on provenance alone. Citation metrics are
    #: IGNORED below this age, not scored as zero: a guideline published four
    #: months ago has no citations because no one has had time to cite it, which
    #: says nothing at all about its authority.
    age_tier_months: float = 18.0

    # ------------------------------------------------------- top-level blend
    weight_credibility: float = 0.7
    weight_recency: float = 0.3

    #: Within credibility, how much impact evidence is allowed to move a record
    #: once it is old enough to have accrued any. Provenance still dominates.
    weight_provenance: float = 0.75
    weight_impact: float = 0.25

    # ------------------------------------------------- literature provenance
    #: Publication-type hierarchy. Highest matching type wins.
    #: Publication-type hierarchy. Highest matching type wins, and the ceiling is
    #: deliberately well below 1.0: if the document class alone saturated the
    #: score, every guideline would tie at 1.0 and the signals that actually
    #: separate them — issuing body, evidence synthesis, corroboration — would
    #: have nowhere to go.
    pub_type_scores: dict[str, float] = Field(
        default_factory=lambda: {
            "practice guideline": 0.80,
            "guideline": 0.80,
            "meta-analysis": 0.78,
            "consensus development conference": 0.76,
            "systematic review": 0.70,
            "randomized controlled trial": 0.58,
            "clinical trial, phase iii": 0.56,
            "clinical trial, phase ii": 0.45,
            "clinical trial, phase i": 0.38,
            "clinical trial": 0.45,
            "multicenter study": 0.42,
            "observational study": 0.38,
            "comparative study": 0.36,
            "review": 0.32,
            "journal article": 0.28,
            "case reports": 0.15,
            "editorial": 0.10,
            "comment": 0.08,
        }
    )
    pub_type_default: float = 0.28

    #: Issuing bodies whose name in a title marks a major-society guideline. This
    #: is the signal that keeps a months-old flagship guideline at the top while
    #: it has no citations. Add to it rather than reaching for citation counts.
    guideline_bodies: tuple[str, ...] = (
        "aha", "acc", "ada", "asn", "esc", "hfsa", "accf",
        "american heart association", "american college of cardiology",
        "canadian cardiovascular society", "european society of cardiology",
        "heart failure society",
        "asco", "nccn", "esmo", "imwg", "ebmt", "ash",
        "american society of clinical oncology", "international myeloma working group",
        "kdigo", "who", "nice", "ema", "fda",
    )  # fmt: skip
    guideline_body_bonus: float = 0.30

    #: Carrying a systematic-review or meta-analysis publication type *in
    #: addition to* its headline type. A guideline that documents a systematic
    #: evidence review is stronger than one that does not, and taking only the
    #: single highest publication type throws that away.
    secondary_synthesis_bonus: float = 0.12

    medline_indexed_bonus: float = 0.08
    #: Preprints are admitted but capped. They may corroborate, never carry a
    #: claim alone; the cap is what stops one topping the ranking.
    preprint_credibility_cap: float = 0.50

    #: Cited by a systematic review or meta-analysis already in the corpus.
    corroboration_bonus: float = 0.15
    #: Discusses an intervention that is under active registered study — the
    #: "has this translated to practice" signal.
    trial_linkage_bonus: float = 0.08

    # ------------------------------------------------------ trial provenance
    phase_scores: dict[str, float] = Field(
        default_factory=lambda: {
            "PHASE4": 0.90,
            "PHASE3": 0.85,
            "PHASE2|PHASE3": 0.70,
            "PHASE2": 0.55,
            "PHASE1|PHASE2": 0.40,
            "PHASE1": 0.30,
            "EARLY_PHASE1": 0.20,
            "NA": 0.25,
        }
    )
    phase_default: float = 0.25

    status_scores: dict[str, float] = Field(
        default_factory=lambda: {
            "COMPLETED": 1.00,
            "ACTIVE_NOT_RECRUITING": 0.90,
            "RECRUITING": 0.90,
            "ENROLLING_BY_INVITATION": 0.80,
            "NOT_YET_RECRUITING": 0.60,
            "UNKNOWN": 0.40,
            # A terminated or withdrawn trial is the single most expensive thing
            # to describe as promising, so it is scored near the floor rather
            # than merely discounted.
            "TERMINATED": 0.10,
            "SUSPENDED": 0.10,
            "WITHDRAWN": 0.05,
            "NO_LONGER_AVAILABLE": 0.05,
        }
    )
    status_default: float = 0.40

    sponsor_scores: dict[SponsorClass, float] = Field(
        default_factory=lambda: {
            SponsorClass.NIH: 0.90,
            SponsorClass.NETWORK: 0.85,
            SponsorClass.FED: 0.80,
            SponsorClass.OTHER_GOV: 0.80,
            SponsorClass.INDUSTRY: 0.80,
            SponsorClass.OTHER: 0.60,
            SponsorClass.INDIV: 0.30,
            SponsorClass.UNKNOWN: 0.40,
        }
    )

    #: Enrollment saturates: 10**enrollment_log_saturation participants scores 1.0.
    enrollment_log_saturation: float = 4.0

    #: Has the trial actually produced evidence yet. A recruiting Phase III
    #: study is evidence that a question is being asked, not evidence of an
    #: answer, and it should not reach the credibility of a guideline or of a
    #: trial that has reported. This is the dimension that keeps an exciting
    #: unread trial from leading a standard-of-care section.
    maturity_results_posted: float = 1.00
    maturity_completed_no_results: float = 0.75
    maturity_in_progress: float = 0.60

    # ---------------------------------------------- trial component weighting
    trial_weight_phase: float = 0.35
    trial_weight_status: float = 0.20
    trial_weight_sponsor: float = 0.15
    trial_weight_enrollment: float = 0.10
    trial_weight_maturity: float = 0.20

    # ------------------------------------------------------------ drug label
    #: An effective FDA label is a regulatory fact, not an opinion — high, but
    #: just under a major-society guideline, which synthesizes the evidence the
    #: label reports rather than one product's share of it.
    drug_label_provenance: float = 0.85

    # ---------------------------------------------------------------- impact
    #: Metrics map through x/(x+midpoint), a saturating curve anchored on the
    #: metric's own definition rather than an arbitrary ceiling. RCR is already
    #: field- and time-normalized with 1.0 meaning "the average NIH-funded
    #: paper", so RCR 1.0 lands exactly on the neutral point below.
    rcr_midpoint: float = 1.0
    clinical_citation_midpoint: float = 2.0
    weight_rcr: float = 0.6
    weight_clinical_citations: float = 0.4

    #: The impact score of an unremarkable paper. Above it, accrued influence
    #: raises credibility; below it, an old paper nobody cited is discounted.
    #: Impact modulates provenance rather than averaging against it: a flagship
    #: guideline with merely good citation numbers must not come out *worse*
    #: than the same guideline with no numbers at all, which is what a blend
    #: does as soon as provenance outruns impact.
    impact_neutral: float = 0.5

    # --------------------------------------------------------------- recency
    #: Used when a corpus carries no FieldVelocity.
    default_half_life_months: float = 36.0
    #: Recency assigned when a record has no usable date at all. Neutral by
    #: design: unknown age is not evidence of being old.
    unknown_date_recency: float = 0.50


DEFAULT_CONFIG = RankConfig()
