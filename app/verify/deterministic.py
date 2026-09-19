"""Layer 1: deterministic verification. No model, no network, must pass.

These gates are the reason the anti-fabrication claim is structural rather than
aspirational. They do not ask a model whether a briefing looks right; they
re-derive every checkable assertion from the frozen corpus snapshot and compare.
A briefing that fails any of them does not ship.

The five gates
--------------
1. **Citation integrity** — every citation resolves to a record in the snapshot,
   ``record.text[start:end]`` equals ``cited_text`` exactly, and ``text_sha``
   matches the record the offsets were taken against. This is what makes an
   offset a fact rather than a decoration.
2. **Trial reference** — every NCT ID written in prose exists in the corpus.
3. **Entity support** — every drug, brand and company named in prose appears in
   the corpus. This is the invented-company gate.
4. **Trial state** — phase and recruitment status in prose match CT.gov at
   snapshot time, and a terminated trial is never described as if it were
   running. This is what catches "terminated trial described as promising",
   the error that misdirects capital most directly.
5. **Tier separation** — evidence claims carry citations, implications claims do
   not. Analysis presented as sourced fact is the failure the two-tier contract
   exists to prevent.

``tests/fixtures/briefing__broken.json`` seeds one defect per gate. The suite
asserts these gates fail on it and pass on the good briefing; a verifier that
passes both is not a verifier.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.models import Briefing, Claim, SectionKind, SourceRecord, SourceType
from app.verify.entities import CorpusLexicon, extract_named_entities, extract_nct_ids
from app.verify.models import Finding, Gate, Severity, VerificationReport

_PHASE = re.compile(
    r"\bphase\s+(0|iv|i{1,3}|[1-4])(?:\s*[/–-]\s*(iv|i{1,3}|[1-4]))?\b",
    re.IGNORECASE,
)
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}

#: Prose that asserts something about a trial's recruitment state, mapped to the
#: CT.gov statuses that would make the assertion true.
_STATUS_CLAIMS: tuple[tuple[re.Pattern[str], frozenset[str], str], ...] = (
    (
        re.compile(
            r"\b(recruit\w*|enroll\w*|accru\w*|open to (?:new )?(?:patients|participants))\b",
            re.I,
        ),
        frozenset({"RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING"}),
        "open to enrolment",
    ),
    (
        re.compile(
            r"\b(completed|has reported|reported (?:its )?results|read out|readout)\b", re.I
        ),
        frozenset({"COMPLETED"}),
        "completed",
    ),
    (
        re.compile(r"\b(ongoing|under ?way|in progress|currently active)\b", re.I),
        frozenset({"RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION"}),
        "ongoing",
    ),
)

#: Vocabulary that discloses a trial is no longer running. A claim citing a
#: terminated trial must contain one of these.
_STOPPED = re.compile(
    r"\b(terminat\w*|withdraw\w*|suspend\w*|halt\w*|discontinu\w*|stopped|closed early)\b",
    re.IGNORECASE,
)


def extract_phases(text: str) -> set[str]:
    """Phases a claim asserts, normalized to CT.gov spelling.

    "Phase 3", "Phase III" and "phase II/III" all land on the PHASE<n> strings
    that ``TrialMeta.phases`` uses.
    """
    out: set[str] = set()
    for first, second in _PHASE.findall(text or ""):
        for token in (first, second):
            if not token:
                continue
            digit = _ROMAN.get(token.lower(), token)
            if digit.isdigit():
                out.add("EARLY_PHASE1" if digit == "0" else f"PHASE{digit}")
    return out


def _cited_records(claim: Claim, by_id: dict[str, SourceRecord]) -> list[SourceRecord]:
    seen: dict[str, SourceRecord] = {}
    for citation in claim.citations:
        record = by_id.get(citation.source_id)
        if record is not None:
            seen.setdefault(record.source_id, record)
    return list(seen.values())


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def _gate_citation_integrity(
    section_id: str, claim: Claim, by_id: dict[str, SourceRecord]
) -> list[Finding]:
    findings: list[Finding] = []
    for citation in claim.citations:
        where = {"section_id": section_id, "claim_id": claim.claim_id}
        record = by_id.get(citation.source_id)
        if record is None:
            findings.append(
                Finding(
                    gate=Gate.CITATION_INTEGRITY,
                    severity=Severity.ERROR,
                    message=(
                        f"citation points at {citation.source_id}, which is not in the "
                        "corpus snapshot"
                    ),
                    source_id=citation.source_id,
                    **where,
                )
            )
            continue

        sliced = record.text[citation.start_char : citation.end_char]
        if sliced != citation.cited_text:
            findings.append(
                Finding(
                    gate=Gate.CITATION_INTEGRITY,
                    severity=Severity.ERROR,
                    message=(
                        f"offsets [{citation.start_char}:{citation.end_char}] slice "
                        f"{sliced!r}, not the quoted {citation.cited_text!r}"
                    ),
                    source_id=citation.source_id,
                    detail={
                        "start_char": citation.start_char,
                        "end_char": citation.end_char,
                        "sliced": sliced[:120],
                        "cited_text": citation.cited_text[:120],
                    },
                    **where,
                )
            )
        if citation.text_sha and citation.text_sha != record.text_sha:
            findings.append(
                Finding(
                    gate=Gate.CITATION_INTEGRITY,
                    severity=Severity.ERROR,
                    message=(
                        "citation was taken against a different version of the record "
                        f"text (sha {citation.text_sha} vs {record.text_sha}); its "
                        "offsets cannot be trusted"
                    ),
                    source_id=citation.source_id,
                    **where,
                )
            )
    return findings


def _gate_trial_reference(section_id: str, claim: Claim, lexicon: CorpusLexicon) -> list[Finding]:
    return [
        Finding(
            gate=Gate.TRIAL_REFERENCE,
            severity=Severity.ERROR,
            message=f"prose cites {nct}, which is not in the corpus snapshot",
            section_id=section_id,
            claim_id=claim.claim_id,
            detail={"nct_id": nct},
        )
        for nct in sorted(extract_nct_ids(claim.text) - lexicon.nct_ids)
    ]


def _gate_entity_support(
    section_id: str, claim: Claim, lexicon: CorpusLexicon
) -> tuple[list[Finding], int]:
    names = extract_named_entities(claim.text)
    findings = [
        Finding(
            gate=Gate.ENTITY_SUPPORT,
            severity=Severity.ERROR,
            message=(
                f"{name!r} is named in prose but appears in no retrieved record; "
                "nothing in the corpus vouches for it"
            ),
            section_id=section_id,
            claim_id=claim.claim_id,
            detail={"entity": name},
        )
        for name in lexicon.unsupported(names)
    ]
    return findings, len(names)


def _gate_trial_state(
    section_id: str, claim: Claim, by_id: dict[str, SourceRecord]
) -> list[Finding]:
    findings: list[Finding] = []
    where = {"section_id": section_id, "claim_id": claim.claim_id}
    trials = [r for r in _cited_records(claim, by_id) if r.trial]
    if not trials:
        return findings

    claimed_phases = extract_phases(claim.text)
    available = {p for r in trials for p in (r.trial.phases if r.trial else [])}
    for phase in sorted(claimed_phases - available):
        findings.append(
            Finding(
                gate=Gate.TRIAL_STATE,
                severity=Severity.ERROR,
                message=(
                    f"prose says {phase}, but the trial(s) it cites are registered as "
                    f"{sorted(available) or ['no phase']}"
                ),
                source_id=trials[0].source_id,
                detail={"claimed": phase, "registered": ", ".join(sorted(available))},
                **where,
            )
        )

    statuses = {(r.trial.overall_status or "").upper() for r in trials if r.trial}
    for pattern, allowed, described in _STATUS_CLAIMS:
        if pattern.search(claim.text) and not (statuses & allowed):
            findings.append(
                Finding(
                    gate=Gate.TRIAL_STATE,
                    severity=Severity.ERROR,
                    message=(
                        f"prose describes the trial as {described}, but CT.gov had it "
                        f"as {sorted(statuses)} at snapshot time"
                    ),
                    source_id=trials[0].source_id,
                    detail={"described": described, "registered": ", ".join(sorted(statuses))},
                    **where,
                )
            )

    # The expensive one: a trial that stopped, written up as if it were running.
    stopped = [r for r in trials if r.trial and r.trial.is_terminated]
    if stopped and not _STOPPED.search(claim.text):
        record = stopped[0]
        status = (record.trial.overall_status or "").lower() if record.trial else "stopped"
        findings.append(
            Finding(
                gate=Gate.TRIAL_STATE,
                severity=Severity.ERROR,
                message=(
                    f"claim rests on {record.source_id}, which is {status}, without "
                    "saying so; a stopped trial must never read as a live one"
                ),
                source_id=record.source_id,
                detail={"registered": status},
                **where,
            )
        )
    return findings


def _gate_tier_separation(section_id: str, kind: SectionKind, claim: Claim) -> list[Finding]:
    where = {"section_id": section_id, "claim_id": claim.claim_id}
    if kind is SectionKind.EVIDENCE and not claim.citations:
        return [
            Finding(
                gate=Gate.TIER_SEPARATION,
                severity=Severity.ERROR,
                message="evidence claim carries no citation",
                **where,
            )
        ]
    if kind is SectionKind.IMPLICATIONS and claim.citations:
        return [
            Finding(
                gate=Gate.TIER_SEPARATION,
                severity=Severity.ERROR,
                message=(
                    "implications claim carries a citation; labeled analysis must never "
                    "be presented as sourced fact"
                ),
                source_id=claim.citations[0].source_id,
                **where,
            )
        ]
    return []


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def verify_deterministic(briefing: Briefing, corpus: Sequence[SourceRecord]) -> VerificationReport:
    """Run every Layer 1 gate over a briefing against its corpus snapshot.

    ``corpus`` must be the snapshot the briefing was generated from. Verifying
    against a newer snapshot is meaningless: offsets index into the text as it
    was, which is exactly why ``Citation.text_sha`` is checked.
    """
    by_id = {r.source_id: r for r in corpus}
    lexicon = CorpusLexicon(corpus)

    findings: list[Finding] = []
    claims = citations = entities = 0

    for section in briefing.sections:
        for claim in section.claims:
            claims += 1
            citations += len(claim.citations)
            findings += _gate_citation_integrity(section.section_id, claim, by_id)
            findings += _gate_trial_reference(section.section_id, claim, lexicon)
            entity_findings, seen = _gate_entity_support(section.section_id, claim, lexicon)
            findings += entity_findings
            entities += seen
            findings += _gate_trial_state(section.section_id, claim, by_id)
            findings += _gate_tier_separation(section.section_id, section.kind, claim)

    findings += _check_suppression(briefing)

    return VerificationReport(
        briefing_id=briefing.briefing_id,
        findings=findings,
        claims_checked=claims,
        citations_checked=citations,
        entities_checked=entities,
    )


def _check_suppression(briefing: Briefing) -> list[Finding]:
    """A suppressed implications layer must actually be absent.

    When evidence is too sparse to support analysis, the implications section is
    suppressed rather than asked to speculate. If the flag says suppressed and
    the claims are still there, the flag is the thing that is wrong.
    """
    if not briefing.implications_suppressed:
        return []
    leaked = [
        s.section_id for s in briefing.sections if s.kind is SectionKind.IMPLICATIONS and s.claims
    ]
    return [
        Finding(
            gate=Gate.TIER_SEPARATION,
            severity=Severity.ERROR,
            message=(
                "briefing is marked implications_suppressed but still carries implications claims"
            ),
            section_id=section_id,
        )
        for section_id in leaked
    ]


def unsupported_entities(briefing: Briefing, corpus: Sequence[SourceRecord]) -> set[str]:
    """Every name in the briefing that no retrieved record vouches for."""
    lexicon = CorpusLexicon(corpus)
    names: set[str] = set()
    for section in briefing.sections:
        for claim in section.claims:
            names |= extract_named_entities(claim.text)
    return set(lexicon.unsupported(names))


def corpus_for(briefing: Briefing, records: Sequence[SourceRecord]) -> list[SourceRecord]:
    """The subset of a record set the briefing actually cites, plus its sources."""
    wanted = {c.source_id for s in briefing.sections for cl in s.claims for c in cl.citations}
    wanted |= set(briefing.sources)
    return [r for r in records if r.source_id in wanted or r.source_type is SourceType.TRIAL]
