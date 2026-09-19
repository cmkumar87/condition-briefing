"""Controlled-vocabulary lookups used to validate expansion terms.

Nothing a model suggests is trusted on its own. A suggested synonym earns a
place in retrieval only if a controlled vocabulary recognises it — MeSH for
conditions, RxNorm for drug and trade names. Everything else is surfaced to the
analyst as a suggestion and never silently queried.
"""

from __future__ import annotations

from app.sources.base import HttpSource

ICD10_URL = "https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search"
RXNAV_URL = "https://rxnav.nlm.nih.gov/REST"


class ICD10Client(HttpSource):
    """NLM Clinical Table Search Service. Free, no key."""

    provider = "icd10cm"
    rate_limit = 5.0

    async def lookup(self, term: str, *, limit: int = 12) -> list[tuple[str, str]]:
        """Return [(code, description)] for a condition term."""
        payload = await self.get_json(
            ICD10_URL, {"sf": "code,name", "terms": term, "maxList": str(limit)}
        )
        # Response shape: [total, [codes], null, [[code, name], ...]]
        rows = payload[3] if len(payload) > 3 and payload[3] else []
        return [(row[0], row[1]) for row in rows if len(row) >= 2]

    async def codes_for(self, term: str, *, limit: int = 12) -> list[str]:
        return [code for code, _ in await self.lookup(term, limit=limit)]


class RxNormClient(HttpSource):
    """RxNav. Used only to confirm that a suggested drug or trade name is real."""

    provider = "rxnorm"
    rate_limit = 5.0

    async def is_known_drug(self, name: str) -> bool:
        try:
            payload = await self.get_json(f"{RXNAV_URL}/rxcui.json", {"name": name, "search": "2"})
        except Exception:  # noqa: BLE001
            # A vocabulary being unreachable must not promote an unvalidated
            # term. Failing closed keeps unverified names out of retrieval.
            return False
        return bool((payload.get("idGroup") or {}).get("rxnormId"))
