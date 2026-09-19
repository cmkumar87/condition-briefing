"""Verification. Two layers, both shipping.

Layer 1 (:mod:`app.verify.deterministic`) is the control: no model, no network,
blocking. Layer 2 is an adversarial LLM judge, and it is advisory until the
calibration harness has scored it against human labels — an uncalibrated judge
is a second opinion from the same model family, not a control.
"""

from __future__ import annotations

from app.verify.deterministic import (
    corpus_for,
    extract_phases,
    unsupported_entities,
    verify_deterministic,
)
from app.verify.entities import (
    CorpusLexicon,
    extract_named_entities,
    extract_nct_ids,
    normalize,
)
from app.verify.models import Finding, Gate, Severity, VerificationReport

__all__ = [
    "CorpusLexicon",
    "Finding",
    "Gate",
    "Severity",
    "VerificationReport",
    "corpus_for",
    "extract_named_entities",
    "extract_nct_ids",
    "extract_phases",
    "normalize",
    "unsupported_entities",
    "verify_deterministic",
]
