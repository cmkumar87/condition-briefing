"""Briefing -> HTML.

A pure function of the ``Briefing`` object. This package reads no corpus, makes
no API calls and imports nothing from ``app.sources``, ``app.rank``,
``app.synthesize`` or ``app.verify`` — ``briefing.sources`` is guaranteed
self-sufficient by contract test ``test_renderer_needs_no_corpus_access``, so
the surface can iterate against already-generated briefings for free.

    from app.render import render_briefing, RenderOptions

    html = render_briefing(briefing, RenderOptions(today=date(2026, 9, 19)))

The design problem this package exists to solve is keeping two tiers of
epistemic status apart and never letting them blur:

* **Evidence** — every claim carries a citation, one click from the sentence to
  the NCT record, PubMed entry or FDA label that supports it.
* **Implications** — labeled strategic analysis. Valuable, not sourced fact,
  and rendered as a different element with a standing label.

See :mod:`app.render.integrity` for what the surface can and cannot verify on
its own, and why it says so on the page.
"""

from __future__ import annotations

from app.render.html import (
    CSS_FILENAME,
    render_briefing,
    render_view,
    stylesheet,
    write_briefing,
)
from app.render.integrity import (
    Defect,
    DefectCode,
    IntegrityReport,
    Severity,
    inspect,
)
from app.render.viewmodel import BriefingView, RenderOptions, build_view

__all__ = [
    "CSS_FILENAME",
    "BriefingView",
    "Defect",
    "DefectCode",
    "IntegrityReport",
    "RenderOptions",
    "Severity",
    "build_view",
    "inspect",
    "render_briefing",
    "render_view",
    "stylesheet",
    "write_briefing",
]
