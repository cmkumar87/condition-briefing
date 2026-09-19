"""Render every fixture briefing to browsable HTML. The UX iteration loop.

    uv run python web/preview.py && open web/preview/index.html

The brief says to expect the UX to iterate, and the strict ``Briefing``
contract exists so it can. This is the other half of that: a way to see every
state the surface has to handle, side by side, in one command, with no API
calls and nothing regenerated.

Every variant here is *derived from a committed fixture* via ``model_copy``,
never hand-built. Inventing briefing objects is how three streams end up each
passing against their own private idea of the contract.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # allow `python web/preview.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from app.models import Briefing, JudgeResult, Verdict  # noqa: E402
from app.render import RenderOptions, inspect, render_briefing  # noqa: E402
from tests.fixtures import load_briefing, load_broken_briefing  # noqa: E402

OUT_DIR = Path(__file__).parent / "preview"

#: Fixed so the preview is reproducible and matches the fixtures' as_of.
TODAY = date(2026, 9, 19)


@dataclass(frozen=True, slots=True)
class Variant:
    slug: str
    title: str
    why: str
    briefing: Briefing
    options: RenderOptions


def _judged(briefing: Briefing) -> Briefing:
    """The good briefing with app/verify's verdicts attached.

    Stream B has not landed, so this is what its output will look like when it
    does: one verdict per evidence claim, cycling through every disposition so
    each badge treatment is visible at once.
    """
    verdicts = [
        JudgeResult(
            verdict=Verdict.SUPPORTED,
            reason="The cited span states the approval and the combination directly.",
            confidence=0.94,
            judge_model="claude-opus-5",
        ),
        JudgeResult(
            verdict=Verdict.PARTIAL,
            reason=(
                "The span confirms a boxed warning but does not name neurologic toxicity; "
                "the claim goes slightly beyond it."
            ),
            confidence=0.61,
            judge_model="claude-opus-5",
        ),
        JudgeResult(
            verdict=Verdict.NOT_SUPPORTED,
            reason="The span gives the phase only. Nothing in it names either comparator arm.",
            confidence=0.88,
            judge_model="claude-opus-5",
        ),
        JudgeResult(
            verdict=Verdict.SPAN_IRRELEVANT,
            reason="The quoted span is an enrollment count and does not bear on the claim.",
            confidence=0.79,
            judge_model="claude-opus-5",
        ),
    ]
    sections = []
    cursor = 0
    for section in briefing.sections:
        claims = []
        for claim in section.claims:
            if claim.citations and cursor < len(verdicts):
                claim = claim.model_copy(update={"judge": verdicts[cursor]})
                cursor += 1
            claims.append(claim)
        sections.append(section.model_copy(update={"claims": claims}))
    return briefing.model_copy(update={"sections": sections})


def _sparse(briefing: Briefing) -> Briefing:
    """The graceful-degradation path: thin evidence, implications suppressed.

    This is what a rare disease looks like. "Three Phase I trials and no
    guidelines" is a useful answer; a fluent page implying more is known than
    is known is not.
    """
    thin = {
        "record_count": 3,
        "guideline_count": 0,
        "trial_count": 3,
        "late_phase_trial_count": 0,
        "newest_year": 2024,
        "is_sparse": True,
        "rationale": "Three early-phase trials, no guidelines and no approved label.",
    }
    sections = []
    for section in briefing.sections:
        if section.kind.value == "implications":
            continue  # suppressed, not emptied
        density = section.density.model_copy(update=thin) if section.density else None
        sections.append(
            section.model_copy(
                update={
                    "density": density,
                    "sparse_notice": (
                        "Evidence for this section is thin: three early-phase trials, no "
                        "society guideline and no approved label. Treat everything below "
                        "as preliminary."
                    ),
                }
            )
        )
    return briefing.model_copy(
        update={
            "sections": sections,
            "implications_suppressed": True,
            "suppression_reason": (
                "Below the evidence-density threshold. Strategic implications were not "
                "generated rather than speculated past what three early-phase trials show."
            ),
        }
    )


def variants() -> list[Variant]:
    good = load_briefing()
    base = RenderOptions(today=TODAY)
    return [
        Variant(
            "good",
            "Valid briefing",
            "The reference rendering. Two tiers, every evidence claim one click from its "
            "primary source, nothing flagged.",
            good,
            base,
        ),
        Variant(
            "broken",
            "Four seeded defects",
            "Defective claims are rendered in place and flagged. Two of the four defects "
            "are visible from the briefing alone; the other two need the corpus and are "
            "named in the scope note.",
            load_broken_briefing(),
            base,
        ),
        Variant(
            "judged",
            "With verification verdicts",
            "Every Claim.judge disposition at once: supported, partial, not supported, "
            "span irrelevant.",
            _judged(good),
            base,
        ),
        Variant(
            "sparse",
            "Sparse evidence, implications suppressed",
            "The rare-disease path. Sparse notices per section and no analysis tier at all.",
            _sparse(good),
            base,
        ),
        Variant(
            "aging",
            "Aging (45 days)",
            "Past the 30-day mark. The as-of block changes state rather than staying quietly "
            "reassuring.",
            good,
            RenderOptions(today=TODAY + timedelta(days=45)),
        ),
        Variant(
            "stale",
            "Stale (150 days)",
            "Past the freshness budget. In oncology this briefing is now misleading.",
            good,
            RenderOptions(today=TODAY + timedelta(days=150)),
        ),
        Variant(
            "feedback",
            "With Layer-3 rating capture",
            "The analyst usefulness widget, rendered only when a feedback endpoint is configured.",
            good,
            RenderOptions(today=TODAY, feedback_endpoint="/api/feedback/rating"),
        ),
    ]


_INDEX = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Briefing surface &mdash; preview</title>
<style>{css}
.sheet {{ max-width: 780px; }}
.v {{ padding: .85rem 0; border-top: 1px solid var(--rule); }}
.v a {{ font-weight: 600; font-size: 1.05rem; }}
.v p {{ margin: .2rem 0 0; color: var(--ink-soft); font-size: .9rem; }}
.v code {{ font-size: .75rem; color: var(--ink-faint); }}
</style></head><body><main class="sheet">
<header class="masthead"><p class="masthead__kicker">Stream C &middot; surface</p>
<h1>Briefing surface preview</h1>
<p class="masthead__meta">every state the renderer has to handle &middot;
rendered {today} &middot; no API calls</p></header>
{rows}
<footer class="colophon"><p>Regenerate with
<code>uv run python web/preview.py</code>. Every variant is derived from a committed
fixture in <code>tests/fixtures/</code>, never hand-built.</p></footer>
</main></body></html>
"""


def build(out_dir: Path = OUT_DIR) -> Path:
    """Render every variant plus an index. Returns the index path."""
    from app.render import stylesheet

    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for v in variants():
        path = out_dir / f"{v.slug}.html"
        path.write_text(render_briefing(v.briefing, v.options), encoding="utf-8")
        report = inspect(v.briefing, today=v.options.resolved_today())
        rows.append(
            f'<div class="v"><a href="{v.slug}.html">{v.title}</a>'
            f"<p>{v.why}</p>"
            f"<code>{report.claim_count} claims &middot; {report.citation_count} citations "
            f"&middot; {len(report.errors)} errors &middot; {len(report.warnings)} warnings "
            f"&middot; {len(report.notices)} notices</code></div>"
        )
    index = out_dir / "index.html"
    index.write_text(
        _INDEX.format(css=stylesheet(), today=TODAY.isoformat(), rows="\n".join(rows)),
        encoding="utf-8",
    )
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--out", type=Path, default=OUT_DIR, help="output directory")
    args = parser.parse_args()
    index = build(args.out)
    print(f"{len(variants())} variants -> {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
