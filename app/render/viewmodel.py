"""Briefing + IntegrityReport -> typed view models.

All display logic lives here. The templates get objects with the attributes
they need already computed, so they contain loops and conditionals over
*presentation* state and no derivation of their own. That is deliberate: the
UX is expected to iterate hard, and logic that has leaked into Jinja is logic
that cannot be tested or type-checked.

Nothing in this module reads a corpus, a clock, a file or an environment
variable. ``build_view(briefing, options)`` is a pure function, which is what
makes a golden-HTML test meaningful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date

from app.models import (
    Briefing,
    BriefingSection,
    Citation,
    Claim,
    EvidenceDensity,
    SectionKind,
    SourceRef,
    SourceType,
    Verdict,
)
from app.render.integrity import (
    STOPPED_TRIAL_STATUSES,
    Defect,
    IntegrityReport,
    Severity,
    inspect,
)

_NON_SLUG = re.compile(r"[^a-zA-Z0-9]+")

SOURCE_TYPE_LABELS: dict[SourceType, str] = {
    SourceType.TRIAL: "Clinical trial",
    SourceType.LITERATURE: "Publication",
    SourceType.DRUG_LABEL: "FDA drug label",
    SourceType.GUIDELINE: "Guideline",
}

#: Plural headings for the sources appendix, in the order they are shown.
SOURCE_GROUP_ORDER: tuple[SourceType, ...] = (
    SourceType.GUIDELINE,
    SourceType.TRIAL,
    SourceType.LITERATURE,
    SourceType.DRUG_LABEL,
)

SOURCE_GROUP_LABELS: dict[SourceType, str] = {
    SourceType.TRIAL: "Clinical trials",
    SourceType.LITERATURE: "Publications",
    SourceType.DRUG_LABEL: "FDA drug labels",
    SourceType.GUIDELINE: "Guidelines",
}


def slug(value: str) -> str:
    """HTML-id-safe token. Source ids carry ``:`` and ``.``, which are legal in
    an id but awkward in a CSS selector and a URL fragment."""
    return _NON_SLUG.sub("-", value).strip("-").lower() or "x"


# --------------------------------------------------------------------------
# options
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """Everything the renderer needs from outside the Briefing.

    ``today`` is injected so the output is a deterministic function of its
    inputs; a renderer that reads the clock cannot be golden-tested and its
    staleness banner cannot be exercised.
    """

    today: date | None = None
    aging_after_days: int = 30
    #: Oncology goes stale fast. This is a renderer-side default because the
    #: Briefing contract carries no freshness budget — see NOTES-surface.md.
    stale_after_days: int = 90
    #: Inline the stylesheet so a single .html file is self-contained when it
    #: is emailed or dropped in a shared drive, which is how these travel.
    inline_css: bool = True
    #: When set, the briefing renders a Layer-3 usefulness-rating widget
    #: posting to this URL. Omitted entirely when None.
    feedback_endpoint: str | None = None

    def resolved_today(self) -> date:
        return self.today or date.today()


# --------------------------------------------------------------------------
# leaf views
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DefectView:
    code: str
    severity: str
    label: str
    detail: str
    claim_anchor: str | None

    @property
    def is_error(self) -> bool:
        return self.severity == Severity.ERROR.value


@dataclass(frozen=True, slots=True)
class CitationView:
    """One citation chip: the one-click path from a sentence to its source."""

    #: Short human handle — "NCT07285239", "PMID 41494138", "TECVAYLI label".
    label: str
    #: Primary-source URL. This is the one click. Empty when dangling.
    href: str
    #: The exact characters the model was allowed to rely on.
    cited_text: str
    source_id: str
    source_anchor: str
    source_title: str
    source_type_label: str
    start_char: int
    end_char: int
    text_sha: str
    detail: str | None
    #: Cited trial was TERMINATED/WITHDRAWN/SUSPENDED at snapshot time. The
    #: chip says so on its face: "terminated trial cited as promising" is a
    #: failure mode the surface can help catch even though only app/verify can
    #: prove the prose actually mis-describes it.
    is_stopped_trial: bool
    defects: tuple[DefectView, ...]

    @property
    def is_dangling(self) -> bool:
        return not self.href

    @property
    def is_defective(self) -> bool:
        return any(d.is_error for d in self.defects)

    @property
    def offsets(self) -> str:
        return f"{self.start_char}–{self.end_char}"


@dataclass(frozen=True, slots=True)
class SourceChip:
    """One chip in a claim's footer: one per distinct source, not per citation.

    A claim quoting two spans of the same trial record needs one link, not two
    identical ones. The spans are all still enumerated in the quotes
    disclosure, so nothing is hidden — only the duplicate link is dropped.
    """

    label: str
    href: str
    source_id: str
    source_anchor: str
    source_title: str
    source_type_label: str
    is_dangling: bool
    is_stopped_trial: bool
    span_count: int

    @property
    def status_note(self) -> str:
        return "unresolved" if self.is_dangling else ""


@dataclass(frozen=True, slots=True)
class VerdictView:
    """Per-claim verification state.

    ``state == "unjudged"`` is rendered as exactly that. It is never styled as
    a pass: the renderer has no basis for a green tick and must not imply one.
    """

    state: str
    label: str
    tone: str
    reason: str | None = None
    judge_model: str | None = None
    confidence: float | None = None

    @property
    def is_unjudged(self) -> bool:
        return self.state == "unjudged"


_VERDICT_VIEWS: dict[Verdict, VerdictView] = {
    Verdict.SUPPORTED: VerdictView("supported", "Judged supported", "ok"),
    Verdict.PARTIAL: VerdictView("partial", "Partially supported — analyst review", "warn"),
    Verdict.NOT_SUPPORTED: VerdictView("not_supported", "Not supported by cited span", "bad"),
    Verdict.SPAN_IRRELEVANT: VerdictView("span_irrelevant", "Cited span is irrelevant", "bad"),
}

UNJUDGED = VerdictView("unjudged", "Not independently judged", "neutral")


@dataclass(frozen=True, slots=True)
class ClaimView:
    claim_id: str
    anchor: str
    text: str
    #: "evidence" or "implications" — carried on the claim, not just the
    #: section, so the tier survives a copied fragment.
    tier: str
    citations: tuple[CitationView, ...]
    #: Deduplicated citation links, in first-cited order.
    chips: tuple[SourceChip, ...]
    verdict: VerdictView
    defects: tuple[DefectView, ...]

    @property
    def is_flagged(self) -> bool:
        """True if anything about this claim OR its citations is an error.

        A claim whose citation points nowhere is a defective claim, even though
        the finding is technically attached to the citation. It gets the full
        flagged treatment, because a reader scanning the page should not have
        to notice a small struck-through chip to know not to rely on it.
        """
        return any(d.is_error for d in self.defects) or any(c.is_defective for c in self.citations)

    @property
    def has_warnings(self) -> bool:
        return any(d.severity == Severity.WARNING.value for d in self.defects) or any(
            d.severity == Severity.WARNING.value for c in self.citations for d in c.defects
        )

    @property
    def is_evidence(self) -> bool:
        return self.tier == SectionKind.EVIDENCE.value

    @property
    def verdict_reason(self) -> str | None:
        """The judge's reasoning, unless a defect banner on this claim already
        carries it. PARTIAL and NOT_SUPPORTED become defects whose detail *is*
        the reason, and printing it twice makes the page look padded."""
        reason = self.verdict.reason
        if not reason or any(d.detail == reason for d in self.defects):
            return None
        return reason

    @property
    def show_verdict(self) -> bool:
        """Evidence claims always state their verification state, including
        "not judged". Implications claims have nothing to verify against, so
        a pill appears only if app/verify actually judged one."""
        return self.is_evidence or not self.verdict.is_unjudged


@dataclass(frozen=True, slots=True)
class DensityView:
    record_count: int
    guideline_count: int
    trial_count: int
    late_phase_trial_count: int
    newest_year: int | None
    is_sparse: bool
    rationale: str | None

    @property
    def stats(self) -> tuple[tuple[str, str], ...]:
        """(label, value) pairs for the density strip, in reading order."""
        return (
            ("records", str(self.record_count)),
            ("guidelines", str(self.guideline_count)),
            ("trials", str(self.trial_count)),
            ("late-phase", str(self.late_phase_trial_count)),
            ("newest", str(self.newest_year) if self.newest_year else "—"),
        )


@dataclass(frozen=True, slots=True)
class SectionView:
    section_id: str
    anchor: str
    heading: str
    kind: str
    claims: tuple[ClaimView, ...]
    density: DensityView | None
    sparse_notice: str | None
    defects: tuple[DefectView, ...]

    @property
    def is_evidence(self) -> bool:
        return self.kind == SectionKind.EVIDENCE.value

    @property
    def is_sparse(self) -> bool:
        return bool(self.density and self.density.is_sparse)

    @property
    def density_caption(self) -> str:
        """The same numbers mean different things either side of the tier line.

        On an evidence section they describe what the claims rest on. On an
        implications section they describe the evidence base the analysis read
        from — not support for the analysis itself, which has none.
        """
        if self.is_evidence:
            return "Evidence density"
        return "Evidence base this analysis reads from"

    @property
    def flagged_claim_count(self) -> int:
        return sum(1 for c in self.claims if c.is_flagged)

    @property
    def citation_count(self) -> int:
        return sum(len(c.citations) for c in self.claims)


@dataclass(frozen=True, slots=True)
class SourceView:
    source_id: str
    anchor: str
    label: str
    title: str
    url: str
    provider: str
    source_type: str
    source_type_label: str
    sort_date: date | None
    detail: str | None
    #: Anchors of the claims that cite this source, for the back-reference.
    cited_by: tuple[str, ...]

    @property
    def is_cited(self) -> bool:
        return bool(self.cited_by)

    @property
    def is_stopped_trial(self) -> bool:
        return (
            self.source_type == SourceType.TRIAL.value
            and (self.detail or "").strip().upper() in STOPPED_TRIAL_STATUSES
        )


@dataclass(frozen=True, slots=True)
class SourceGroup:
    label: str
    sources: tuple[SourceView, ...]


@dataclass(frozen=True, slots=True)
class FreshnessView:
    as_of: date
    age_days: int
    state: str
    label: str
    budget_days: int

    @property
    def as_of_long(self) -> str:
        return self.as_of.strftime("%d %B %Y").lstrip("0")

    @property
    def as_of_iso(self) -> str:
        return self.as_of.isoformat()


@dataclass(frozen=True, slots=True)
class IntegrityView:
    """Document-level integrity summary, plus the scope caveat.

    ``checks_not_performed`` is rendered next to the summary. A clean banner
    that does not say what it did not check is the kind of reassurance that
    gets a briefing trusted further than it has earned.
    """

    is_clean: bool
    error_count: int
    warning_count: int
    notice_count: int
    claim_count: int
    citation_count: int
    judged_claim_count: int
    unjudged_claim_count: int
    errors: tuple[DefectView, ...]
    warnings: tuple[DefectView, ...]
    notices: tuple[DefectView, ...]
    checks_performed: tuple[str, ...]
    checks_not_performed: tuple[str, ...]

    @property
    def flagged_claim_count(self) -> int:
        return len({d.claim_anchor for d in self.errors if d.claim_anchor})


@dataclass(frozen=True, slots=True)
class BriefingView:
    briefing_id: str
    run_id: str
    condition: str
    query: str
    freshness: FreshnessView
    sections: tuple[SectionView, ...]
    evidence_sections: tuple[SectionView, ...]
    implications_sections: tuple[SectionView, ...]
    implications_suppressed: bool
    suppression_reason: str | None
    source_groups: tuple[SourceGroup, ...]
    uncited_source_count: int
    integrity: IntegrityView
    contract_version: str
    unvalidated_synonyms: tuple[str, ...]
    feedback_endpoint: str | None

    @property
    def total_source_count(self) -> int:
        return sum(len(g.sources) for g in self.source_groups)


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------


def build_view(
    briefing: Briefing,
    options: RenderOptions | None = None,
    report: IntegrityReport | None = None,
) -> BriefingView:
    """Pure Briefing -> BriefingView. Runs ``inspect`` unless given a report."""
    options = options or RenderOptions()
    today = options.resolved_today()
    report = report or inspect(
        briefing,
        today=today,
        aging_after_days=options.aging_after_days,
        stale_after_days=options.stale_after_days,
    )

    claim_anchors = {
        claim.claim_id: _claim_anchor(claim.claim_id)
        for section in briefing.sections
        for claim in section.claims
    }
    sections = tuple(_build_section(s, briefing, report, claim_anchors) for s in briefing.sections)

    return BriefingView(
        briefing_id=briefing.briefing_id,
        run_id=briefing.run_id,
        condition=briefing.condition,
        query=briefing.profile.query,
        freshness=_build_freshness(briefing.as_of, today, options),
        sections=sections,
        evidence_sections=tuple(s for s in sections if s.is_evidence),
        implications_sections=tuple(s for s in sections if not s.is_evidence),
        implications_suppressed=briefing.implications_suppressed,
        suppression_reason=briefing.suppression_reason,
        source_groups=_build_source_groups(briefing, claim_anchors),
        uncited_source_count=_uncited_source_count(briefing),
        integrity=_build_integrity(report, claim_anchors),
        contract_version=briefing.contract_version,
        unvalidated_synonyms=tuple(briefing.profile.unvalidated_synonyms),
        feedback_endpoint=options.feedback_endpoint,
    )


def _build_freshness(as_of: date, today: date, options: RenderOptions) -> FreshnessView:
    age = (today - as_of).days
    if age >= options.stale_after_days:
        state, label = "stale", f"{age} days old — re-run before relying on this"
    elif age >= options.aging_after_days:
        state, label = "aging", f"{age} days old"
    elif age <= 0:
        state, label = "current", "captured today"
    elif age == 1:
        state, label = "current", "1 day old"
    else:
        state, label = "current", f"{age} days old"
    return FreshnessView(
        as_of=as_of,
        age_days=age,
        state=state,
        label=label,
        budget_days=options.stale_after_days,
    )


def _build_section(
    section: BriefingSection,
    briefing: Briefing,
    report: IntegrityReport,
    claim_anchors: dict[str, str],
) -> SectionView:
    return SectionView(
        section_id=section.section_id,
        anchor=f"section-{slug(section.section_id)}",
        heading=section.heading,
        kind=section.kind.value,
        claims=tuple(
            _build_claim(c, section, briefing, report, claim_anchors) for c in section.claims
        ),
        density=_build_density(section.density),
        sparse_notice=section.sparse_notice,
        defects=_defect_views(report.for_section(section.section_id), claim_anchors),
    )


def _build_density(density: EvidenceDensity | None) -> DensityView | None:
    if density is None:
        return None
    return DensityView(
        record_count=density.record_count,
        guideline_count=density.guideline_count,
        trial_count=density.trial_count,
        late_phase_trial_count=density.late_phase_trial_count,
        newest_year=density.newest_year,
        is_sparse=density.is_sparse,
        rationale=density.rationale,
    )


def _build_claim(
    claim: Claim,
    section: BriefingSection,
    briefing: Briefing,
    report: IntegrityReport,
    claim_anchors: dict[str, str],
) -> ClaimView:
    claim_defects = report.for_claim(claim.claim_id)
    # Citation-scoped findings are rendered on the chip, not repeated on the
    # claim; the claim keeps only findings about the claim as a whole.
    own = tuple(d for d in claim_defects if d.citation_index is None)
    citations = tuple(
        _build_citation(c, i, briefing, report, claim.claim_id, claim_anchors)
        for i, c in enumerate(claim.citations)
    )
    return ClaimView(
        claim_id=claim.claim_id,
        anchor=claim_anchors[claim.claim_id],
        text=claim.text,
        tier=section.kind.value,
        citations=citations,
        chips=_build_chips(citations),
        verdict=_build_verdict(claim),
        defects=_defect_views(own, claim_anchors),
    )


def _build_chips(citations: tuple[CitationView, ...]) -> tuple[SourceChip, ...]:
    """Collapse citations to one chip per source, preserving first-cited order."""
    chips: dict[str, SourceChip] = {}
    for cite in citations:
        existing = chips.get(cite.source_id)
        if existing is not None:
            chips[cite.source_id] = replace(existing, span_count=existing.span_count + 1)
            continue
        chips[cite.source_id] = SourceChip(
            label=cite.label,
            href=cite.href,
            source_id=cite.source_id,
            source_anchor=cite.source_anchor,
            source_title=cite.source_title,
            source_type_label=cite.source_type_label,
            is_dangling=cite.is_dangling,
            is_stopped_trial=cite.is_stopped_trial,
            span_count=1,
        )
    return tuple(chips.values())


def _build_verdict(claim: Claim) -> VerdictView:
    if claim.judge is None:
        return UNJUDGED
    base = _VERDICT_VIEWS.get(claim.judge.verdict, UNJUDGED)
    return replace(
        base,
        reason=claim.judge.reason or None,
        judge_model=claim.judge.judge_model,
        confidence=claim.judge.confidence,
    )


def _build_citation(
    cite: Citation,
    index: int,
    briefing: Briefing,
    report: IntegrityReport,
    claim_id: str,
    claim_anchors: dict[str, str],
) -> CitationView:
    ref = briefing.sources.get(cite.source_id)
    return CitationView(
        label=_source_label(cite.source_id, ref),
        href=ref.url if ref else "",
        cited_text=cite.cited_text,
        source_id=cite.source_id,
        source_anchor=f"src-{slug(cite.source_id)}",
        source_title=ref.title if ref else "Unresolved source",
        source_type_label=SOURCE_TYPE_LABELS.get(ref.source_type, "Source")
        if ref
        else "Unresolved",
        start_char=cite.start_char,
        end_char=cite.end_char,
        text_sha=cite.text_sha,
        detail=ref.detail if ref else None,
        is_stopped_trial=bool(
            ref
            and ref.source_type is SourceType.TRIAL
            and (ref.detail or "").strip().upper() in STOPPED_TRIAL_STATUSES
        ),
        defects=_defect_views(report.for_citation(claim_id, index), claim_anchors),
    )


def _source_label(source_id: str, ref: SourceRef | None) -> str:
    """Short handle a reader recognises: an NCT id, a PMID, a brand name."""
    native = source_id.split(":", 1)[-1] if ":" in source_id else source_id
    if ref is None:
        return native
    match ref.source_type:
        case SourceType.TRIAL:
            return native
        case SourceType.LITERATURE:
            return native if native.upper().startswith("PMID") else f"PMID {native}"
        case SourceType.DRUG_LABEL:
            brand = re.split(r"\s+[—-]\s+", ref.title.strip(), maxsplit=1)[0]
            return f"{brand} label" if brand else "FDA label"
        case _:
            return ref.provider or native


def _build_source_groups(
    briefing: Briefing, claim_anchors: dict[str, str]
) -> tuple[SourceGroup, ...]:
    back_refs: dict[str, list[str]] = {}
    for section in briefing.sections:
        for claim in section.claims:
            for cite in claim.citations:
                anchors = back_refs.setdefault(cite.source_id, [])
                anchor = claim_anchors[claim.claim_id]
                if anchor not in anchors:
                    anchors.append(anchor)

    views: dict[SourceType, list[SourceView]] = {}
    for source_id, ref in briefing.sources.items():
        views.setdefault(ref.source_type, []).append(
            SourceView(
                source_id=source_id,
                anchor=f"src-{slug(source_id)}",
                label=_source_label(source_id, ref),
                title=ref.title,
                url=ref.url,
                provider=ref.provider,
                source_type=ref.source_type.value,
                source_type_label=SOURCE_TYPE_LABELS.get(ref.source_type, "Source"),
                sort_date=ref.sort_date,
                detail=ref.detail,
                cited_by=tuple(back_refs.get(source_id, ())),
            )
        )

    groups: list[SourceGroup] = []
    for kind in SOURCE_GROUP_ORDER:
        bucket = views.get(kind)
        if not bucket:
            continue
        # Cited sources first, then newest first: what supported the prose is
        # what a reader is checking.
        bucket.sort(key=lambda s: (not s.is_cited, -(s.sort_date or date.min).toordinal()))
        groups.append(SourceGroup(label=SOURCE_GROUP_LABELS[kind], sources=tuple(bucket)))
    return tuple(groups)


def _uncited_source_count(briefing: Briefing) -> int:
    cited = {c.source_id for s in briefing.sections for cl in s.claims for c in cl.citations}
    return sum(1 for sid in briefing.sources if sid not in cited)


def _build_integrity(report: IntegrityReport, claim_anchors: dict[str, str]) -> IntegrityView:
    return IntegrityView(
        is_clean=report.is_clean,
        error_count=len(report.errors),
        warning_count=len(report.warnings),
        notice_count=len(report.notices),
        claim_count=report.claim_count,
        citation_count=report.citation_count,
        judged_claim_count=report.judged_claim_count,
        unjudged_claim_count=report.unjudged_claim_count,
        errors=_defect_views(report.errors, claim_anchors),
        warnings=_defect_views(report.warnings, claim_anchors),
        notices=_defect_views(report.notices, claim_anchors),
        checks_performed=report.checks_performed,
        checks_not_performed=report.checks_not_performed,
    )


def _defect_views(
    defects: tuple[Defect, ...], claim_anchors: dict[str, str]
) -> tuple[DefectView, ...]:
    return tuple(
        DefectView(
            code=d.code.value,
            severity=d.severity.value,
            label=d.label,
            detail=d.detail,
            claim_anchor=claim_anchors.get(d.claim_id) if d.claim_id else None,
        )
        for d in defects
    )


def _claim_anchor(claim_id: str) -> str:
    return f"claim-{slug(claim_id)}"
