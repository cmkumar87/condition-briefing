"""Collapse journal co-publications into one logical source.

Major guidelines are routinely published simultaneously in several journals —
the 2026 AHA/ACC/ADA/ASN cardiovascular-kidney-metabolic guideline appears in
both JACC and Circulation, and the KDIGO heart-failure report in both JACC
Heart Failure and Kidney International. They are one piece of evidence with two
PMIDs and two DOIs.

Left alone this corrupts two things at once: the guideline occupies two slots in
a size-limited corpus, and — worse — it double-counts as corroboration, which is
the strongest replication signal in the credibility score. Two copies of one
document are not two independent sources agreeing.

This runs as a corpus-level pass rather than inside a parser: detection needs to
see every record at once, and a parser that mutated records based on their
neighbours would not be reproducible from a single raw response.
"""

from __future__ import annotations

import re
import unicodedata

from app.models import SourceRecord, SourceType

_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")

#: Titles are compared on a prefix. Co-publications occasionally differ in a
#: trailing subtitle ("...: A Report of the ACC/AHA Joint Committee" vs
#: "...: Executive Summary"), which a full-string match would miss.
_PREFIX_LEN = 70


def normalize_title(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title or "").lower()
    return _SPACE.sub(" ", _PUNCT.sub(" ", folded)).strip()


def _key(record: SourceRecord) -> str:
    return normalize_title(record.title)[:_PREFIX_LEN]


def _primacy(record: SourceRecord) -> tuple[int, int, str]:
    """Rank candidates for which copy is the canonical one.

    Most-cited first, since that is the copy the field actually references;
    PMID as a deterministic tie-break so runs are reproducible.
    """
    meta = record.literature
    citations = (meta.citation_count if meta else None) or 0
    return (-citations, 0, record.native_id)


def detect_copublications(records: list[SourceRecord]) -> dict[str, str]:
    """Mark co-publications in place; return {duplicate_id: canonical_id}.

    Only literature is considered. Two trials with similar titles are two
    trials, and two labels for the same drug are genuinely distinct documents.
    """
    groups: dict[str, list[SourceRecord]] = {}
    for record in records:
        if record.source_type is not SourceType.LITERATURE or not record.literature:
            continue
        key = _key(record)
        if key:
            groups.setdefault(key, []).append(record)

    mapping: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        canonical, *duplicates = sorted(members, key=_primacy)
        for dup in duplicates:
            if dup.literature:
                dup.literature.copublication_of = canonical.source_id
            mapping[dup.source_id] = canonical.source_id
    return mapping
