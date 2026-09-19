"""The renderer's isolation, asserted rather than assumed.

Stream C's whole claim is that the surface is a pure function of ``Briefing``:
no corpus, no API calls, no cooperation from the other two streams. That is
what lets the UX iterate against already-generated briefings for free. It is
also exactly the kind of property that erodes the first time someone reaches
for a corpus lookup to fill in one missing field, so it is tested.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.render import build_view
from app.render.viewmodel import RenderOptions, slug

RENDER_PKG = Path(__file__).resolve().parents[2] / "app" / "render"

#: Packages owned by the other two streams. The renderer must not import them.
FORBIDDEN_PREFIXES = (
    "app.sources",
    "app.corpus",
    "app.resolve",
    "app.rank",
    "app.synthesize",
    "app.verify",
    "app.api",
    "app.pipeline",
    "app.cli",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.parametrize("path", sorted(RENDER_PKG.rglob("*.py")), ids=lambda p: p.name)
def test_render_imports_no_other_stream(path: Path):
    for module in _imported_modules(path):
        assert not module.startswith(FORBIDDEN_PREFIXES), (
            f"{path.name} imports {module}; app/render must depend on app.models alone"
        )


@pytest.mark.parametrize("path", sorted(RENDER_PKG.rglob("*.py")), ids=lambda p: p.name)
def test_render_makes_no_network_calls(path: Path):
    for module in _imported_modules(path):
        root = module.split(".")[0]
        assert root not in {"httpx", "requests", "urllib", "anthropic", "socket"}, (
            f"{path.name} imports {module}; the surface costs nothing to run and stays that way"
        )


def test_the_view_is_derived_from_the_briefing_alone(briefing):
    """Every source a claim cites must resolve out of ``briefing.sources``.

    The contract test ``test_renderer_needs_no_corpus_access`` guarantees the
    fixture satisfies this; this asserts the renderer actually relies on that
    guarantee rather than reaching elsewhere.
    """
    view = build_view(briefing, RenderOptions(today=briefing.as_of))
    chips = [c for s in view.sections for cl in s.claims for c in cl.chips]
    assert chips
    assert not [c for c in chips if c.is_dangling]
    assert all(c.href.startswith("https://") for c in chips)


def test_building_a_view_twice_gives_equal_values(briefing):
    options = RenderOptions(today=briefing.as_of)
    assert build_view(briefing, options) == build_view(briefing, options)


def test_building_a_view_does_not_mutate_the_briefing(briefing):
    before = briefing.model_dump_json()
    build_view(briefing, RenderOptions(today=briefing.as_of))
    assert briefing.model_dump_json() == before


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("clinicaltrials.gov:NCT07285239", "clinicaltrials-gov-nct07285239"),
        ("pubmed:41494138", "pubmed-41494138"),
        ("soc-1", "soc-1"),
        ("  ", "x"),
    ],
)
def test_slug_is_stable_and_id_safe(raw, expected):
    assert slug(raw) == expected


def test_source_labels_read_the_way_a_clinician_names_them(briefing):
    """ "NCT07285239", "PMID 41494138", "TECVAYLI label" — the handle a reader
    would use out loud, not an opaque internal id."""
    view = build_view(briefing, RenderOptions(today=briefing.as_of))
    labels = {s.source_id: s.label for g in view.source_groups for s in g.sources}
    for source_id, ref in briefing.sources.items():
        label = labels[source_id]
        match ref.source_type.value:
            case "trial":
                assert label.startswith("NCT")
            case "literature":
                assert label.startswith("PMID ")
            case "drug_label":
                assert label.endswith("label")
