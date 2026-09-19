"""Renderers for ``SourceRecord.text`` — the citable document text.

This is the most consequential module in Stream A. ``text`` is the exact string
handed to the Citations API as a document block, and every Citation's character
offsets index into it. Change how a line is built and every previously stored
citation silently points at the wrong span — valid-looking, wrong.

Structured registry fields are spelled out as prose-like lines on purpose:
"Status: TERMINATED" is a span a model can cite and a verifier can check against
the registry. A JSON blob is neither.

DELIBERATE DUPLICATION: ``tests/fixtures/_capture.py`` contains an independent
implementation of these same renderers. It is the reference spec. If this module
imported from there (or vice versa) the parity test would be a tautology; two
independent implementations that must agree byte-for-byte is a real test.
"""

from __future__ import annotations

from app.models import DrugLabelMeta, LiteratureMeta, TrialMeta

#: Beyond this, the institution list is truncated with a count. Large
#: multi-site trials otherwise contribute thousands of tokens of facility
#: names to a prompt that is already 150-300K tokens.
MAX_INSTITUTIONS_SHOWN = 12


def render_trial(title: str, m: TrialMeta, brief_summary: str | None) -> str:
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
        shown = m.institutions[:MAX_INSTITUTIONS_SHOWN]
        overflow = len(m.institutions) - MAX_INSTITUTIONS_SHOWN
        suffix = f" (and {overflow} more)" if overflow > 0 else ""
        lines.append(f"Participating institutions: {'; '.join(shown)}{suffix}")
    lines.append(f"Results posted: {'yes' if m.has_results else 'no'}")
    if brief_summary:
        lines += ["", "Brief summary:", brief_summary.strip()]
    return "\n".join(lines)


def render_literature(title: str, m: LiteratureMeta) -> str:
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
        # Stated inside the citable text, not only in metadata, so that a model
        # quoting this record sees the caveat in the same span it is citing.
        lines.append("NOTE: This record is a preprint and has not been peer reviewed.")
    if m.abstract:
        lines += ["", "Abstract:", m.abstract]
    return "\n".join(lines)


def render_drug_label(title: str, m: DrugLabelMeta) -> str:
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
