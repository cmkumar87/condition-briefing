"""ClinicalTrials.gov API v2.

The backbone of the briefing. Phase, status, sponsor, collaborators, enrollment,
sites and dates all arrive as structured fields, which means "emerging treatments
in development" and "key companies and institutions" are registry lookups rather
than model inferences. No auth, no key, generous limits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models import ConditionProfile, SourceRecord, SourceType, SponsorClass, TrialMeta
from app.sources.base import HttpSource, parse_partial_date, unique
from app.sources.text import render_trial

BASE_URL = "https://clinicaltrials.gov/api/v2/studies"

#: Deliberately includes TERMINATED, WITHDRAWN and SUSPENDED.
#:
#: It is tempting to fetch only live trials — the briefing is about what is
#: coming, after all. That would be a mistake. The deterministic staleness gate
#: catches "terminated trial described as promising" by comparing prose against
#: the registry status IN THE CORPUS. A trial that was never retrieved cannot be
#: checked, so filtering out dead trials at retrieval silently disables the very
#: gate that protects against the most expensive failure mode.
DEFAULT_STATUSES = (
    "NOT_YET_RECRUITING",
    "RECRUITING",
    "ENROLLING_BY_INVITATION",
    "ACTIVE_NOT_RECRUITING",
    "COMPLETED",
    "TERMINATED",
    "WITHDRAWN",
    "SUSPENDED",
)

#: Late-phase by default. Phase 1 is added automatically when a condition turns
#: out to be evidence-sparse — see `search`'s `include_early_phase`.
DEFAULT_PHASES = ("2", "3")

MAX_PAGE_SIZE = 1000


class ClinicalTrialsClient(HttpSource):
    provider = "clinicaltrials.gov"
    rate_limit = 5.0
    base_url = BASE_URL

    async def search(
        self,
        profile: ConditionProfile,
        *,
        max_records: int = 200,
        phases: tuple[str, ...] = DEFAULT_PHASES,
        statuses: tuple[str, ...] = DEFAULT_STATUSES,
        include_early_phase: bool = False,
    ) -> list[SourceRecord]:
        """Retrieve trials for a resolved condition profile.

        Expansion terms are OR'd in an Essie expression so that a query for
        "heart attack" also matches records indexed under Myocardial Infarction.
        """
        params = build_search_params(
            profile,
            phases=phases,
            statuses=statuses,
            include_early_phase=include_early_phase,
            page_size=min(max_records, MAX_PAGE_SIZE),
        )

        records: list[SourceRecord] = []
        token: str | None = None
        while len(records) < max_records:
            page_params = dict(params)
            if token:
                page_params["pageToken"] = token
            payload = await self.get_json(BASE_URL, page_params)

            studies = payload.get("studies") or []
            if not studies:
                break
            records.extend(parse_study(s) for s in studies)

            token = payload.get("nextPageToken")
            if not token:
                break

        return records[:max_records]


def parse_study(study: dict[str, Any], retrieved_at: datetime | None = None) -> SourceRecord:
    """Normalise one v2 study into a SourceRecord.

    Must produce records equal to tests/fixtures/records/*__trials.json when fed
    the matching raw fixture — see tests/sources/test_parity.py.
    """
    proto = study.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    status = proto.get("statusModule", {})
    design = proto.get("designModule", {})
    spons = proto.get("sponsorCollaboratorsModule", {})
    arms = proto.get("armsInterventionsModule", {})
    conds = proto.get("conditionsModule", {})
    locs = proto.get("contactsLocationsModule", {})
    desc = proto.get("descriptionModule", {})

    nct = ident.get("nctId", "")
    title = ident.get("briefTitle") or ident.get("officialTitle") or nct
    lead = spons.get("leadSponsor", {})

    try:
        sponsor_class = SponsorClass(lead.get("class", "UNKNOWN"))
    except ValueError:
        # CT.gov occasionally introduces new sponsor classes. An unknown value
        # must not lose the whole record.
        sponsor_class = SponsorClass.UNKNOWN

    meta = TrialMeta(
        nct_id=nct,
        phases=design.get("phases") or [],
        overall_status=status.get("overallStatus"),
        study_type=design.get("studyType"),
        enrollment=(design.get("enrollmentInfo") or {}).get("count"),
        start_date=(status.get("startDateStruct") or {}).get("date"),
        primary_completion_date=(status.get("primaryCompletionDateStruct") or {}).get("date"),
        completion_date=(status.get("completionDateStruct") or {}).get("date"),
        lead_sponsor=lead.get("name"),
        lead_sponsor_class=sponsor_class,
        collaborators=[c.get("name", "") for c in spons.get("collaborators", []) if c.get("name")],
        interventions=[
            f"{i.get('type', '')}: {i.get('name', '')}".strip(": ")
            for i in arms.get("interventions", [])
            if i.get("name")
        ],
        conditions=conds.get("conditions") or [],
        institutions=unique(
            [loc.get("facility", "") for loc in locs.get("locations", []) if loc.get("facility")]
        ),
        countries=unique(
            [loc.get("country", "") for loc in locs.get("locations", []) if loc.get("country")]
        ),
        has_results=bool(study.get("hasResults")),
    )

    text = render_trial(title, meta, desc.get("briefSummary"))
    return SourceRecord(
        source_id=SourceRecord.make_source_id("clinicaltrials.gov", nct),
        source_type=SourceType.TRIAL,
        provider="clinicaltrials.gov",
        native_id=nct,
        title=title,
        url=f"https://clinicaltrials.gov/study/{nct}",
        retrieved_at=retrieved_at or datetime.now(UTC),
        sort_date=parse_partial_date(meta.start_date),
        text=text,
        text_sha=SourceRecord.sha(text),
        trial=meta,
        payload=study,
    )


def condition_expression(profile: ConditionProfile) -> str:
    """OR the resolved expansion terms into an Essie expression."""
    terms = profile.search_terms() or [profile.query]
    return " OR ".join(f'"{t}"' if " " in t else t for t in terms)


def build_search_params(
    profile: ConditionProfile,
    *,
    phases: tuple[str, ...] = DEFAULT_PHASES,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    include_early_phase: bool = False,
    page_size: int = 200,
) -> dict[str, str]:
    """Build the v2 query string.

    Separated out and unit-tested because this API fails quietly: a malformed
    filter returns zero studies with HTTP 200 rather than an error, so a typo
    here produces empty briefings instead of a stack trace.
    """
    phase_filter = (*phases, "1") if include_early_phase else phases
    return {
        "query.cond": condition_expression(profile),
        "filter.overallStatus": "|".join(statuses),
        # Space-separated. "phase:23" is NOT a syntax error -- it silently
        # matches nothing.
        "aggFilters": f"phase:{' '.join(phase_filter)}",
        "pageSize": str(min(page_size, MAX_PAGE_SIZE)),
        "sort": "StartDate:desc",
        "countTotal": "true",
    }
