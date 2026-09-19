"""NCBI E-utilities: PubMed search, metadata and abstracts.

Supplies the standard-of-care half of the briefing. Publication-type filters do
most of the work — a guideline, a meta-analysis and a case report are wildly
different evidence, and PubMed already labels them.

Rate limit is a hard external constraint: 3 requests/second without an API key,
10 with one. Exceeding it earns 429s and a temporary block.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from typing import Any

from app.models import ConditionProfile, LiteratureMeta, SourceRecord, SourceType
from app.sources.base import HttpSource, RateLimiter, year_from
from app.sources.text import render_literature

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

#: Publication-type filters, strongest evidence first. The briefing's
#: standard-of-care section is built from GUIDELINES and SYNTHESES; PRIMARY is
#: used to fill gaps when a condition turns out to be evidence-sparse.
GUIDELINES = "(Practice Guideline[pt] OR Guideline[pt])"
SYNTHESES = "(Systematic Review[pt] OR Meta-Analysis[pt])"
PRIMARY = "(Randomized Controlled Trial[pt] OR Clinical Trial, Phase III[pt])"

#: PubMed caps efetch/esummary batches; stay well under it.
BATCH_SIZE = 200

#: Journals that publish preprints into PubMed via the NIH Preprint Pilot.
_PREPRINT_JOURNALS = {"medrxiv", "biorxiv", "research square"}


class PubMedClient(HttpSource):
    provider = "pubmed"
    rate_limit = 3.0

    def __init__(self, *args: Any, api_key: str | None = None, **kwargs: Any) -> None:
        # An API key raises the ceiling from 3 to 10 requests/second. Worth
        # having for a real run: a briefing issues dozens of E-utilities calls.
        if api_key:
            kwargs.setdefault("limiter", RateLimiter(10.0))
        super().__init__(*args, **kwargs)
        self._api_key = api_key

    def _params(self, **extra: Any) -> dict[str, Any]:
        params = {"db": "pubmed", **extra}
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    async def search(
        self,
        profile: ConditionProfile,
        *,
        pub_type_filter: str = GUIDELINES,
        since_year: int = 2021,
        max_records: int = 50,
    ) -> list[SourceRecord]:
        pmids = await self.search_ids(
            profile, pub_type_filter=pub_type_filter, since_year=since_year, retmax=max_records
        )
        return await self.fetch_records(pmids) if pmids else []

    async def search_ids(
        self,
        profile: ConditionProfile,
        *,
        pub_type_filter: str = GUIDELINES,
        since_year: int = 2021,
        retmax: int = 50,
    ) -> list[str]:
        payload = await self.get_json(
            f"{BASE_URL}/esearch.fcgi",
            self._params(
                term=self._term(profile, pub_type_filter, since_year),
                retmax=str(retmax),
                retmode="json",
                sort="date",
            ),
        )
        return list(payload["esearchresult"].get("idlist") or [])

    async def fetch_records(self, pmids: list[str]) -> list[SourceRecord]:
        """Two calls per batch: esummary for metadata, efetch for abstracts."""
        records: list[SourceRecord] = []
        for start in range(0, len(pmids), BATCH_SIZE):
            batch = pmids[start : start + BATCH_SIZE]
            ids = ",".join(batch)

            summary = await self.get_json(
                f"{BASE_URL}/esummary.fcgi", self._params(id=ids, retmode="json")
            )
            xml_text = await self.get_text(
                f"{BASE_URL}/efetch.fcgi",
                self._params(id=ids, rettype="abstract", retmode="xml"),
            )
            abstracts = parse_abstracts(xml_text)

            results = summary.get("result", {})
            for pmid in batch:
                entry = results.get(pmid)
                if entry:
                    records.append(parse_article(pmid, entry, abstracts.get(pmid)))
        return records

    @staticmethod
    def _term(profile: ConditionProfile, pub_type_filter: str, since_year: int) -> str:
        """Build an Entrez query.

        The MeSH descriptor carries the expansion — PubMed explodes a MeSH term
        to its subtree automatically, which is most of why resolution matters.
        Free-text synonyms are OR'd in to catch very recent records that have
        not been MeSH-indexed yet; indexing lags publication by weeks to months,
        which is exactly the window a strategy briefing cares about most.
        """
        clauses: list[str] = []
        if profile.mesh_descriptor:
            clauses.append(f'"{profile.mesh_descriptor}"[MeSH Terms]')
        for term in [profile.query, *profile.entry_terms, *profile.synonyms]:
            if term:
                clauses.append(f'"{term}"[Title/Abstract]')

        concept = " OR ".join(dict.fromkeys(clauses)) or f'"{profile.query}"'
        return f'({concept}) AND {pub_type_filter} AND ("{since_year}"[dp] : "3000"[dp])'


def parse_abstracts(xml_text: str) -> dict[str, str]:
    """Extract structured abstracts from an efetch XML response.

    Section labels (BACKGROUND / METHODS / RESULTS) are preserved because they
    make far better citable spans than an undifferentiated block of prose.
    """
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # A malformed batch must not lose the metadata we already have.
        return out

    for article in root.iter("PubmedArticle"):
        pmid_el = article.find(".//MedlineCitation/PMID")
        if pmid_el is None or not pmid_el.text:
            continue
        chunks: list[str] = []
        for node in article.iter("AbstractText"):
            label = node.get("Label")
            body = "".join(node.itertext()).strip()
            if body:
                chunks.append(f"{label}: {body}" if label else body)
        if chunks:
            out[pmid_el.text] = "\n".join(chunks)
    return out


def parse_article(
    pmid: str,
    summary: dict[str, Any],
    abstract: str | None,
    retrieved_at: datetime | None = None,
) -> SourceRecord:
    """Normalise one esummary entry into a SourceRecord.

    iCite fields are left unset here; app/sources/icite.py enriches them in a
    second pass. Keeping them separate means a PubMed record is still usable
    when iCite is unavailable.
    """
    title = (summary.get("title") or "").rstrip(".")
    journal = summary.get("fulljournalname")
    pub_types = summary.get("pubtype") or []
    doi = next(
        (a.get("value") for a in (summary.get("articleids") or []) if a.get("idtype") == "doi"),
        None,
    )
    year = year_from(summary.get("pubdate"))

    meta = LiteratureMeta(
        pmid=pmid,
        journal=journal,
        journal_abbrev=summary.get("source"),
        pub_types=pub_types,
        pub_date=summary.get("pubdate"),
        year=year,
        doi=doi,
        abstract=abstract,
        is_preprint="Preprint" in pub_types or (journal or "").lower() in _PREPRINT_JOURNALS,
        medline_indexed="MEDLINE" in (summary.get("recordstatus") or ""),
    )

    text = render_literature(title, meta)
    return SourceRecord(
        source_id=SourceRecord.make_source_id("pubmed", pmid),
        source_type=SourceType.LITERATURE,
        provider="pubmed",
        native_id=pmid,
        title=title,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        retrieved_at=retrieved_at or datetime.now(UTC),
        sort_date=date(year, 1, 1) if year else None,
        text=text,
        text_sha=SourceRecord.sha(text),
        literature=meta,
        payload={"esummary": summary},
    )
