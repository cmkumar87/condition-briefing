"""Collapse journal co-publications into one logical source.

Major guidelines are routinely published in two journals at once — the 2026
AHA/ACC/ADA/ASN CKM guideline appears in both *JACC* and *Circulation*, and the
KDIGO heart-failure conference conclusions appear in both *JACC: Heart Failure*
and *Kidney International*. Left alone, one guideline counts twice as
corroboration, which is the strongest single signal in the credibility score.

Detection is by ``LiteratureMeta.copublication_of`` when the retrieval layer
managed to set it, and otherwise by exact normalized-title match. Normalized
title match is deliberately strict: in the fixtures it collapses the two real
pairs and leaves the ASCO Living Guideline and the ASCO-Ontario Health Living
Guideline — genuinely different documents sharing a title prefix — apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date

from app.models import SourceRecord, SourceType

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str) -> str:
    """Case-, punctuation- and whitespace-insensitive title key."""
    return _NON_ALNUM.sub(" ", (title or "").lower()).strip()


class CopublicationGroup:
    """One logical source and the records that are all publications of it."""

    __slots__ = ("canonical", "duplicates")

    def __init__(self, canonical: SourceRecord, duplicates: list[SourceRecord]) -> None:
        self.canonical = canonical
        self.duplicates = duplicates

    @property
    def urls(self) -> list[str]:
        """Every place this one logical source can be read."""
        return [self.canonical.url, *(d.url for d in self.duplicates)]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CopublicationGroup({self.canonical.source_id}, +{len(self.duplicates)})"


def _rank_key(rec: SourceRecord) -> tuple:
    """Which record of a co-published set is the one to keep.

    Prefer the one whose metadata is richest — iCite-enriched, MEDLINE-indexed,
    most publication types — then the earliest publication, then the source_id
    so the choice is stable across runs. Citation *counts* deliberately do not
    decide this: which of two journals accrued more citations for the same
    guideline is an artifact of indexing, not a quality signal.
    """
    lit = rec.literature
    return (
        -int(bool(lit and lit.icite_enriched)),
        -int(bool(lit and lit.medline_indexed)),
        -len(lit.pub_types) if lit else 0,
        rec.sort_date or date.max,
        rec.source_id,
    )


def group_copublications(records: Sequence[SourceRecord]) -> list[CopublicationGroup]:
    """Partition records into logical sources, collapsing co-publications."""
    by_id = {r.source_id: r for r in records}
    parent: dict[str, str] = {r.source_id: r.source_id for r in records}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 1. Explicit links from the retrieval layer.
    for rec in records:
        other = rec.literature.copublication_of if rec.literature else None
        if other and other in by_id:
            union(rec.source_id, other)

    # 2. Exact normalized-title match among literature records.
    by_title: dict[str, list[str]] = {}
    for rec in records:
        if rec.source_type is not SourceType.LITERATURE:
            continue
        by_title.setdefault(normalize_title(rec.title), []).append(rec.source_id)
    for ids in by_title.values():
        for other in ids[1:]:
            union(ids[0], other)

    clusters: dict[str, list[SourceRecord]] = {}
    for rec in records:
        clusters.setdefault(find(rec.source_id), []).append(rec)

    groups: list[CopublicationGroup] = []
    for members in clusters.values():
        ordered = sorted(members, key=_rank_key)
        groups.append(CopublicationGroup(ordered[0], ordered[1:]))
    return groups


def merged_literature_metrics(group: CopublicationGroup) -> tuple[float | None, int | None]:
    """Best-known (rcr, clinical_citation_count) across a co-published set.

    Taken as the maximum, never the sum: the citations of one guideline are
    split across its journal versions by whichever index a citing author
    happened to reference. Summing them would reintroduce exactly the
    double-count that collapsing exists to remove. ``None`` survives as ``None``
    when no version has accrued anything.
    """
    rcrs = [m.rcr for m in _metas([group.canonical, *group.duplicates]) if m.rcr is not None]
    cccs = [
        m.clinical_citation_count
        for m in _metas([group.canonical, *group.duplicates])
        if m.clinical_citation_count is not None
    ]
    return (max(rcrs) if rcrs else None, max(cccs) if cccs else None)


def _metas(records: Iterable[SourceRecord]):
    return [r.literature for r in records if r.literature is not None]
