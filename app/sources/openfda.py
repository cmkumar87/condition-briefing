"""openFDA: approved drug labels.

Grounds the "what is actually approved today" half of standard of care, which
is exactly where a model left to its own devices will confidently describe an
investigational agent as available. Labels also carry boxed warnings, and a
boxed warning is often the operational fact a health system cares about most —
it implies monitoring capacity, staff training and site-of-care constraints.

Rate limit: 240 requests/minute per IP without an API key, 1000 with one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models import ConditionProfile, DrugLabelMeta, SourceRecord, SourceType
from app.sources.base import HttpSource, first, parse_partial_date
from app.sources.text import render_drug_label

BASE_URL = "https://api.fda.gov/drug/label.json"
MAX_LIMIT = 100


class OpenFDAClient(HttpSource):
    provider = "openfda"
    rate_limit = 4.0

    def __init__(self, *args: Any, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._api_key = api_key

    def _params(self, search: str, limit: int) -> dict[str, Any]:
        params: dict[str, Any] = {"search": search, "limit": str(min(limit, MAX_LIMIT))}
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    async def search_by_indication(
        self, profile: ConditionProfile, *, limit: int = 25
    ) -> list[SourceRecord]:
        """Labels whose indications mention the condition.

        Searching the indications field rather than drug names is what makes
        this work for a general-purpose tool: we do not know the drug names for
        an arbitrary condition in advance — that is the question being asked.
        """
        terms = profile.search_terms()[:6]
        expr = " OR ".join(f'indications_and_usage:"{t}"' for t in terms)
        return await self._search(expr, limit)

    async def search_by_drug(self, name: str, *, limit: int = 1) -> list[SourceRecord]:
        """Look up a specific agent by generic or brand name.

        Useful as a second pass over intervention names harvested from
        ClinicalTrials.gov: it answers "is this investigational agent already
        approved for something else", which is a question strategy teams ask
        constantly and which no single source answers on its own.
        """
        expr = f'openfda.generic_name:"{name}" OR openfda.brand_name:"{name}"'
        return await self._search(expr, limit)

    async def _search(self, expr: str, limit: int) -> list[SourceRecord]:
        try:
            payload = await self.get_json(BASE_URL, self._params(expr, limit))
        except Exception as exc:  # noqa: BLE001
            # openFDA returns 404 for "no matches", which is a normal outcome
            # for a rare condition, not an error worth failing a run over.
            if _is_no_match(exc):
                return []
            raise
        return [parse_label(r) for r in (payload.get("results") or [])]


def _is_no_match(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


def parse_label(result: dict[str, Any], retrieved_at: datetime | None = None) -> SourceRecord:
    """Normalise one structured product label into a SourceRecord."""
    openfda = result.get("openfda", {})
    spl_id = result.get("id") or first(openfda.get("spl_set_id")) or "unknown"
    brand = openfda.get("brand_name", []) or []
    generic = openfda.get("generic_name", []) or []
    display = brand[0] if brand else (generic[0] if generic else "Unknown")
    title = f"{display} — FDA label"

    meta = DrugLabelMeta(
        spl_id=spl_id,
        brand_names=brand,
        generic_names=generic,
        manufacturers=openfda.get("manufacturer_name", []) or [],
        routes=openfda.get("route", []) or [],
        pharm_classes=openfda.get("pharm_class_epc", []) or [],
        effective_time=result.get("effective_time"),
        boxed_warning=first(result.get("boxed_warning")),
        indications=first(result.get("indications_and_usage")),
    )

    text = render_drug_label(title, meta)
    return SourceRecord(
        source_id=SourceRecord.make_source_id("openfda", spl_id),
        source_type=SourceType.DRUG_LABEL,
        provider="openfda",
        native_id=spl_id,
        title=title,
        url=f"https://labels.fda.gov/{spl_id}",
        retrieved_at=retrieved_at or datetime.now(UTC),
        sort_date=parse_partial_date(meta.effective_time),
        text=text,
        text_sha=SourceRecord.sha(text),
        drug_label=meta,
        payload=result,
    )
