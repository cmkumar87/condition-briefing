"""Shared data contract for the condition briefing engine.

FROZEN CONTRACT — single-owner file.

Every workstream reads this module; no workstream writes it. Streams add their
own private types inside their own packages freely. Changes here ripple to all
three streams at once, so they are additive-only and go through the contract
owner. If you are a stream agent and need a change here, ask — do not edit.

The pipeline these types describe:

    query -> ConditionProfile -> [SourceRecord] -> CorpusSnapshot
          -> [RecordScore] -> Briefing(sections -> claims -> citations)
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0.0"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# --------------------------------------------------------------------------
# Condition resolution  (Stream A owns the producer: app/resolve/)
# --------------------------------------------------------------------------


class ConditionProfile(_Base):
    """Normalized concept set for a condition, expanded for retrieval recall.

    A literal query string retrieves almost nothing from biomedical registries.
    This is the expansion that makes retrieval find the actual field, and the
    object an analyst reviews and edits before retrieval runs.
    """

    query: str
    mesh_descriptor: str | None = None
    mesh_ui: str | None = None
    entry_terms: list[str] = Field(default_factory=list)
    icd10_codes: list[str] = Field(default_factory=list)
    snomed_concepts: list[str] = Field(default_factory=list)

    #: Model-suggested synonyms that were validated against MeSH/RxNorm.
    synonyms: list[str] = Field(default_factory=list)
    #: Model-suggested synonyms that failed validation. Shown to the analyst,
    #: never used for retrieval unless the analyst promotes them.
    unvalidated_synonyms: list[str] = Field(default_factory=list)

    #: True once a human has reviewed the expansion.
    analyst_reviewed: bool = False

    def search_terms(self) -> list[str]:
        """De-duplicated terms to query registries with, best-first."""
        seen: dict[str, None] = {}
        for term in [self.mesh_descriptor, self.query, *self.entry_terms, *self.synonyms]:
            if term and term.strip():
                seen.setdefault(term.strip(), None)
        return list(seen)


# --------------------------------------------------------------------------
# Source records  (Stream A owns the producers: app/sources/)
# --------------------------------------------------------------------------


class SourceType(StrEnum):
    TRIAL = "trial"
    LITERATURE = "literature"
    DRUG_LABEL = "drug_label"
    GUIDELINE = "guideline"


class SponsorClass(StrEnum):
    INDUSTRY = "INDUSTRY"
    NIH = "NIH"
    FED = "FED"
    OTHER_GOV = "OTHER_GOV"
    NETWORK = "NETWORK"
    INDIV = "INDIV"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class TrialMeta(_Base):
    """ClinicalTrials.gov v2 fields, normalized. Structured — never model-extracted."""

    nct_id: str
    phases: list[str] = Field(default_factory=list)
    overall_status: str | None = None
    study_type: str | None = None
    enrollment: int | None = None
    start_date: str | None = None
    primary_completion_date: str | None = None
    completion_date: str | None = None
    lead_sponsor: str | None = None
    lead_sponsor_class: SponsorClass = SponsorClass.UNKNOWN
    collaborators: list[str] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    #: Distinct facility names — the "key institutions" answer.
    institutions: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    has_results: bool = False

    @property
    def is_terminated(self) -> bool:
        return (self.overall_status or "").upper() in {
            "TERMINATED",
            "WITHDRAWN",
            "SUSPENDED",
            "NO_LONGER_AVAILABLE",
        }


class LiteratureMeta(_Base):
    """PubMed metadata plus NIH iCite citation-quality enrichment."""

    pmid: str
    journal: str | None = None
    journal_abbrev: str | None = None
    pub_types: list[str] = Field(default_factory=list)
    pub_date: str | None = None
    year: int | None = None
    doi: str | None = None
    abstract: str | None = None

    is_preprint: bool = False
    medline_indexed: bool = False

    # --- iCite enrichment. None means "not yet accrued", NOT "zero impact".
    # The age-tiering in app/rank/ depends on that distinction.
    rcr: float | None = None
    nih_percentile: float | None = None
    citation_count: int | None = None
    clinical_citation_count: int | None = None
    icite_enriched: bool = False

    #: Set when this record is a journal co-publication of another record
    #: (e.g. a guideline published in both JACC and Circulation). Ranking must
    #: collapse these or corroboration is double-counted.
    copublication_of: str | None = None


class DrugLabelMeta(_Base):
    """openFDA structured product label fields."""

    spl_id: str
    brand_names: list[str] = Field(default_factory=list)
    generic_names: list[str] = Field(default_factory=list)
    manufacturers: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    pharm_classes: list[str] = Field(default_factory=list)
    effective_time: str | None = None
    boxed_warning: str | None = None
    indications: str | None = None


class SourceRecord(_Base):
    """One retrieved primary-source record.

    ``text`` is the single most important field: it is the exact string handed
    to the Citations API as a document block, and every Citation's character
    offsets index into it. Changing how ``text`` is built changes every stored
    citation, so it is versioned via ``text_sha``.
    """

    source_id: str
    source_type: SourceType
    provider: str
    native_id: str
    title: str
    url: str
    retrieved_at: datetime

    #: Normalized date used for recency scoring (publication or trial start).
    sort_date: date | None = None

    #: Citable text. Citation offsets index into THIS string.
    text: str = ""
    text_sha: str = ""

    trial: TrialMeta | None = None
    literature: LiteratureMeta | None = None
    drug_label: DrugLabelMeta | None = None

    #: Verbatim upstream payload, kept for audit and regeneration.
    payload: dict[str, Any] = Field(default_factory=dict, repr=False)

    @staticmethod
    def make_source_id(provider: str, native_id: str) -> str:
        return f"{provider}:{native_id}"

    @staticmethod
    def sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def entities(self) -> set[str]:
        """Named entities this record vouches for.

        The deterministic 'unsupported entity' gate in app/verify/ checks every
        drug and company named in generated prose against the union of these.
        """
        out: set[str] = set()
        if self.trial:
            out.add(self.trial.nct_id)
            if self.trial.lead_sponsor:
                out.add(self.trial.lead_sponsor)
            out.update(self.trial.collaborators)
            out.update(self.trial.interventions)
            out.update(self.trial.institutions)
        if self.drug_label:
            out.update(self.drug_label.brand_names)
            out.update(self.drug_label.generic_names)
            out.update(self.drug_label.manufacturers)
        return {e for e in out if e}


# --------------------------------------------------------------------------
# Corpus  (Stream A owns the store: app/corpus/)
# --------------------------------------------------------------------------


class FieldVelocity(_Base):
    """How fast a condition's evidence base turns over.

    Drives condition-adaptive recency decay: oncology discounts old evidence
    aggressively, stable cardiology does not. Derived from retrieved data, so
    it costs no extra API calls.
    """

    trials_per_year: float = 0.0
    publications_per_year: float = 0.0
    #: Recency half-life in months, derived from the above.
    half_life_months: float = 36.0


class CorpusSnapshot(_Base):
    """Immutable evidence set for one run.

    Snapshots are immutable so any briefing can be regenerated or audited
    against exactly the evidence it was built from, months later.
    """

    run_id: str
    query: str
    profile: ConditionProfile
    created_at: datetime
    records: list[SourceRecord] = Field(default_factory=list)
    velocity: FieldVelocity | None = None
    contract_version: str = CONTRACT_VERSION

    def by_id(self) -> dict[str, SourceRecord]:
        return {r.source_id: r for r in self.records}


# --------------------------------------------------------------------------
# Ranking  (Stream B owns: app/rank/)
# --------------------------------------------------------------------------


class ScoreTier(StrEnum):
    """Which signals were eligible for this record.

    Under ~18 months, citation metrics are IGNORED rather than scored as zero —
    a brand-new major-society guideline must rank on its authority, not lose to
    an older paper for want of citations it has not had time to accrue.
    """

    PROVENANCE_ONLY = "provenance_only"
    PROVENANCE_AND_IMPACT = "provenance_and_impact"


class RecordScore(_Base):
    source_id: str
    total: float
    credibility: float
    recency: float
    tier: ScoreTier
    #: Per-signal breakdown, so an analyst asking "why did this rank here"
    #: gets a real answer.
    components: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    #: Set when collapsed into another record as a co-publication.
    superseded_by: str | None = None


# --------------------------------------------------------------------------
# Briefing  (Stream B produces, Stream C renders)
# --------------------------------------------------------------------------


class Citation(_Base):
    """One citation returned by the Citations API, resolved to a source.

    ``start_char``/``end_char`` index into ``SourceRecord.text`` of the record
    identified by ``source_id``. The deterministic citation-validity gate
    re-slices the text at these offsets and compares against ``cited_text``.
    """

    source_id: str
    cited_text: str
    start_char: int
    end_char: int
    document_index: int | None = None
    text_sha: str = ""


class Verdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    SPAN_IRRELEVANT = "SPAN_IRRELEVANT"


class JudgeResult(_Base):
    verdict: Verdict
    reason: str
    confidence: float | None = None
    judge_model: str | None = None


class Claim(_Base):
    claim_id: str
    text: str
    citations: list[Citation] = Field(default_factory=list)
    judge: JudgeResult | None = None

    @property
    def is_sourced(self) -> bool:
        return bool(self.citations)


class SectionKind(StrEnum):
    #: Every claim carries a citation. This is sourced fact.
    EVIDENCE = "evidence"
    #: Labeled analysis. Traceable to evidence IDs, never presented as fact.
    IMPLICATIONS = "implications"


class EvidenceDensity(_Base):
    """Whether there is enough evidence to say anything responsibly.

    Below threshold the section says so plainly and the implications layer is
    suppressed rather than asked to speculate. "Three Phase I trials and no
    guidelines" is a useful answer; a fluent page implying more is known than
    is known is not.
    """

    record_count: int = 0
    guideline_count: int = 0
    trial_count: int = 0
    late_phase_trial_count: int = 0
    newest_year: int | None = None
    is_sparse: bool = False
    rationale: str | None = None


class BriefingSection(_Base):
    section_id: str
    heading: str
    kind: SectionKind
    claims: list[Claim] = Field(default_factory=list)
    density: EvidenceDensity | None = None
    #: Rendered when density is below threshold.
    sparse_notice: str | None = None


class Briefing(_Base):
    """The versioned contract between the pipeline and the renderer.

    app/render/ consumes this and nothing else, so UX can iterate freely
    against already-generated briefings without touching retrieval, ranking
    or verification — and without spending a cent on regeneration.
    """

    briefing_id: str
    run_id: str
    condition: str
    profile: ConditionProfile
    as_of: date
    sections: list[BriefingSection] = Field(default_factory=list)
    implications_suppressed: bool = False
    suppression_reason: str | None = None
    #: source_id -> minimal display info, so the renderer needs no corpus access.
    sources: dict[str, SourceRef] = Field(default_factory=dict)
    contract_version: str = CONTRACT_VERSION


class SourceRef(_Base):
    """Display-side view of a source. Keeps the renderer decoupled from corpus."""

    source_id: str
    source_type: SourceType
    title: str
    url: str
    provider: str
    sort_date: date | None = None
    detail: str | None = None


Briefing.model_rebuild()
