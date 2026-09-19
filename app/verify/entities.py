"""Pull the checkable nouns out of generated prose, and out of the corpus.

The unsupported-entity gate needs two things: a set of names a claim asserts,
and a set of names the corpus can vouch for. Both are built here, without a
model, because a fabrication detector that itself runs on a model has the same
failure mode as the thing it is checking.

Extraction is precision-first by design. Missing an invented name costs a
detection that Layer 2 may still catch; flagging a real one wrongly costs
trust in the gate and, with a blocking gate, blocks a correct briefing. So each
pattern keys off a structural marker rather than trying to recognise
"a company" or "a drug" in general:

* NCT IDs and development codes have fixed shapes.
* Brand names are set in capitals in labels and carry over into prose.
* Company names end in a corporate suffix.
* International Nonproprietary Names end in a WHO-assigned stem — ``-mab`` for
  monoclonal antibodies, ``-zomib`` for proteasome inhibitors, ``-gliflozin``
  for SGLT2 inhibitors. The stem list is the one real lexicon in here, and it
  is what catches a plausible-sounding drug that does not exist.

A lowercase invented name with no recognised stem is the known blind spot. It
is recorded in NOTES-intelligence.md rather than papered over.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.models import SourceRecord

NCT_ID = re.compile(r"\bNCT\d{8}\b")

#: Sponsor development codes: AZD0120, LY3437943, BMS-986365.
DEV_CODE = re.compile(r"\b[A-Z]{2,5}-?\d{3,6}\b")

#: Brand names as labels write them. Four characters minimum so ordinary
#: acronyms do not sweep in.
BRAND_LIKE = re.compile(r"\b[A-Z][A-Z0-9]{3,}\b")

_COMPANY_SUFFIX = (
    r"Therapeutics|Pharmaceuticals|Pharmaceutical|Pharma|Biopharma|Biopharmaceuticals|"
    r"Biosciences|Bioscience|Biotech|Biotechnology|Laboratories|Labs|Medicines|Sciences|"
    r"Oncology|Diagnostics|Healthcare|Health|Holdings|Ventures|"
    r"Inc|LLC|Ltd|Limited|Corp|Corporation|Company|GmbH|AG|SA|NV|plc|PLC"
)
COMPANY = re.compile(r"\b((?:[A-Z][\w&'’.-]*\s+){0,4}?(?:" + _COMPANY_SUFFIX + r"))\b(?![\w-])")

#: WHO/USAN nonproprietary stems. Ordered longest-first so -tuzumab reports as
#: the token it is part of rather than fragmenting.
INN_STEMS: tuple[str, ...] = (
    "mafodotin", "deruxtecan", "vedotin", "govitecan", "tansine",
    "gliflozin", "glutide", "gliptin", "lidomide", "tinib", "ciclib", "parib",
    "zomib", "lisib", "rafenib", "denib", "degib", "sartan", "statin", "vastatin",
    "cycline", "floxacin", "conazole", "prazole", "setron", "triptan", "navir",
    "ciclovir", "bactam", "cillin", "micin", "mycin", "fungin", "dipine",
    "poetin", "kinra", "cept", "stim", "tide", "olone", "sone", "mab", "nib",
)  # fmt: skip
_STEM_RE = re.compile(r"\b([a-z][a-z-]{2,}(?:" + "|".join(INN_STEMS) + r"))\b", re.IGNORECASE)

#: Internally capitalised single-token company names: GlaxoSmithKline, AstraZeneca.
CAMEL_NAME = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")

_SEPARATOR = re.compile(r"[,;:()\[\]/]|\s[-\u2013\u2014]\s")
_TOKEN = re.compile(r"[A-Za-z][\w&\u2019'.-]*|&")
#: Lowercase words that may sit inside a proper-noun phrase without breaking it.
_CONNECTORS = frozenset({"of", "for", "the", "de", "van", "von", "&"})
#: Leading words to trim: "The University of Leeds" is not how the corpus spells it.
_DETERMINERS = frozenset({"the", "a", "an", "this", "that", "these", "those", "its"})

#: Capitalised tokens and acronyms that are not names of anything to verify.
STOPWORDS: frozenset[str] = frozenset(
    {
        "fda",
        "nct",
        "pmid",
        "doi",
        "rct",
        "rcts",
        "who",
        "nih",
        "ema",
        "nice",
        "phase",
        "trial",
        "trials",
        "study",
        "studies",
        "guideline",
        "guidelines",
        "hfref",
        "hfpef",
        "hfmref",
        "mrna",
        "dna",
        "car-t",
        "cart",
        "ecog",
        "usa",
        "uk",
        "eu",
        "us",
        "ct",
        "gov",
        "phase3",
        "phase2",
        "phase1",
        "april",
        "august",
    }
)


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip(" .,;:()[]'\"").strip()


def normalize(name: str) -> str:
    """Lowercase, strip a CT.gov intervention-type prefix, collapse whitespace.

    CT.gov writes interventions as ``DRUG: Bortezomib`` and
    ``BIOLOGICAL: AZD0120``; prose writes ``bortezomib``.
    """
    name = re.sub(
        r"^(DRUG|BIOLOGICAL|DEVICE|PROCEDURE|RADIATION|BEHAVIORAL|DIETARY_SUPPLEMENT"
        r"|GENETIC|COMBINATION_PRODUCT|DIAGNOSTIC_TEST|OTHER)\s*:\s*",
        "",
        name.strip(),
        flags=re.IGNORECASE,
    )
    # "Arm A: Belantamab Mafodotin" -> "belantamab mafodotin"
    name = re.sub(r"^Arm\s+[A-Z0-9]+\s*:\s*", "", name, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", name).strip().lower()


def extract_nct_ids(text: str) -> set[str]:
    """NCT identifiers asserted in prose."""
    return set(NCT_ID.findall(text or ""))


def extract_named_entities(text: str) -> set[str]:
    """Drug, brand and company names a claim asserts, by structural shape."""
    text = text or ""
    found: set[str] = set()

    for match in COMPANY.finditer(text):
        name = _clean(match.group(1))
        # A bare suffix ("Health", "Limited") on its own names nobody.
        if " " in name:
            found.add(name)

    for pattern in (DEV_CODE, BRAND_LIKE):
        for token in pattern.findall(text):
            if token.lower() not in STOPWORDS and not NCT_ID.fullmatch(token):
                found.add(token)

    for token in _STEM_RE.findall(text):
        if len(token) >= 6 and token.lower() not in STOPWORDS:
            found.add(token)

    found.update(CAMEL_NAME.findall(text))
    found.update(_proper_noun_phrases(text))

    return {f for f in found if len(f) >= 4}


def _flush_run(run: list[str], phrases: set[str]) -> None:
    while run and run[0].lower() in _DETERMINERS | _CONNECTORS:
        run.pop(0)
    while run and run[-1].lower() in _CONNECTORS:
        run.pop()
    significant = [t for t in run if t[:1].isupper() and t.lower() not in STOPWORDS | _DETERMINERS]
    if len(significant) >= 2:
        cleaned = _clean(" ".join(run))
        if cleaned:
            phrases.add(cleaned)
    run.clear()


def _proper_noun_phrases(text: str) -> set[str]:
    """Multi-word proper nouns: "University of Leeds", "Cancer Research UK".

    Many organisations carry no corporate suffix at all, so the suffix pattern
    alone would let an invented university or research network through. A run
    counts only when it holds at least two capitalised tokens that are not
    ordinary words, which is what keeps sentence openers such as "Bispecific
    and antibody-drug-conjugate regimens" from being read as an organisation.
    """
    phrases: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        run: list[str] = []
        cursor = 0
        for match in _TOKEN.finditer(sentence):
            # A comma or dash between two capitalised tokens separates two
            # organisations; without this, "Cancer Research UK, Blood Cancer UK
            # and Johnson & Johnson" reads as one very long invented company.
            if _SEPARATOR.search(sentence[cursor : match.start()]):
                _flush_run(run, phrases)
            cursor = match.end()
            token = match.group(0)
            if token[:1].isupper():
                run.append(token)
            elif run and token.lower() in _CONNECTORS:
                run.append(token)
            else:
                _flush_run(run, phrases)
        _flush_run(run, phrases)
    return phrases


class CorpusLexicon:
    """What the corpus can vouch for.

    Two tiers: the structured entities each record explicitly names, and the
    full citable text. A name found only in the text is still corroborated by a
    primary source — it was retrieved, not invented — so both count.
    """

    def __init__(self, records: Sequence[SourceRecord]) -> None:
        self.entities: set[str] = set()
        for record in records:
            self.entities.update(normalize(e) for e in record.entities())
            self.entities.add(normalize(record.title))
        self.nct_ids: set[str] = {r.trial.nct_id for r in records if r.trial and r.trial.nct_id}
        self._blob = "\n".join(r.text for r in records).lower()

    def supports(self, name: str) -> bool:
        key = normalize(name)
        if not key:
            return True
        return key in self.entities or key in self._blob

    def unsupported(self, names: Iterable[str]) -> list[str]:
        return sorted(n for n in names if not self.supports(n))
