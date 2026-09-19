"""Build the Briefing fixture from captured source records. Offline, deterministic.

CONTRACT OWNER ONLY.

Streams B and C both need a realistic ``Briefing`` before the pipeline exists:
Stream C renders it, Stream B tests its verification gates against it. Hand-
authoring one invites invalid citation offsets, which would make every gate
look broken. So this generator locates each cited span inside the REAL record
text with ``str.find`` — every offset in the fixture is guaranteed to slice
back to exactly its ``cited_text``.

It also emits a deliberately BROKEN companion fixture. The deterministic gates
in ``app/verify/`` must fail on it; a verifier that passes both fixtures is not
a verifier.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.models import (  # noqa: E402
    Briefing,
    BriefingSection,
    Citation,
    Claim,
    ConditionProfile,
    EvidenceDensity,
    SectionKind,
    SourceRecord,
    SourceRef,
)

HERE = Path(__file__).parent
RECORDS = HERE / "records"


def load(name: str) -> list[SourceRecord]:
    return [
        SourceRecord.model_validate(r) for r in json.loads((RECORDS / f"{name}.json").read_text())
    ]


def cite(rec: SourceRecord, needle: str) -> Citation:
    """Locate a real span in a real record. Raises if absent — fixtures cannot lie."""
    start = rec.text.find(needle)
    if start < 0:
        raise ValueError(f"span not found in {rec.source_id}: {needle!r}")
    return Citation(
        source_id=rec.source_id,
        cited_text=needle,
        start_char=start,
        end_char=start + len(needle),
        text_sha=rec.text_sha,
    )


def main() -> None:
    trials = {r.native_id: r for r in load("multiple_myeloma__trials")}
    lits = load("multiple_myeloma__literature")
    labels = load("multiple_myeloma__labels")

    label = labels[0]
    t_gsk = trials["NCT07285239"]
    t_azd = trials["NCT07764978"]
    t_leeds = trials["NCT07649525"]

    claims_soc = [
        Claim(
            claim_id="soc-1",
            text=(
                f"{label.drug_label.brand_names[0]} is approved for relapsed or refractory "
                "multiple myeloma in combination with daratumumab and hyaluronidase-fihj."
            ),
            citations=[cite(label, "Indications and usage:")],
        ),
        Claim(
            claim_id="soc-2",
            text=(
                "The label carries a boxed warning for cytokine release syndrome "
                "and neurologic toxicity."
            ),
            citations=[cite(label, "BOXED WARNING:")],
        ),
    ]

    claims_emerging = [
        Claim(
            claim_id="emg-1",
            text=(
                "A Phase 3 trial is comparing belantamab mafodotin against daratumumab, each "
                "combined with bortezomib, lenalidomide and dexamethasone, in newly diagnosed "
                "transplant-ineligible myeloma."
            ),
            citations=[cite(t_gsk, "Phase: PHASE3"), cite(t_gsk, "Status: RECRUITING")],
        ),
        Claim(
            claim_id="emg-2",
            text=(
                "AstraZeneca is enrolling 750 participants in a Phase 3 study of AZD0120 "
                "in transplant-ineligible newly diagnosed myeloma."
            ),
            citations=[
                cite(t_azd, "Enrollment: 750 participants"),
                cite(t_azd, "Lead sponsor: AstraZeneca (INDUSTRY)"),
            ],
        ),
    ]

    claims_players = [
        Claim(
            claim_id="ply-1",
            text=(
                "GlaxoSmithKline is a named collaborator on the PrECOG-led belantamab "
                "mafodotin Phase 3 study."
            ),
            citations=[cite(t_gsk, "Collaborators: GlaxoSmithKline")],
        ),
        Claim(
            claim_id="ply-2",
            text=(
                "The University of Leeds leads a 1226-participant response- and fitness-adapted "
                "trial with Cancer Research UK, Blood Cancer UK and Johnson & Johnson."
            ),
            citations=[cite(t_leeds, "Lead sponsor: University of Leeds (OTHER)")],
        ),
    ]

    claims_impl = [
        Claim(
            claim_id="imp-1",
            text=(
                "Bispecific and antibody-drug-conjugate regimens moving into newly diagnosed, "
                "transplant-ineligible populations would shift myeloma treatment volume earlier "
                "in the pathway and toward outpatient infusion capacity. CRS and neurotoxicity "
                "monitoring requirements imply step-up dosing capacity and staff training that "
                "community sites do not uniformly have today."
            ),
            citations=[],  # implications carry no citations by contract
        )
    ]

    sources: dict[str, SourceRef] = {}
    for rec in [*trials.values(), *lits, *labels]:
        sources[rec.source_id] = SourceRef(
            source_id=rec.source_id,
            source_type=rec.source_type,
            title=rec.title,
            url=rec.url,
            provider=rec.provider,
            sort_date=rec.sort_date,
            detail=(
                rec.trial.overall_status
                if rec.trial
                else rec.literature.journal
                if rec.literature
                else None
            ),
        )

    density = EvidenceDensity(
        record_count=len(sources),
        guideline_count=sum(
            1 for r in lits if "Practice Guideline" in (r.literature.pub_types or [])
        ),
        trial_count=len(trials),
        late_phase_trial_count=sum(
            1 for r in trials.values() if "PHASE3" in (r.trial.phases or [])
        ),
        newest_year=2026,
        is_sparse=False,
        rationale="5 late-phase trials and an approved label; sufficient for synthesis.",
    )

    briefing = Briefing(
        briefing_id="brf-fixture-001",
        run_id="run-fixture-001",
        condition="Multiple myeloma",
        profile=ConditionProfile(
            query="multiple myeloma",
            mesh_descriptor="Multiple Myeloma",
            mesh_ui="D009101",
            entry_terms=["Myeloma, Plasma-Cell", "Kahler Disease", "Myelomatosis"],
            icd10_codes=["C90.0"],
            synonyms=["plasma cell myeloma"],
            unvalidated_synonyms=["bone marrow cancer"],
            analyst_reviewed=True,
        ),
        as_of=date(2026, 9, 19),
        sections=[
            BriefingSection(
                section_id="standard-of-care",
                heading="Current standard of care",
                kind=SectionKind.EVIDENCE,
                claims=claims_soc,
                density=density,
            ),
            BriefingSection(
                section_id="emerging",
                heading="Emerging treatments in development",
                kind=SectionKind.EVIDENCE,
                claims=claims_emerging,
                density=density,
            ),
            BriefingSection(
                section_id="key-players",
                heading="Key companies and institutions",
                kind=SectionKind.EVIDENCE,
                claims=claims_players,
                density=density,
            ),
            BriefingSection(
                section_id="implications",
                heading="Strategic implications",
                kind=SectionKind.IMPLICATIONS,
                claims=claims_impl,
                density=density,
            ),
        ],
        sources=sources,
    )

    out = HERE / "briefing__multiple_myeloma.json"
    out.write_text(
        json.dumps(json.loads(briefing.model_dump_json()), indent=2, sort_keys=True) + "\n"
    )
    print(f"  wrote {out.name}  ({sum(len(s.claims) for s in briefing.sections)} claims)")

    # --- The broken companion. Every deterministic gate must fail on this. ---
    broken = briefing.model_copy(deep=True)
    broken.briefing_id = "brf-fixture-broken-001"
    # 1. citation offsets that do not slice back to cited_text
    broken.sections[0].claims[0].citations[0].start_char += 40
    broken.sections[0].claims[0].citations[0].end_char += 40
    # 2. a citation to a source that is not in the corpus at all
    broken.sections[1].claims[0].citations[0].source_id = "clinicaltrials.gov:NCT99999999"
    # 3. an invented company that appears in no record
    broken.sections[2].claims[1].text = "Vantrix Therapeutics leads the Leeds study."
    # 4. an implications claim smuggling a citation, violating the two-tier contract
    broken.sections[3].claims[0].citations = [cite(t_gsk, "Status: RECRUITING")]

    out_b = HERE / "briefing__broken.json"
    out_b.write_text(
        json.dumps(json.loads(broken.model_dump_json()), indent=2, sort_keys=True) + "\n"
    )
    print(f"  wrote {out_b.name}  (4 seeded defects)")


if __name__ == "__main__":
    main()
