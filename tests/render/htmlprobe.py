"""A very small HTML reader for the render tests.

String matching on rendered HTML makes brittle tests that pass for the wrong
reasons. These helpers assert on the *document tree* — that the implications
section is genuinely an ``<aside>``, that a given claim's block genuinely
contains an anchor to the trial registry — so a test fails when the structure
regresses rather than when the whitespace moves.

Deliberately dependency-free: no parser is pinned in pyproject.toml, and
``html.parser`` is enough for start tags, attributes and element extents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
)


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    #: Raw HTML between this element's start and end tag.
    inner_html: str = ""
    text: str = ""
    children_tags: list[str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def has_class(self, name: str) -> bool:
        return name in self.classes


class _Collector(HTMLParser):
    """Collects every element, recording its extent so inner HTML is exact."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self._source = source
        self.elements: list[Element] = []
        self._open: list[tuple[Element, int]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        element = Element(tag=tag, attrs={k: (v or "") for k, v in attrs})
        self.elements.append(element)
        if self._open:
            self._open[-1][0].children_tags.append(tag)
        if tag not in VOID_ELEMENTS:
            self._open.append((element, self.rawdata.find(">", self.getpos_offset()) + 1))

    def getpos_offset(self) -> int:
        line, col = self.getpos()
        lines = self._source.splitlines(keepends=True)
        return sum(len(x) for x in lines[: line - 1]) + col

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._open) - 1, -1, -1):
            element, start = self._open[i]
            if element.tag == tag:
                element.inner_html = self._source[start : self.getpos_offset()]
                del self._open[i:]
                return

    def handle_data(self, data: str) -> None:
        for element, _ in self._open:
            element.text += data


def parse(html: str) -> list[Element]:
    collector = _Collector(html)
    collector.feed(html)
    collector.close()
    return collector.elements


def by_id(html: str, element_id: str) -> Element | None:
    for element in parse(html):
        if element.attrs.get("id") == element_id:
            return element
    return None


def by_class(html: str, class_name: str) -> list[Element]:
    return [e for e in parse(html) if e.has_class(class_name)]


def hrefs_in(fragment: str) -> list[str]:
    return [e.attrs["href"] for e in parse(fragment) if e.tag == "a" and "href" in e.attrs]


def norm(text: str) -> str:
    """Collapse whitespace, the way a browser does when it lays text out.

    Source-level line breaks inside a sentence are invisible in the rendered
    page, so a test that asserts on rendered wording should not see them
    either.
    """
    return " ".join(text.split())
