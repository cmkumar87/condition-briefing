"""Shared fixture loaders. CONTRACT-OWNED — all three streams import from here.

Do not build your own SourceRecord or Briefing objects in stream tests. If each
stream invents its own test data, all three pass their own suites and none of
them integrate. Load from here instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.models import Briefing, SourceRecord

DIR = Path(__file__).parent
RECORDS = DIR / "records"
RAW = DIR / "raw"

CONDITIONS = ("multiple_myeloma", "heart_failure")


def load_records(name: str) -> list[SourceRecord]:
    """e.g. load_records("multiple_myeloma__trials")"""
    return [
        SourceRecord.model_validate(r) for r in json.loads((RECORDS / f"{name}.json").read_text())
    ]


def load_corpus(condition: str) -> list[SourceRecord]:
    """All records for one condition, across every source."""
    out: list[SourceRecord] = []
    for kind in ("trials", "literature", "labels"):
        path = RECORDS / f"{condition}__{kind}.json"
        if path.exists():
            out.extend(load_records(f"{condition}__{kind}"))
    return out


def load_raw(name: str) -> dict:
    """Verbatim upstream API response, e.g. load_raw("multiple_myeloma__ctgov")."""
    return json.loads((RAW / f"{name}.json").read_text())


def load_briefing(name: str = "briefing__multiple_myeloma") -> Briefing:
    return Briefing.model_validate(json.loads((DIR / f"{name}.json").read_text()))


def load_broken_briefing() -> Briefing:
    """A briefing with four seeded defects. Every deterministic gate must fail on it."""
    return load_briefing("briefing__broken")


def all_records() -> list[SourceRecord]:
    return [r for c in CONDITIONS for r in load_corpus(c)]
