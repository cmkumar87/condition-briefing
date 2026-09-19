"""Jinja2 environment and the HTML entry point.

``render_briefing(briefing) -> str`` is the whole public surface. It is a pure
function of the ``Briefing`` plus ``RenderOptions``: no corpus, no clock unless
you let it default, no network. That is what lets the UX iterate against
already-generated briefings without regenerating anything.

Autoescaping is on and stays on. Claim text is model-generated and quoted spans
are verbatim upstream registry text; neither is trusted markup.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.models import Briefing
from app.render.integrity import IntegrityReport
from app.render.viewmodel import BriefingView, RenderOptions, build_view

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
CSS_PATH = STATIC_DIR / "briefing.css"

#: Filename used when the caller writes the stylesheet out alongside the HTML.
CSS_FILENAME = "briefing.css"


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """Jinja environment. Cached; templates are read-only at runtime.

    ``StrictUndefined`` is deliberate: a renamed view-model attribute should
    break a test loudly, not render a silently empty briefing.
    """
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(default_for_string=True, default=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


@lru_cache(maxsize=1)
def stylesheet() -> str:
    """The briefing stylesheet, read once."""
    return CSS_PATH.read_text(encoding="utf-8")


def render_view(
    view: BriefingView,
    *,
    inline_css: bool = True,
    css_href: str = CSS_FILENAME,
) -> str:
    """Render an already-built view. Useful when a caller wants to inspect or
    adjust the view model before it becomes HTML."""
    template = _environment().get_template("briefing.html.j2")
    return template.render(
        view=view,
        inline_css=inline_css,
        css=stylesheet() if inline_css else "",
        css_href=css_href,
    )


def render_briefing(
    briefing: Briefing,
    options: RenderOptions | None = None,
    *,
    report: IntegrityReport | None = None,
    css_href: str = CSS_FILENAME,
) -> str:
    """``Briefing`` -> a complete HTML document.

    Pass ``report`` to render against an integrity pass you already ran (the
    eval harness does this to avoid inspecting twice). Otherwise one is
    computed.
    """
    options = options or RenderOptions()
    view = build_view(briefing, options, report=report)
    return render_view(view, inline_css=options.inline_css, css_href=css_href)


def write_briefing(
    briefing: Briefing,
    path: Path | str,
    options: RenderOptions | None = None,
) -> Path:
    """Render to a file, writing the stylesheet beside it when not inlined."""
    options = options or RenderOptions()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_briefing(briefing, options), encoding="utf-8")
    if not options.inline_css:
        (path.parent / CSS_FILENAME).write_text(stylesheet(), encoding="utf-8")
    return path
