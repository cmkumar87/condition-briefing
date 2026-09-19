"""NIH iCite: field- and time-normalised citation quality.

This is the free answer to "is this citation any good". Journal Impact Factor is
Clarivate-licensed and out of scope; iCite's Relative Citation Ratio is not, and
is better suited anyway because it is field-normalised — an oncology paper and a
cardiology paper are directly comparable, which matters for a general-purpose
tool that will be asked about both.

Enrichment is a separate pass from PubMed retrieval on purpose: a PubMed record
is still usable when iCite is unavailable, and citation metrics are the one
signal the ranking layer must treat as optional.
"""

from __future__ import annotations

from typing import Any

from app.models import SourceRecord
from app.sources.base import HttpSource

BASE_URL = "https://icite.od.nih.gov/api/pubs"

#: iCite accepts a comma-separated list; keep requests to a sane URL length.
BATCH_SIZE = 200


class ICiteClient(HttpSource):
    provider = "icite"
    rate_limit = 5.0

    async def enrich(self, records: list[SourceRecord]) -> list[SourceRecord]:
        """Attach citation metrics to literature records, in place.

        Records without a PMID are skipped. A failed lookup leaves the record
        un-enriched rather than raising — ranking degrades to provenance-only,
        which is the correct behaviour anyway for anything recent.
        """
        by_pmid = {r.literature.pmid: r for r in records if r.literature}
        if not by_pmid:
            return records

        pmids = list(by_pmid)
        for start in range(0, len(pmids), BATCH_SIZE):
            batch = pmids[start : start + BATCH_SIZE]
            payload = await self.get_json(BASE_URL, {"pmids": ",".join(batch)})
            apply_enrichment(records, payload.get("data") or [])
        return records


def apply_enrichment(records: list[SourceRecord], icite_rows: list[dict[str, Any]]) -> None:
    """Merge iCite rows into matching records.

    Note what is NOT done here: a missing metric is left as ``None``. It must
    never be coerced to 0.0. ``None`` means "not yet accrued" — a guideline
    published three months ago has no citations because it is new, not because
    it is weak — and the ranking layer's age tiering depends on being able to
    tell those apart.
    """
    rows = {str(row.get("pmid")): row for row in icite_rows}
    for record in records:
        if not record.literature:
            continue
        row = rows.get(record.literature.pmid)
        if not row:
            continue

        meta = record.literature
        meta.rcr = row.get("relative_citation_ratio")
        meta.nih_percentile = row.get("nih_percentile")
        meta.citation_count = row.get("citation_count")
        # `or None` collapses a genuine zero into None. Preserved deliberately
        # for byte-parity with the committed fixtures; see NOTES-retrieval.md.
        # Consumers should read `icite_enriched` to tell "zero" from "unknown".
        meta.clinical_citation_count = len(row.get("cited_by_clin") or []) or None
        meta.icite_enriched = True

        record.payload["icite"] = row
