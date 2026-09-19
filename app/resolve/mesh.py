"""MeSH descriptor lookup via NCBI E-utilities.

The expansion that makes retrieval work. A registry query for the literal string
"heart attack" finds almost nothing; the MeSH descriptor *Myocardial Infarction*
carries entry terms including "Heart Attack", "STEMI" and "NSTEMI", and PubMed
explodes the descriptor to its subtree automatically.

This is unglamorous and it is where briefing recall is won or lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.sources.base import HttpSource

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


@dataclass(slots=True)
class MeshConcept:
    ui: str
    descriptor: str
    #: Synonyms MeSH itself recognises. High confidence by construction — these
    #: need no further validation.
    entry_terms: list[str] = field(default_factory=list)
    scope_note: str | None = None


class MeshClient(HttpSource):
    provider = "mesh"
    rate_limit = 3.0

    def __init__(self, *args, api_key: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._api_key = api_key

    def _params(self, **extra) -> dict:
        params = {"db": "mesh", **extra}
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    async def resolve(self, query: str) -> MeshConcept | None:
        """Best-matching MeSH descriptor for a free-text condition, or None.

        None is a legitimate outcome — novel or very narrowly-phrased conditions
        may have no descriptor. Retrieval then falls back to free-text search,
        with lower recall, and the analyst review screen is where that gets
        caught.
        """
        search = await self.get_json(
            f"{BASE_URL}/esearch.fcgi",
            self._params(term=query, retmode="json", retmax="1", sort="relevance"),
        )
        uids = search["esearchresult"].get("idlist") or []
        if not uids:
            return None

        summary = await self.get_json(
            f"{BASE_URL}/esummary.fcgi", self._params(id=uids[0], retmode="json")
        )
        return parse_concept(summary["result"][uids[0]])


def parse_concept(entry: dict) -> MeshConcept | None:
    """Parse one MeSH esummary record.

    ``ds_meshterms`` is ordered: the first element is the preferred descriptor,
    the rest are entry terms.
    """
    terms = entry.get("ds_meshterms") or []
    if not terms:
        return None
    return MeshConcept(
        ui=entry.get("ds_meshui", ""),
        descriptor=terms[0],
        entry_terms=list(terms[1:]),
        scope_note=(entry.get("ds_scopenote") or "").strip() or None,
    )
