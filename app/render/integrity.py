"""Structural integrity checks over a Briefing. Pure function, no corpus, no I/O.

Scope, stated precisely, because overclaiming here is the failure mode that
matters: these checks see the ``Briefing`` document and nothing else. They catch
defects that are *intrinsic to the document* — a citation pointing at a source
the briefing cannot resolve, analysis smuggled in wearing a citation, a span
whose arithmetic does not add up.

They CANNOT catch defects that need the source text or the corpus:

* whether ``record.text[start:end]`` actually equals ``cited_text`` — a
  length-preserving offset shift is invisible from here;
* whether a drug or company named in prose appears in any retrieved record;
* whether the phase and status in prose match the registry at snapshot time.

Those are :mod:`app.verify`'s deterministic gates (Layer 1), and their outcome
reaches the renderer only through ``Claim.judge``. So the report carries both
what was checked and what was *not*, and the template prints the latter. A
reader who sees a clean integrity banner must not conclude the briefing was
verified — only that it is internally well formed.

The corresponding rule for the renderer: a claim with ``judge is None`` is
rendered as *unjudged*, never as *passing*. Absence of evidence of a defect is
not a green tick.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from app.models import Briefing, BriefingSection, Claim, SectionKind, SourceType, Verdict

#: Trial statuses that mean the study is not running. Mirrors
#: ``TrialMeta.is_terminated``; re-stated here because the renderer sees only
#: ``SourceRef.detail``, a free-text display field, not ``TrialMeta``.
STOPPED_TRIAL_STATUSES = frozenset({"TERMINATED", "WITHDRAWN", "SUSPENDED", "NO_LONGER_AVAILABLE"})


class Severity(StrEnum):
    """How loudly the surface should complain.

    ERROR means the claim is not safe to rely on as written. WARNING means a
    human should look. NOTICE is context a reader needs but is not a defect —
    a sparse section correctly reporting its own sparseness is working, not
    broken, and must not be styled as a failure.
    """

    ERROR = "error"
    WARNING = "warning"
    NOTICE = "notice"


class DefectCode(StrEnum):
    # --- document-intrinsic defects -------------------------------------
    DANGLING_CITATION = "dangling_citation"
    TIER_VIOLATION = "tier_violation"
    UNCITED_EVIDENCE_CLAIM = "uncited_evidence_claim"
    MALFORMED_SPAN = "malformed_span"
    EMPTY_CITED_TEXT = "empty_cited_text"
    MISSING_TEXT_SHA = "missing_text_sha"
    EMPTY_CLAIM_TEXT = "empty_claim_text"
    # --- verdicts handed to us by app/verify ----------------------------
    JUDGE_NOT_SUPPORTED = "judge_not_supported"
    JUDGE_SPAN_IRRELEVANT = "judge_span_irrelevant"
    JUDGE_PARTIAL = "judge_partial"
    # --- document-level ---------------------------------------------------
    STALE_AS_OF = "stale_as_of"
    AGING_AS_OF = "aging_as_of"
    SUPPRESSION_UNEXPLAINED = "suppression_unexplained"
    # --- context, not defects --------------------------------------------
    CITED_TRIAL_NOT_ACTIVE = "cited_trial_not_active"
    SPARSE_SECTION = "sparse_section"


#: Human-readable rendering of each code. Kept next to the code so the template
#: needs no conditional prose of its own.
DEFECT_LABELS: dict[DefectCode, str] = {
    DefectCode.DANGLING_CITATION: "Citation points to a source this briefing cannot resolve",
    DefectCode.TIER_VIOLATION: "Analysis carries a citation — implications must not be "
    "presented as sourced fact",
    DefectCode.UNCITED_EVIDENCE_CLAIM: "Evidence claim carries no citation",
    DefectCode.MALFORMED_SPAN: "Citation offsets are inconsistent with the quoted text",
    DefectCode.EMPTY_CITED_TEXT: "Citation quotes nothing",
    DefectCode.MISSING_TEXT_SHA: "Citation has no text checksum, so it cannot be re-audited",
    DefectCode.EMPTY_CLAIM_TEXT: "Claim has no text",
    DefectCode.JUDGE_NOT_SUPPORTED: "Judged NOT SUPPORTED by the cited span",
    DefectCode.JUDGE_SPAN_IRRELEVANT: "Judged: the cited span is irrelevant to the claim",
    DefectCode.JUDGE_PARTIAL: "Judged only PARTIALLY supported — needs analyst review",
    DefectCode.STALE_AS_OF: "Briefing is stale",
    DefectCode.AGING_AS_OF: "Briefing is aging",
    DefectCode.SUPPRESSION_UNEXPLAINED: "Implications were suppressed without a stated reason",
    DefectCode.CITED_TRIAL_NOT_ACTIVE: "Cited trial is not actively running",
    DefectCode.SPARSE_SECTION: "Thin evidence base for this section",
}

_VERDICT_DEFECTS: dict[Verdict, tuple[DefectCode, Severity]] = {
    Verdict.NOT_SUPPORTED: (DefectCode.JUDGE_NOT_SUPPORTED, Severity.ERROR),
    Verdict.SPAN_IRRELEVANT: (DefectCode.JUDGE_SPAN_IRRELEVANT, Severity.ERROR),
    Verdict.PARTIAL: (DefectCode.JUDGE_PARTIAL, Severity.WARNING),
}


@dataclass(frozen=True, slots=True)
class Defect:
    """One finding, anchored to wherever it can be pointed at in the document."""

    code: DefectCode
    severity: Severity
    detail: str
    section_id: str | None = None
    claim_id: str | None = None
    #: Index into ``claim.citations``, when the finding is about one citation.
    citation_index: int | None = None

    @property
    def label(self) -> str:
        return DEFECT_LABELS[self.code]

    @property
    def is_blocking(self) -> bool:
        return self.severity is Severity.ERROR


#: What a Briefing-only pass can and cannot establish. Rendered verbatim, so a
#: reader can see the boundary rather than infer it.
CHECKS_PERFORMED: tuple[str, ...] = (
    "every citation resolves to a source listed in this briefing",
    "citation offsets are internally consistent with the quoted text",
    "every evidence claim carries at least one citation",
    "no implications claim carries a citation",
    "every citation records a source checksum",
    "the as-of date is within the freshness budget",
)

CHECKS_NOT_PERFORMED: tuple[str, ...] = (
    "that each quoted span matches the source text at those offsets",
    "that every drug and company named in prose appears in a retrieved record",
    "that trial phase and status in prose match the registry at snapshot time",
)


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """Everything the surface knows about this briefing's structural soundness."""

    defects: tuple[Defect, ...] = ()
    claim_count: int = 0
    citation_count: int = 0
    judged_claim_count: int = 0
    checks_performed: tuple[str, ...] = CHECKS_PERFORMED
    checks_not_performed: tuple[str, ...] = CHECKS_NOT_PERFORMED

    # -- aggregate views the template asks for ---------------------------

    @property
    def errors(self) -> tuple[Defect, ...]:
        return tuple(d for d in self.defects if d.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Defect, ...]:
        return tuple(d for d in self.defects if d.severity is Severity.WARNING)

    @property
    def notices(self) -> tuple[Defect, ...]:
        return tuple(d for d in self.defects if d.severity is Severity.NOTICE)

    @property
    def is_clean(self) -> bool:
        """No errors and no warnings. Notices are context, not defects."""
        return not self.errors and not self.warnings

    @property
    def flagged_claim_ids(self) -> frozenset[str]:
        return frozenset(d.claim_id for d in self.defects if d.claim_id and d.is_blocking)

    @property
    def unjudged_claim_count(self) -> int:
        return self.claim_count - self.judged_claim_count

    def for_claim(self, claim_id: str) -> tuple[Defect, ...]:
        return tuple(d for d in self.defects if d.claim_id == claim_id)

    def for_citation(self, claim_id: str, index: int) -> tuple[Defect, ...]:
        return tuple(
            d for d in self.defects if d.claim_id == claim_id and d.citation_index == index
        )

    def for_section(self, section_id: str) -> tuple[Defect, ...]:
        """Findings attached to the section itself, not to a claim inside it."""
        return tuple(d for d in self.defects if d.section_id == section_id and d.claim_id is None)


def inspect(
    briefing: Briefing,
    *,
    today: date | None = None,
    aging_after_days: int = 30,
    stale_after_days: int = 90,
) -> IntegrityReport:
    """Check a briefing against itself.

    ``today`` is injected rather than read from the clock so rendering is a
    deterministic function of its inputs and the staleness banner is testable.
    """
    today = today or date.today()
    defects: list[Defect] = []
    claim_count = citation_count = judged = 0

    for section in briefing.sections:
        defects.extend(_inspect_section(section, briefing))
        for claim in section.claims:
            claim_count += 1
            citation_count += len(claim.citations)
            if claim.judge is not None:
                judged += 1
            defects.extend(_inspect_claim(claim, section, briefing))

    defects.extend(_inspect_document(briefing, today, aging_after_days, stale_after_days))

    return IntegrityReport(
        defects=tuple(defects),
        claim_count=claim_count,
        citation_count=citation_count,
        judged_claim_count=judged,
    )


# --------------------------------------------------------------------------
# section / claim / citation level
# --------------------------------------------------------------------------


def _inspect_section(section: BriefingSection, briefing: Briefing) -> Iterable[Defect]:
    density = section.density
    if density is not None and density.is_sparse:
        yield Defect(
            code=DefectCode.SPARSE_SECTION,
            severity=Severity.NOTICE,
            detail=section.sparse_notice
            or density.rationale
            or "Evidence base for this section is below the density threshold.",
            section_id=section.section_id,
        )


def _inspect_claim(claim: Claim, section: BriefingSection, briefing: Briefing) -> Iterable[Defect]:
    here = {"section_id": section.section_id, "claim_id": claim.claim_id}

    if not claim.text.strip():
        yield Defect(DefectCode.EMPTY_CLAIM_TEXT, Severity.ERROR, "Claim text is empty.", **here)

    # The two-tier contract, enforced in both directions.
    if section.kind is SectionKind.IMPLICATIONS and claim.citations:
        yield Defect(
            code=DefectCode.TIER_VIOLATION,
            severity=Severity.ERROR,
            detail=(
                f"This claim sits in an implications section but carries "
                f"{len(claim.citations)} citation(s). Strategic analysis dressed as sourced "
                f"fact is the exact confusion this briefing format exists to prevent."
            ),
            **here,
        )
    if section.kind is SectionKind.EVIDENCE and not claim.citations:
        yield Defect(
            code=DefectCode.UNCITED_EVIDENCE_CLAIM,
            severity=Severity.ERROR,
            detail="An evidence claim with no citation cannot be traced to a primary source.",
            **here,
        )

    if claim.judge is not None:
        mapped = _VERDICT_DEFECTS.get(claim.judge.verdict)
        if mapped is not None:
            code, severity = mapped
            yield Defect(code=code, severity=severity, detail=claim.judge.reason, **here)

    for i, cite in enumerate(claim.citations):
        yield from _inspect_citation(cite, i, briefing, here)


def _inspect_citation(cite, index: int, briefing: Briefing, here: dict) -> Iterable[Defect]:
    at = {**here, "citation_index": index}
    ref = briefing.sources.get(cite.source_id)

    if ref is None:
        yield Defect(
            code=DefectCode.DANGLING_CITATION,
            severity=Severity.ERROR,
            detail=(
                f"No source '{cite.source_id}' is listed in this briefing, so the quoted "
                f"text cannot be traced to anything. A reader has no way to check it."
            ),
            **at,
        )
    elif ref.source_type is SourceType.TRIAL and _is_stopped(ref.detail):
        yield Defect(
            code=DefectCode.CITED_TRIAL_NOT_ACTIVE,
            severity=Severity.NOTICE,
            detail=(
                f"{cite.source_id} was {(ref.detail or '').strip().upper()} at snapshot time. "
                f"Check that the surrounding prose does not describe it as ongoing."
            ),
            **at,
        )

    if not cite.cited_text:
        yield Defect(
            DefectCode.EMPTY_CITED_TEXT, Severity.ERROR, "Citation quotes an empty span.", **at
        )
    else:
        span = cite.end_char - cite.start_char
        if cite.start_char < 0 or span <= 0 or span != len(cite.cited_text):
            yield Defect(
                code=DefectCode.MALFORMED_SPAN,
                severity=Severity.ERROR,
                detail=(
                    f"Offsets {cite.start_char}-{cite.end_char} describe a {span}-character "
                    f"span, but the quoted text is {len(cite.cited_text)} characters."
                ),
                **at,
            )

    if not cite.text_sha:
        yield Defect(
            code=DefectCode.MISSING_TEXT_SHA,
            severity=Severity.WARNING,
            detail=(
                "Without a source checksum this citation cannot be re-validated against a "
                "later snapshot."
            ),
            **at,
        )


def _is_stopped(detail: str | None) -> bool:
    return (detail or "").strip().upper() in STOPPED_TRIAL_STATUSES


# --------------------------------------------------------------------------
# document level
# --------------------------------------------------------------------------


def _inspect_document(
    briefing: Briefing, today: date, aging_after_days: int, stale_after_days: int
) -> Iterable[Defect]:
    age = (today - briefing.as_of).days
    if age >= stale_after_days:
        yield Defect(
            code=DefectCode.STALE_AS_OF,
            severity=Severity.WARNING,
            detail=(
                f"Evidence was frozen {age} days ago, past the {stale_after_days}-day "
                f"freshness budget. Re-run before relying on this."
            ),
        )
    elif age >= aging_after_days:
        yield Defect(
            code=DefectCode.AGING_AS_OF,
            severity=Severity.NOTICE,
            detail=f"Evidence was frozen {age} days ago.",
        )

    if briefing.implications_suppressed and not (briefing.suppression_reason or "").strip():
        yield Defect(
            code=DefectCode.SUPPRESSION_UNEXPLAINED,
            severity=Severity.WARNING,
            detail="Implications were suppressed but no reason was recorded.",
        )


__all__ = [
    "CHECKS_NOT_PERFORMED",
    "CHECKS_PERFORMED",
    "DEFECT_LABELS",
    "STOPPED_TRIAL_STATUSES",
    "Defect",
    "DefectCode",
    "IntegrityReport",
    "Severity",
    "inspect",
]
