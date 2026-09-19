"""The rendered HTML contract.

These assert on the document tree, not on substrings, so they fail when the
structure regresses rather than when the whitespace moves. The things tested
here are the promises the surface makes to a reader who is about to spend
capital on it:

* the two tiers are structurally distinct and cannot be confused;
* any sentence is one click from its primary source;
* nothing defective is hidden;
* the renderer never implies a verification it did not perform.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models import JudgeResult, SectionKind, Verdict
from app.render import RenderOptions, render_briefing
from tests.render.htmlprobe import by_class, by_id, hrefs_in, norm, parse


def at(as_of: date, **kw) -> RenderOptions:
    return RenderOptions(today=as_of, inline_css=False, **kw)


@pytest.fixture
def good_html(briefing) -> str:
    return render_briefing(briefing, at(briefing.as_of))


@pytest.fixture
def broken_html(broken_briefing) -> str:
    return render_briefing(broken_briefing, at(broken_briefing.as_of))


# --------------------------------------------------------------------------
# the two tiers, never blurred
# --------------------------------------------------------------------------


def test_evidence_and_implications_are_different_elements(briefing, good_html):
    """The separation lives in the document tree, not only in the stylesheet.

    It therefore survives a screen reader, reader mode and a stylesheet that
    fails to load.
    """
    seen = 0
    for section in briefing.sections:
        element = by_id(good_html, f"section-{section.section_id}")
        assert element is not None, f"section {section.section_id} did not render"
        if section.kind is SectionKind.EVIDENCE:
            assert element.tag == "article"
            assert element.has_class("section--evidence")
        else:
            assert element.tag == "aside"
            assert element.has_class("section--implications")
        seen += 1
    assert seen == len(briefing.sections)


def test_implications_section_carries_a_standing_label(briefing, good_html):
    """Not a one-time heading: a reader who scrolls into the middle of the
    analysis block still knows what they are reading."""
    for section in briefing.sections:
        if section.kind is SectionKind.IMPLICATIONS:
            element = by_id(good_html, f"section-{section.section_id}")
            assert "not sourced fact" in element.inner_html
            assert "tier-banner" in element.inner_html
            assert "not sourced fact" in element.attrs.get("aria-label", "").lower()


def test_analysis_claims_carry_the_label_as_real_text(briefing, good_html):
    """A ::before would not survive copy-paste. Half these sentences travel by
    being pasted into an email, and the label has to travel with them."""
    for section in briefing.sections:
        if section.kind is not SectionKind.IMPLICATIONS:
            continue
        for claim in section.claims:
            element = by_id(good_html, f"claim-{claim.claim_id}")
            marks = [e for e in parse(element.inner_html) if e.has_class("tier-mark")]
            assert marks, f"{claim.claim_id} has no analysis mark in the DOM"
            assert "Analysis" in marks[0].text
            # A real separator, so a paste reads "Analysis — Bispecific", not
            # "AnalysisBispecific".
            assert "</span>&#32;" in element.inner_html


def test_every_claim_records_its_tier_as_a_data_attribute(briefing, good_html):
    for section in briefing.sections:
        for claim in section.claims:
            element = by_id(good_html, f"claim-{claim.claim_id}")
            assert element.attrs["data-tier"] == section.kind.value


def test_the_legend_defines_both_tiers_before_any_content(good_html):
    legend = by_class(good_html, "legend")
    assert legend, "no two-tier key rendered"
    body = good_html.index("<body")
    assert good_html.index('class="legend"') < good_html.index('class="section'), (
        "the key must appear before the first section"
    )
    assert body < good_html.index('class="legend"')


# --------------------------------------------------------------------------
# one click from any sentence to its primary source
# --------------------------------------------------------------------------


def test_every_cited_claim_links_to_its_primary_source(briefing, good_html):
    """The whole defensibility argument reduces to this test."""
    checked = 0
    for section in briefing.sections:
        for claim in section.claims:
            if not claim.citations:
                continue
            element = by_id(good_html, f"claim-{claim.claim_id}")
            hrefs = hrefs_in(element.inner_html)
            for cite in claim.citations:
                url = briefing.sources[cite.source_id].url
                assert url in hrefs, (
                    f"{claim.claim_id} does not link to {cite.source_id} in one click"
                )
                checked += 1
    assert checked >= 6, "fixture should exercise more citations than this"


def test_source_links_are_external_and_safe(good_html):
    for chip in by_class(good_html, "chip"):
        href = chip.attrs.get("href")
        if not href or href.startswith("#"):
            continue
        assert href.startswith("https://"), "a chip must reach the primary record directly"
        assert chip.attrs.get("target") == "_blank"
        assert "noopener" in chip.attrs.get("rel", "")


def test_quoted_spans_are_shown_verbatim(briefing, good_html):
    """A reader checking a number wants the exact characters the model was
    handed, not a paraphrase of them."""
    for section in briefing.sections:
        for claim in section.claims:
            element = by_id(good_html, f"claim-{claim.claim_id}")
            for cite in claim.citations:
                assert cite.cited_text in element.inner_html
                assert str(cite.start_char) in element.inner_html


def test_duplicate_citations_to_one_source_collapse_to_one_chip(briefing, good_html):
    """Two spans of the same trial need one link, not two identical ones — but
    both spans stay enumerated in the disclosure, so nothing is hidden."""
    for section in briefing.sections:
        for claim in section.claims:
            distinct = {c.source_id for c in claim.citations}
            if len(distinct) == len(claim.citations):
                continue
            element = by_id(good_html, f"claim-{claim.claim_id}")
            chips = [e for e in parse(element.inner_html) if e.has_class("chip")]
            assert len(chips) == len(distinct)
            assert all(c.cited_text in element.inner_html for c in claim.citations)


def test_sources_appendix_lists_every_source_with_backrefs(briefing, good_html):
    for source_id, ref in briefing.sources.items():
        element = by_id(good_html, f"src-{_slug(source_id)}")
        assert element is not None, f"{source_id} missing from the appendix"
        assert ref.url in element.inner_html

    cited = {c.source_id for s in briefing.sections for cl in s.claims for c in cl.citations}
    for source_id in cited:
        element = by_id(good_html, f"src-{_slug(source_id)}")
        assert "Cited by" in element.inner_html


def test_uncited_sources_are_shown_dimmed_not_dropped(briefing, good_html):
    """The corpus is wider than the prose. Hiding the remainder would make the
    evidence base look exactly as large as the argument built on it."""
    cited = {c.source_id for s in briefing.sections for cl in s.claims for c in cl.citations}
    uncited = set(briefing.sources) - cited
    assert uncited, "fixture should contain retrieved-but-uncited sources"
    for source_id in uncited:
        element = by_id(good_html, f"src-{_slug(source_id)}")
        assert element.has_class("source--uncited")


def _slug(value: str) -> str:
    from app.render.viewmodel import slug

    return slug(value)


# --------------------------------------------------------------------------
# defective claims are flagged, never dropped
# --------------------------------------------------------------------------


def test_no_claim_is_ever_dropped(briefing, broken_briefing, broken_html, good_html):
    """A renderer that quietly hides broken claims defeats the whole
    verification layer: the reader sees a shorter, cleaner briefing and has no
    idea why."""
    for source, html in ((briefing, good_html), (broken_briefing, broken_html)):
        for section in source.sections:
            for claim in section.claims:
                element = by_id(html, f"claim-{claim.claim_id}")
                assert element is not None, f"{claim.claim_id} was dropped"
                assert claim.text[:40] in element.inner_html


def test_defective_claims_are_visibly_flagged(broken_html):
    flagged = {e.attrs["data-claim-id"] for e in by_class(broken_html, "claim--flagged")}
    assert flagged == {"emg-1", "imp-1"}, (
        "the two briefing-visible defects must both flag their claim"
    )


def test_a_dangling_citation_renders_as_a_broken_non_link(broken_html):
    element = by_id(broken_html, "claim-emg-1")
    broken = [e for e in parse(element.inner_html) if e.has_class("chip--broken")]
    assert len(broken) == 1
    assert broken[0].tag != "a", "a citation that resolves to nothing must not look clickable"
    assert "NCT99999999" in broken[0].text
    # The claim's other, valid citation still works.
    assert "https://clinicaltrials.gov/study/NCT07285239" in hrefs_in(element.inner_html)


def test_the_integrity_banner_reports_and_links_to_every_defect(broken_html):
    banner = by_id(broken_html, "integrity")
    assert banner.has_class("integrity--defective")
    assert "2 defects found in this briefing" in norm(banner.text)
    assert "#claim-emg-1" in banner.inner_html
    assert "#claim-imp-1" in banner.inner_html
    assert "never removed" in banner.inner_html


def test_the_integrity_banner_is_clean_on_the_good_fixture(good_html):
    banner = by_id(good_html, "integrity")
    assert banner.has_class("integrity--clean")
    assert "Structurally sound" in banner.inner_html


# --------------------------------------------------------------------------
# never imply a verification that did not happen
# --------------------------------------------------------------------------


def test_unjudged_claims_say_so_and_are_not_styled_as_passing(good_html):
    """Absence of evidence of a defect is not a green tick."""
    verdicts = by_class(good_html, "verdict")
    assert verdicts
    for v in verdicts:
        assert "Not independently judged" in v.text
        assert v.has_class("verdict--neutral")
        assert not v.has_class("verdict--ok")


def test_a_clean_banner_still_states_what_it_did_not_check(good_html):
    """A green banner with no stated scope is how a document gets trusted
    further than it has earned."""
    banner = by_id(good_html, "integrity")
    assert "Not checked here" in banner.inner_html
    for phrase in ("offsets", "company", "phase and status"):
        assert phrase in banner.inner_html


def test_judge_verdicts_render_with_distinct_treatments(briefing):
    section = briefing.sections[0]
    claims = list(section.claims)
    claims[0] = claims[0].model_copy(
        update={
            "judge": JudgeResult(
                verdict=Verdict.NOT_SUPPORTED,
                reason="The span does not carry the claim.",
                judge_model="claude-opus-5",
            )
        }
    )
    mutated = briefing.model_copy(
        update={"sections": [section.model_copy(update={"claims": claims}), *briefing.sections[1:]]}
    )
    html = render_briefing(mutated, at(briefing.as_of))
    element = by_id(html, f"claim-{claims[0].claim_id}")
    assert element.has_class("claim--flagged")
    assert "Not supported by cited span" in element.inner_html
    assert "The span does not carry the claim." in element.inner_html
    # The reason appears once, in the flag banner — not twice.
    assert element.inner_html.count("The span does not carry the claim.") == 1


# --------------------------------------------------------------------------
# as-of, prominently
# --------------------------------------------------------------------------


def test_as_of_is_in_the_masthead_as_a_machine_readable_time(briefing, good_html):
    masthead = by_class(good_html, "masthead")[0]
    times = [e for e in parse(masthead.inner_html) if e.tag == "time"]
    assert times, "the as-of date must be in the masthead"
    assert times[0].attrs["datetime"] == briefing.as_of.isoformat()
    assert "Evidence as of" in masthead.inner_html
    assert by_class(good_html, "asof")


@pytest.mark.parametrize(
    ("age_days", "state"),
    [(0, "current"), (10, "current"), (45, "aging"), (150, "stale")],
)
def test_the_as_of_block_changes_state_as_the_briefing_ages(briefing, age_days, state):
    html = render_briefing(briefing, at(briefing.as_of + timedelta(days=age_days)))
    block = by_class(html, "asof")[0]
    assert block.has_class(f"asof--{state}")
    if state == "stale":
        assert "re-run" in block.text


# --------------------------------------------------------------------------
# density, sparseness, suppression
# --------------------------------------------------------------------------


def test_density_is_rendered_per_section(briefing, good_html):
    for section in briefing.sections:
        if section.density is None:
            continue
        element = by_id(good_html, f"section-{section.section_id}")
        assert str(section.density.record_count) in element.inner_html
        assert "density" in element.inner_html


def test_density_caption_differs_across_the_tier_line(briefing, good_html):
    """The same numbers mean different things either side of it: support for
    the claims, versus the base the analysis read from."""
    assert "Evidence density" in good_html
    assert "Evidence base this analysis reads from" in good_html


def test_sparse_sections_render_their_notice(briefing):
    section = briefing.sections[0]
    mutated = briefing.model_copy(
        update={
            "sections": [
                section.model_copy(
                    update={
                        "density": section.density.model_copy(update={"is_sparse": True}),
                        "sparse_notice": "Three Phase I trials and no guidelines.",
                    }
                ),
                *briefing.sections[1:],
            ]
        }
    )
    html = render_briefing(mutated, at(briefing.as_of))
    element = by_id(html, f"section-{section.section_id}")
    assert "Three Phase I trials and no guidelines." in element.inner_html
    assert "notice" in element.inner_html


def test_suppressed_implications_are_announced_not_silently_absent(briefing):
    """Silence would read as "there were no implications worth drawing", which
    is a different and much more flattering claim than "we declined to
    speculate"."""
    mutated = briefing.model_copy(
        update={
            "sections": [s for s in briefing.sections if s.kind is SectionKind.EVIDENCE],
            "implications_suppressed": True,
            "suppression_reason": "Below the evidence-density threshold.",
        }
    )
    html = render_briefing(mutated, at(briefing.as_of))
    assert "notice--suppressed" in html
    assert "Below the evidence-density threshold." in html
    assert "section--implications" not in _body_of(html)


def _body_of(html: str) -> str:
    return html[html.index("<body") :]


# --------------------------------------------------------------------------
# the renderer as a pure, safe function
# --------------------------------------------------------------------------


def test_rendering_is_deterministic(briefing):
    first = render_briefing(briefing, at(briefing.as_of))
    second = render_briefing(briefing, at(briefing.as_of))
    assert first == second


def test_claim_text_is_escaped(briefing):
    """Claim text is model-generated and quoted spans are verbatim upstream
    text. Neither is trusted markup."""
    section = briefing.sections[0]
    claims = list(section.claims)
    claims[0] = claims[0].model_copy(update={"text": '<script>alert("x")</script> & <b>bold</b>'})
    mutated = briefing.model_copy(
        update={"sections": [section.model_copy(update={"claims": claims}), *briefing.sections[1:]]}
    )
    html = render_briefing(mutated, at(briefing.as_of))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_inlined_css_makes_a_self_contained_file(briefing):
    """These get emailed and dropped in shared drives."""
    html = render_briefing(briefing, RenderOptions(today=briefing.as_of, inline_css=True))
    assert "<style>" in html
    assert "--analysis-accent" in html
    assert '<link rel="stylesheet"' not in html


def test_the_feedback_widget_is_absent_unless_an_endpoint_is_configured(briefing):
    assert 'id="feedback"' not in render_briefing(briefing, at(briefing.as_of))
    html = render_briefing(briefing, at(briefing.as_of, feedback_endpoint="/api/feedback"))
    form = by_id(html, "feedback")
    assert form is not None
    assert 'action="/api/feedback"' in html
    assert briefing.briefing_id in form.inner_html


def test_the_document_declares_language_charset_and_viewport(good_html):
    assert '<html lang="en">' in good_html
    assert 'charset="utf-8"' in good_html
    assert "width=device-width" in good_html
