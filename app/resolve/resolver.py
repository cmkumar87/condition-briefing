"""Free text -> ConditionProfile.

The one place in Stream A that touches a model, and it is deliberately fenced:
the model may only *suggest* expansion terms. Every suggestion is then checked
against a controlled vocabulary, and anything unrecognised is shown to the
analyst rather than silently queried. A hallucinated synonym that reached
retrieval would quietly skew the whole corpus, and no downstream gate would
catch it — the resulting briefing would be perfectly cited and subtly about the
wrong thing.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field

from app.models import ConditionProfile
from app.resolve.mesh import MeshClient
from app.resolve.vocab import ICD10Client, RxNormClient

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

#: Synonym generation is a small, well-specified task. Low effort keeps it cheap
#: without measurably hurting quality; validation is what guarantees correctness
#: here, not model capability.
EFFORT = "low"

MAX_SUGGESTIONS = 12

SYNONYM_PROMPT = """\
List alternative names for the medical condition below, as a clinician or a \
clinical trial registry might record it.

Condition: {query}
{context}

Include: lay and colloquial names, common abbreviations and acronyms, historical \
or eponymous names, and major clinical subtypes.
Exclude: treatments, drug names, unrelated conditions, and broader categories \
that would pull in different diseases.

Return at most {limit} terms. Prefer precision — a term that retrieves a \
different disease is worse than a term omitted."""


class SynonymSuggestions(BaseModel):
    terms: list[str] = Field(default_factory=list, max_length=40)


class ConditionResolver:
    def __init__(
        self,
        mesh: MeshClient,
        icd10: ICD10Client,
        rxnorm: RxNormClient,
        anthropic_client: object | None = None,
        model: str = MODEL,
    ) -> None:
        self._mesh = mesh
        self._icd10 = icd10
        self._rxnorm = rxnorm
        self._anthropic = anthropic_client
        self._model = model

    async def resolve(self, query: str, *, suggest_synonyms: bool = True) -> ConditionProfile:
        concept = await self._mesh.resolve(query)
        descriptor = concept.descriptor if concept else None
        entry_terms = concept.entry_terms if concept else []

        icd10_codes = await self._icd10.codes_for(descriptor or query)

        validated: list[str] = []
        unvalidated: list[str] = []
        if suggest_synonyms and self._anthropic is not None:
            suggestions = await self._suggest(query, concept.scope_note if concept else None)
            validated, unvalidated = await self._partition(suggestions, descriptor, entry_terms)

        return ConditionProfile(
            query=query,
            mesh_descriptor=descriptor,
            mesh_ui=concept.ui if concept else None,
            entry_terms=entry_terms,
            icd10_codes=icd10_codes,
            synonyms=validated,
            unvalidated_synonyms=unvalidated,
            # Never true out of this function. A human sets it, and retrieval
            # for a real briefing should wait for that.
            analyst_reviewed=False,
        )

    # -- internals ---------------------------------------------------------

    async def _suggest(self, query: str, scope_note: str | None) -> list[str]:
        context = f"\nMeSH scope note: {scope_note}" if scope_note else ""
        prompt = SYNONYM_PROMPT.format(query=query, context=context, limit=MAX_SUGGESTIONS)

        def _call() -> SynonymSuggestions:
            return self._anthropic.messages.parse(
                model=self._model,
                max_tokens=1024,
                output_config={"format": SynonymSuggestions, "effort": EFFORT},
                messages=[{"role": "user", "content": prompt}],
            ).parsed_output

        try:
            parsed = await asyncio.to_thread(_call)
        except Exception:
            # Suggestions are an enhancement. MeSH entry terms already carry the
            # expansion that matters, so a model outage degrades recall slightly
            # rather than failing the run.
            log.warning("synonym suggestion failed; continuing with MeSH terms only", exc_info=True)
            return []
        return (parsed.terms if parsed else [])[:MAX_SUGGESTIONS]

    async def _partition(
        self, suggestions: list[str], descriptor: str | None, entry_terms: list[str]
    ) -> tuple[list[str], list[str]]:
        """Split suggestions into vocabulary-backed and merely-plausible."""
        known = {t.strip().casefold() for t in [descriptor, *entry_terms] if t}

        pending: list[str] = []
        validated: list[str] = []
        for term in suggestions:
            cleaned = term.strip()
            if not cleaned or cleaned.casefold() in known:
                continue  # already covered by MeSH; not a new term
            pending.append(cleaned)

        if pending:
            # RxNorm is the fallback check: it catches trade and generic names
            # that MeSH does not index as condition entry terms.
            results = await asyncio.gather(
                *(self._rxnorm.is_known_drug(t) for t in pending), return_exceptions=True
            )
            unvalidated: list[str] = []
            for term, ok in zip(pending, results, strict=True):
                (validated if ok is True else unvalidated).append(term)
            return validated, unvalidated
        return validated, []
