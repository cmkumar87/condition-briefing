"""Condition resolution, with the model fenced off."""

from __future__ import annotations

from app.resolve.mesh import MeshConcept, parse_concept
from app.resolve.resolver import ConditionResolver

# --------------------------------------------------------------------------
# MeSH parsing
# --------------------------------------------------------------------------


def test_first_mesh_term_is_the_descriptor_and_the_rest_are_entry_terms():
    """This ordering is the whole contract of ds_meshterms."""
    concept = parse_concept(
        {
            "ds_meshui": "D009203",
            "ds_meshterms": [
                "Myocardial Infarction",
                "Infarction, Myocardial",
                "Heart Attack",
                "Heart Attacks",
            ],
            "ds_scopenote": "NECROSIS of the MYOCARDIUM ...",
        }
    )
    assert concept is not None
    assert concept.descriptor == "Myocardial Infarction"
    assert "Heart Attack" in concept.entry_terms
    assert concept.descriptor not in concept.entry_terms
    assert concept.ui == "D009203"


def test_a_record_without_terms_resolves_to_nothing():
    assert parse_concept({"ds_meshui": "D000000", "ds_meshterms": []}) is None


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------


class StubMesh:
    def __init__(self, concept: MeshConcept | None):
        self._concept = concept

    async def resolve(self, query: str):
        return self._concept


class StubICD10:
    def __init__(self, codes: list[str]):
        self._codes = codes

    async def codes_for(self, term: str, *, limit: int = 12):
        return self._codes


class StubRxNorm:
    """Recognises only the names it was told about."""

    def __init__(self, known: set[str]):
        self.known = {k.casefold() for k in known}

    async def is_known_drug(self, name: str) -> bool:
        return name.casefold() in self.known


class StubAnthropic:
    def __init__(self, terms: list[str] | None = None, fail: bool = False):
        self._terms = terms or []
        self._fail = fail
        self.messages = self

    def parse(self, **kwargs):
        if self._fail:
            raise RuntimeError("model unavailable")

        class R:
            parsed_output = type("P", (), {"terms": self._terms})()

        return R()


MI = MeshConcept(
    ui="D009203",
    descriptor="Myocardial Infarction",
    entry_terms=["Heart Attack", "Infarction, Myocardial"],
    scope_note="NECROSIS of the MYOCARDIUM ...",
)


def make_resolver(*, suggestions=None, known_drugs=(), fail=False, concept=MI):
    return ConditionResolver(
        mesh=StubMesh(concept),
        icd10=StubICD10(["I21.9", "I21.4"]),
        rxnorm=StubRxNorm(set(known_drugs)),
        anthropic_client=StubAnthropic(suggestions, fail=fail),
    )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


async def test_expands_a_lay_query_into_the_mesh_concept():
    """The case the whole stage exists for: 'heart attack' finds nothing
    literally, and everything once expanded."""
    profile = await make_resolver(suggestions=[]).resolve("heart attack")

    assert profile.mesh_descriptor == "Myocardial Infarction"
    assert profile.icd10_codes == ["I21.9", "I21.4"]
    terms = profile.search_terms()
    assert "Myocardial Infarction" in terms
    assert "Heart Attack" in terms
    assert terms[0] == "Myocardial Infarction", "descriptor should lead"


async def test_unvalidated_suggestions_never_reach_retrieval():
    """A hallucinated synonym in search_terms() would skew the entire corpus,
    and no downstream gate would catch it."""
    resolver = make_resolver(suggestions=["STEMI", "Cardiac Thunderbolt Syndrome"])
    profile = await resolver.resolve("heart attack")

    assert "Cardiac Thunderbolt Syndrome" in profile.unvalidated_synonyms
    assert "Cardiac Thunderbolt Syndrome" not in profile.synonyms
    assert "Cardiac Thunderbolt Syndrome" not in profile.search_terms()


async def test_vocabulary_backed_suggestions_are_promoted():
    resolver = make_resolver(suggestions=["Teclistamab"], known_drugs={"teclistamab"})
    profile = await resolver.resolve("multiple myeloma")

    assert profile.synonyms == ["Teclistamab"]
    assert "Teclistamab" in profile.search_terms()


async def test_suggestions_already_covered_by_mesh_are_dropped():
    """No point re-listing a term the descriptor already carries."""
    profile = await make_resolver(suggestions=["heart attack", "HEART ATTACK"]).resolve(
        "heart attack"
    )
    assert profile.synonyms == []
    assert profile.unvalidated_synonyms == []


async def test_model_failure_degrades_instead_of_failing_the_run():
    """MeSH entry terms already carry the expansion that matters."""
    profile = await make_resolver(fail=True).resolve("heart attack")

    assert profile.mesh_descriptor == "Myocardial Infarction"
    assert "Heart Attack" in profile.search_terms()
    assert profile.synonyms == []


async def test_a_condition_with_no_mesh_descriptor_still_resolves():
    profile = await make_resolver(concept=None, suggestions=[]).resolve("some novel syndrome")

    assert profile.mesh_descriptor is None
    assert profile.search_terms() == ["some novel syndrome"]


async def test_resolution_never_marks_itself_analyst_reviewed():
    """Only a human sets that flag. Retrieval for a real briefing waits for it."""
    profile = await make_resolver(suggestions=["STEMI"]).resolve("heart attack")
    assert profile.analyst_reviewed is False


async def test_search_terms_are_deduplicated_and_ordered():
    profile = await make_resolver(suggestions=[]).resolve("Myocardial Infarction")
    terms = profile.search_terms()
    assert len(terms) == len(set(terms))
