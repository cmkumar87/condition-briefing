"""The gold set: five conditions, and what an expert says retrieval must find.

Two files per condition, split by who owns them:

* ``conditions.toml`` — engineering metadata. Slug, query, therapeutic area,
  expected evidence density, which fixture (if any) backs it.
* ``must_include/<slug>.csv`` — the clinician's list. Five columns, three of
  them required, opens in Excel.

The split exists because a format that is tedious for a clinician is a format
that never gets filled, and without it there is no recall metric — the metric
the brief calls the one that matters most. So the clinician sees a spreadsheet
with five columns and nothing else: no YAML indentation to get wrong, no JSON
commas, no schema to read.

**The must-include lists are deliberately NOT pre-populated from the corpus.**
Seeding them with what retrieval already found would make recall measure
whether the gold set agrees with the pipeline, which it would, by construction,
at close to 1.0. The list has to be an independent statement of what a
knowledgeable reader expects to see. See ``must_include/README.md``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ItemKind(StrEnum):
    """What sort of thing the expert expects to be found."""

    TRIAL = "trial"
    THERAPY = "therapy"


class Criticality(StrEnum):
    """How badly it matters that retrieval found this.

    ``CORE`` items are the recall denominator: missing one is a real failure of
    the briefing. ``SUPPORTING`` items are tracked but scored separately, so a
    long tail of nice-to-haves cannot quietly drag the headline number down and
    discourage anyone from adding to the list.
    """

    CORE = "core"
    SUPPORTING = "supporting"


class MustIncludeItem(_Base):
    """One row of a clinician's must-include CSV."""

    kind: ItemKind
    #: An NCT id for a trial, or a generic (INN) drug name for a therapy.
    #: Matched case-insensitively and as a substring, so "teclistamab" matches
    #: a record whose intervention reads "BIOLOGICAL: Teclistamab-cqyv".
    identifier: str
    #: Free text, for humans reading the report.
    label: str = ""
    criticality: Criticality = Criticality.CORE
    notes: str = ""
    #: 1-based line in the source CSV, for error messages that point somewhere.
    source_line: int | None = None

    @property
    def is_core(self) -> bool:
        return self.criticality is Criticality.CORE

    def normalized(self) -> str:
        return self.identifier.strip().casefold()


class CurationStatus(StrEnum):
    PENDING = "pending"
    PARTIAL = "partial"
    CURATED = "curated"


class EvidenceDensityBand(StrEnum):
    """Roughly how much evidence exists, so the gold set spans the range.

    A gold set of five dense oncology conditions would never exercise graceful
    degradation, which is exactly where a briefing engine embarrasses itself.
    """

    DENSE = "dense"
    MODERATE = "moderate"
    SPARSE = "sparse"


class GoldCondition(_Base):
    """One condition in the gold set."""

    slug: str
    name: str
    query: str
    therapeutic_area: str
    evidence_density: EvidenceDensityBand
    #: Why this condition earns a slot. Five is a small budget; each one is
    #: here to exercise something the others do not.
    rationale: str = ""
    #: Fixture basenames, when this condition is already captured.
    briefing_fixture: str | None = None
    corpus_fixture: str | None = None
    must_include: tuple[MustIncludeItem, ...] = Field(default_factory=tuple)
    curated_by: str = ""
    curated_on: str = ""
    #: Problems found while loading the CSV — bad rows are reported, never
    #: silently dropped, or the recall denominator quietly shrinks.
    load_errors: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def core_items(self) -> tuple[MustIncludeItem, ...]:
        return tuple(i for i in self.must_include if i.is_core)

    @property
    def supporting_items(self) -> tuple[MustIncludeItem, ...]:
        return tuple(i for i in self.must_include if not i.is_core)

    @property
    def curation_status(self) -> CurationStatus:
        if not self.must_include:
            return CurationStatus.PENDING
        if not self.core_items or not self.curated_by:
            return CurationStatus.PARTIAL
        return CurationStatus.CURATED

    @property
    def is_scorable(self) -> bool:
        """Whether a recall number computed for this condition means anything.

        Recall over an empty must-include list is 1.0, which is worse than no
        number at all: it looks like a pass.
        """
        return bool(self.core_items)

    @property
    def has_fixture(self) -> bool:
        return bool(self.briefing_fixture or self.corpus_fixture)


class GoldSet(_Base):
    conditions: tuple[GoldCondition, ...] = Field(default_factory=tuple)

    def __iter__(self):
        return iter(self.conditions)

    def __len__(self) -> int:
        return len(self.conditions)

    def by_slug(self, slug: str) -> GoldCondition | None:
        return next((c for c in self.conditions if c.slug == slug), None)

    @property
    def scorable(self) -> tuple[GoldCondition, ...]:
        return tuple(c for c in self.conditions if c.is_scorable)

    @property
    def with_fixtures(self) -> tuple[GoldCondition, ...]:
        return tuple(c for c in self.conditions if c.has_fixture)

    @property
    def load_errors(self) -> tuple[str, ...]:
        return tuple(e for c in self.conditions for e in c.load_errors)

    def coverage_report(self) -> str:
        """One line per condition. Printed by CI so the curation gap is visible
        rather than being a thing everyone assumes someone else is doing."""
        rows = [f"{'condition':<32} {'density':<9} {'status':<9} core  supporting  fixture"]
        for c in self.conditions:
            rows.append(
                f"{c.slug:<32} {c.evidence_density.value:<9} "
                f"{c.curation_status.value:<9} {len(c.core_items):>4}  "
                f"{len(c.supporting_items):>10}  {'yes' if c.has_fixture else 'no'}"
            )
        return "\n".join(rows)
