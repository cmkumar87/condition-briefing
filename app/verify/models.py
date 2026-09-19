"""Result types for verification. Private to app/verify/.

These are deliberately not in the frozen contract: what a gate reports is this
stream's business, while what a *briefing* contains is everyone's. Stream C
renders findings by claim_id, which is the only coupling.
"""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Gate(StrEnum):
    """Which check produced a finding."""

    #: Citation resolves to a real record and its offsets slice back to cited_text.
    CITATION_INTEGRITY = "citation_integrity"
    #: Every NCT ID written in prose exists in the corpus.
    TRIAL_REFERENCE = "trial_reference"
    #: Every drug, brand and company named in prose appears in the corpus.
    ENTITY_SUPPORT = "entity_support"
    #: Phase and recruitment status in prose match CT.gov at snapshot time.
    TRIAL_STATE = "trial_state"
    #: Evidence claims are cited; implications claims are not.
    TIER_SEPARATION = "tier_separation"


class Severity(StrEnum):
    #: Blocking. The briefing does not ship with one of these outstanding.
    ERROR = "error"
    #: Surfaced to the analyst, does not block.
    WARNING = "warning"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: Gate
    severity: Severity
    message: str
    section_id: str | None = None
    claim_id: str | None = None
    source_id: str | None = None
    #: Machine-readable specifics, e.g. the offsets that failed to slice.
    detail: dict[str, str | int | None] = Field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - reporting convenience
        where = self.claim_id or self.section_id or "-"
        return f"[{self.severity}] {self.gate} {where}: {self.message}"


class VerificationReport(BaseModel):
    """Everything Layer 1 found, and the counts that prove it looked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    briefing_id: str
    findings: list[Finding] = Field(default_factory=list)
    claims_checked: int = 0
    citations_checked: int = 0
    entities_checked: int = 0

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def passed(self) -> bool:
        """A briefing ships only when no gate raised an error."""
        return not self.errors

    def gates_failed(self) -> set[Gate]:
        return {f.gate for f in self.errors}

    def by_claim(self) -> dict[str, list[Finding]]:
        """Findings keyed by claim, for the renderer's per-claim flag."""
        out: dict[str, list[Finding]] = defaultdict(list)
        for f in self.findings:
            if f.claim_id:
                out[f.claim_id].append(f)
        return dict(out)

    def summary(self) -> str:
        if self.passed:
            return (
                f"PASS — {self.claims_checked} claims, {self.citations_checked} citations, "
                f"{self.entities_checked} named entities verified"
            )
        gates = ", ".join(sorted(g.value for g in self.gates_failed()))
        return f"FAIL — {len(self.errors)} error(s) across gates: {gates}"
