"""Capture live source data into committed fixtures.

CONTRACT OWNER ONLY. Regenerating changes the bytes all three streams test
against, so run it deliberately (``make fixtures``) and commit the diff.

This file carries a REFERENCE PARSER for each source. It is not production
code — Stream A writes the real clients in ``app/sources/``. The reference
parser exists so that:

  * Streams B and C have real ``SourceRecord`` data to build against on day
    one, without waiting for Stream A.
  * Stream A has a concrete, executable target: parse ``fixtures/raw/*.json``
    and produce records equal to ``fixtures/records/*.json``. That equality is
    a contract test, not a code review opinion.

Two conditions are captured deliberately:
  * multiple myeloma — fast-moving oncology, crowded late-phase pipeline
  * heart failure    — stable cardiology, dense guideline base
Together they exercise condition-adaptive recency in ``app/rank/``.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.models import (  # noqa: E402
    DrugLabelMeta,
    LiteratureMeta,
    SourceRecord,
    SourceType,
    SponsorClass,
    TrialMeta,
)

HERE = Path(__file__).parent
RAW = HERE / "raw"
RECORDS = HERE / "records"
UA = {"User-Agent": "condition-briefing/0.1 (fixture capture; contact: strategy-team)"}

#: Frozen so fixtures are byte-stable across regeneration.
CAPTURED_AT = datetime(2026, 9, 19, 0, 0, 0, tzinfo=UTC)


def _get(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _get_json(url: str, timeout: int = 45) -> Any:
    return json.loads(_get(url, timeout))


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"  wrote {path.relative_to(HERE.parents[1])}")


# ===========================================================================
# ClinicalTrials.gov v2
# ===========================================================================

CTGOV = "https://clinicaltrials.gov/api/v2/studies"


def fetch_trials(condition: str, page_size: int = 5) -> dict[str, Any]:
    q = urllib.parse.urlencode(
        {
            "query.cond": condition,
            "filter.overallStatus": "RECRUITING",
            "aggFilters": "phase:3",
            "pageSize": str(page_size),
            "sort": "StartDate:desc",
            "countTotal": "true",
        }
    )
    return _get_json(f"{CTGOV}?{q}")


def parse_trial(study: dict[str, Any]) -> SourceRecord:
    """REFERENCE PARSER — Stream A's app/sources/ct_gov.py must match this."""
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
        sponsor_class = SponsorClass.UNKNOWN

    interventions = [
        f"{i.get('type', '')}: {i.get('name', '')}".strip(": ")
        for i in arms.get("interventions", [])
        if i.get("name")
    ]
    institutions = _unique(
        [loc.get("facility", "") for loc in locs.get("locations", []) if loc.get("facility")]
    )
    countries = _unique(
        [loc.get("country", "") for loc in locs.get("locations", []) if loc.get("country")]
    )

    meta = TrialMeta(
        nct_id=nct,
        phases=design.get("phases", []) or [],
        overall_status=status.get("overallStatus"),
        study_type=design.get("studyType"),
        enrollment=(design.get("enrollmentInfo") or {}).get("count"),
        start_date=(status.get("startDateStruct") or {}).get("date"),
        primary_completion_date=(status.get("primaryCompletionDateStruct") or {}).get("date"),
        completion_date=(status.get("completionDateStruct") or {}).get("date"),
        lead_sponsor=lead.get("name"),
        lead_sponsor_class=sponsor_class,
        collaborators=[c.get("name", "") for c in spons.get("collaborators", []) if c.get("name")],
        interventions=interventions,
        conditions=conds.get("conditions", []) or [],
        institutions=institutions,
        countries=countries,
        has_results=bool(study.get("hasResults")),
    )

    text = _render_trial_text(title, meta, desc.get("briefSummary"))
    return SourceRecord(
        source_id=SourceRecord.make_source_id("clinicaltrials.gov", nct),
        source_type=SourceType.TRIAL,
        provider="clinicaltrials.gov",
        native_id=nct,
        title=title,
        url=f"https://clinicaltrials.gov/study/{nct}",
        retrieved_at=CAPTURED_AT,
        sort_date=_parse_partial_date(meta.start_date),
        text=text,
        text_sha=SourceRecord.sha(text),
        trial=meta,
        payload=study,
    )


def _render_trial_text(title: str, m: TrialMeta, summary: str | None) -> str:
    """Build the citable document text.

    Every Citation's character offsets index into this string, so its shape is
    part of the contract. Structured fields are spelled out as prose-like lines
    precisely so the model can cite a span that carries a verifiable fact —
    "Status: TERMINATED" is a citable span; a JSON blob is not.
    """
    lines = [
        f"{m.nct_id} — {title}",
        "",
        f"Status: {m.overall_status or 'UNKNOWN'}",
        f"Phase: {', '.join(m.phases) if m.phases else 'N/A'}",
        f"Study type: {m.study_type or 'UNKNOWN'}",
        f"Enrollment: {m.enrollment if m.enrollment is not None else 'not reported'} participants",
        f"Start date: {m.start_date or 'not reported'}",
        f"Primary completion date: {m.primary_completion_date or 'not reported'}",
        f"Lead sponsor: {m.lead_sponsor or 'not reported'} ({m.lead_sponsor_class.value})",
    ]
    if m.collaborators:
        lines.append(f"Collaborators: {'; '.join(m.collaborators)}")
    if m.interventions:
        lines.append(f"Interventions: {'; '.join(m.interventions)}")
    if m.conditions:
        lines.append(f"Conditions studied: {'; '.join(m.conditions)}")
    if m.institutions:
        shown = m.institutions[:12]
        suffix = f" (and {len(m.institutions) - 12} more)" if len(m.institutions) > 12 else ""
        lines.append(f"Participating institutions: {'; '.join(shown)}{suffix}")
    lines.append(f"Results posted: {'yes' if m.has_results else 'no'}")
    if summary:
        lines += ["", "Brief summary:", summary.strip()]
    return "\n".join(lines)


# ===========================================================================
# PubMed E-utilities
# ===========================================================================

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
GUIDELINE_FILTER = "(Practice Guideline[pt] OR Guideline[pt])"


def fetch_literature(mesh_term: str, retmax: int = 6) -> dict[str, Any]:
    term = f'("{mesh_term}"[MeSH Terms]) AND {GUIDELINE_FILTER} AND ("2021"[dp] : "3000"[dp])'
    search = _get_json(
        f"{EUTILS}/esearch.fcgi?"
        + urllib.parse.urlencode(
            {"db": "pubmed", "term": term, "retmax": str(retmax), "retmode": "json", "sort": "date"}
        )
    )
    pmids = search["esearchresult"]["idlist"]
    time.sleep(0.4)  # NCBI: 3 req/s without an API key

    summary = _get_json(
        f"{EUTILS}/esummary.fcgi?"
        + urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})
    )
    time.sleep(0.4)

    abstracts_xml = _get(
        f"{EUTILS}/efetch.fcgi?"
        + urllib.parse.urlencode(
            {"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "xml"}
        )
    )
    time.sleep(0.4)

    icite = _get_json("https://icite.od.nih.gov/api/pubs?pmids=" + ",".join(pmids))
    return {
        "term": term,
        "pmids": pmids,
        "esearch": search,
        "esummary": summary,
        "efetch_xml": abstracts_xml,
        "icite": icite,
    }


def _parse_abstracts(xml_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for art in root.iter("PubmedArticle"):
        pmid_el = art.find(".//MedlineCitation/PMID")
        if pmid_el is None or not pmid_el.text:
            continue
        chunks: list[str] = []
        for ab in art.iter("AbstractText"):
            label = ab.get("Label")
            body = "".join(ab.itertext()).strip()
            if body:
                chunks.append(f"{label}: {body}" if label else body)
        if chunks:
            out[pmid_el.text] = "\n".join(chunks)
    return out


def parse_literature(bundle: dict[str, Any]) -> list[SourceRecord]:
    """REFERENCE PARSER — Stream A's pubmed.py + icite.py must match this."""
    abstracts = _parse_abstracts(bundle["efetch_xml"])
    icite_by_pmid = {str(r.get("pmid")): r for r in (bundle["icite"].get("data") or [])}
    results: dict[str, Any] = bundle["esummary"]["result"]

    records: list[SourceRecord] = []
    for pmid in bundle["pmids"]:
        r = results.get(pmid)
        if not r:
            continue
        title = (r.get("title") or "").rstrip(".")
        journal = r.get("fulljournalname")
        pub_types = r.get("pubtype") or []
        doi = next(
            (a.get("value") for a in (r.get("articleids") or []) if a.get("idtype") == "doi"), None
        )
        year = _year_from(r.get("pubdate"))
        ic = icite_by_pmid.get(pmid, {})

        is_preprint = "Preprint" in pub_types or (journal or "").lower() in {
            "medrxiv",
            "biorxiv",
            "research square",
        }
        meta = LiteratureMeta(
            pmid=pmid,
            journal=journal,
            journal_abbrev=r.get("source"),
            pub_types=pub_types,
            pub_date=r.get("pubdate"),
            year=year,
            doi=doi,
            abstract=abstracts.get(pmid),
            is_preprint=is_preprint,
            medline_indexed="MEDLINE" in (r.get("recordstatus") or ""),
            # None here means "not yet accrued", never "zero impact". The
            # age-tiering in app/rank/ depends on preserving that distinction.
            rcr=ic.get("relative_citation_ratio"),
            nih_percentile=ic.get("nih_percentile"),
            citation_count=ic.get("citation_count"),
            clinical_citation_count=len(ic.get("cited_by_clin") or []) or None,
            icite_enriched=bool(ic),
        )

        text = _render_literature_text(title, meta)
        records.append(
            SourceRecord(
                source_id=SourceRecord.make_source_id("pubmed", pmid),
                source_type=SourceType.LITERATURE,
                provider="pubmed",
                native_id=pmid,
                title=title,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                retrieved_at=CAPTURED_AT,
                sort_date=date(year, 1, 1) if year else None,
                text=text,
                text_sha=SourceRecord.sha(text),
                literature=meta,
                payload={"esummary": r, "icite": ic},
            )
        )
    return records


def _render_literature_text(title: str, m: LiteratureMeta) -> str:
    lines = [
        title,
        "",
        f"Journal: {m.journal or 'not reported'}",
        f"Publication date: {m.pub_date or 'not reported'}",
        f"Publication types: {', '.join(m.pub_types) if m.pub_types else 'not reported'}",
        f"PMID: {m.pmid}",
    ]
    if m.doi:
        lines.append(f"DOI: {m.doi}")
    if m.is_preprint:
        lines.append("NOTE: This record is a preprint and has not been peer reviewed.")
    if m.abstract:
        lines += ["", "Abstract:", m.abstract]
    return "\n".join(lines)


# ===========================================================================
# openFDA drug labels
# ===========================================================================


def fetch_label(generic_name: str) -> dict[str, Any]:
    q = urllib.parse.urlencode({"search": f'openfda.generic_name:"{generic_name}"', "limit": "1"})
    return _get_json(f"https://api.fda.gov/drug/label.json?{q}")


def parse_label(result: dict[str, Any]) -> SourceRecord:
    """REFERENCE PARSER — Stream A's app/sources/openfda.py must match this."""
    of = result.get("openfda", {})
    spl_id = result.get("id") or (of.get("spl_set_id") or ["unknown"])[0]
    brand = of.get("brand_name", []) or []
    generic = of.get("generic_name", []) or []
    title = f"{brand[0] if brand else (generic[0] if generic else 'Unknown')} — FDA label"

    meta = DrugLabelMeta(
        spl_id=spl_id,
        brand_names=brand,
        generic_names=generic,
        manufacturers=of.get("manufacturer_name", []) or [],
        routes=of.get("route", []) or [],
        pharm_classes=of.get("pharm_class_epc", []) or [],
        effective_time=result.get("effective_time"),
        boxed_warning=_first(result.get("boxed_warning")),
        indications=_first(result.get("indications_and_usage")),
    )
    text = _render_label_text(title, meta)
    return SourceRecord(
        source_id=SourceRecord.make_source_id("openfda", spl_id),
        source_type=SourceType.DRUG_LABEL,
        provider="openfda",
        native_id=spl_id,
        title=title,
        url=f"https://labels.fda.gov/{spl_id}",
        retrieved_at=CAPTURED_AT,
        sort_date=_parse_partial_date(meta.effective_time),
        text=text,
        text_sha=SourceRecord.sha(text),
        drug_label=meta,
        payload=result,
    )


def _render_label_text(title: str, m: DrugLabelMeta) -> str:
    lines = [
        title,
        "",
        f"Brand name(s): {', '.join(m.brand_names) or 'not reported'}",
        f"Generic name(s): {', '.join(m.generic_names) or 'not reported'}",
        f"Manufacturer: {', '.join(m.manufacturers) or 'not reported'}",
        f"Route: {', '.join(m.routes) or 'not reported'}",
        f"Label effective date: {m.effective_time or 'not reported'}",
    ]
    if m.boxed_warning:
        lines += ["", "BOXED WARNING:", m.boxed_warning.strip()]
    if m.indications:
        lines += ["", "Indications and usage:", m.indications.strip()]
    return "\n".join(lines)


# ===========================================================================
# helpers
# ===========================================================================


def _unique(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for i in items:
        if i:
            seen.setdefault(i, None)
    return list(seen)


def _first(v: Any) -> str | None:
    if isinstance(v, list) and v:
        return v[0]
    return v if isinstance(v, str) else None


def _year_from(pubdate: str | None) -> int | None:
    if not pubdate:
        return None
    head = pubdate.strip().split(" ")[0]
    return int(head) if head.isdigit() and len(head) == 4 else None


def _parse_partial_date(value: str | None) -> date | None:
    """CT.gov gives 'YYYY-MM' or 'YYYY-MM-DD'; openFDA gives 'YYYYMMDD'."""
    if not value:
        return None
    v = value.strip()
    try:
        if len(v) == 8 and v.isdigit():
            return date(int(v[:4]), int(v[4:6]), int(v[6:8]))
        parts = v.split("-")
        if len(parts) == 1:
            return date(int(parts[0]), 1, 1)
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1)
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


def _dump_records(name: str, records: list[SourceRecord]) -> None:
    _write(
        RECORDS / f"{name}.json",
        [json.loads(r.model_dump_json()) for r in records],
    )


# ===========================================================================


CONDITIONS = [
    # (slug, CT.gov condition term, MeSH term, drug to pull a label for)
    ("multiple_myeloma", "Multiple Myeloma", "Multiple Myeloma", "teclistamab"),
    ("heart_failure", "Heart Failure", "Heart Failure", "dapagliflozin"),
]


def main() -> None:
    for slug, cond, mesh, drug in CONDITIONS:
        print(f"\n=== {slug} ===")

        print("ClinicalTrials.gov ...")
        raw_trials = fetch_trials(cond)
        _write(RAW / f"{slug}__ctgov.json", raw_trials)
        trials = [parse_trial(s) for s in raw_trials.get("studies", [])]
        _dump_records(f"{slug}__trials", trials)

        print("PubMed + iCite ...")
        raw_lit = fetch_literature(mesh)
        _write(RAW / f"{slug}__pubmed.json", raw_lit)
        _dump_records(f"{slug}__literature", parse_literature(raw_lit))

        print(f"openFDA ({drug}) ...")
        raw_label = fetch_label(drug)
        _write(RAW / f"{slug}__openfda.json", raw_label)
        results = raw_label.get("results") or []
        if results:
            _dump_records(f"{slug}__labels", [parse_label(results[0])])

    print("\nfixtures captured.")


if __name__ == "__main__":
    main()
