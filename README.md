# Condition Briefing Engine

Evidence-grounded briefings on any medical condition for a health system
strategy team: current standard of care, emerging treatments in development,
and the key companies and institutions involved.

## The core idea

**The model never goes looking for facts.** Facts are pulled from primary
registries — ClinicalTrials.gov, PubMed, NIH iCite, openFDA — by ordinary HTTP
code and frozen into a run-scoped corpus *before any model call happens*.
Synthesis then runs through the Anthropic Citations API, which constrains the
model to quoting spans of documents it was handed. A free-roaming research agent
can invent an NCT number; a pipeline that can only cite what it was given cannot.

Verification on top is mostly deterministic — does this NCT exist in the corpus,
does the prose's trial phase match the registry field — so "it doesn't fabricate"
is a CI gate rather than a claim.

Output is two-tiered and never blurred: **Evidence**, where every claim carries a
citation, and **Implications**, labeled analysis that is valuable and is not
presented as sourced fact.

## Status

Phase 0 complete: frozen data contract, live fixtures, contract test suite.
Three streams now build in parallel — see [WORKSTREAMS.md](WORKSTREAMS.md).

## Quick start

```bash
uv sync --extra dev
make check          # lint + 16 contract tests + unit tests
make fixtures       # re-capture from live APIs (contract owner only)
```

## Layout

```
app/models.py     FROZEN shared contract — every stream reads, none writes
app/sources/      retrieval clients (Stream A)
app/corpus/       immutable per-run snapshot store (Stream A)
app/resolve/      condition -> MeSH/ICD-10 expansion (Stream A)
app/rank/         credibility x condition-adaptive recency (Stream B)
app/synthesize/   Citations API synthesis (Stream B)
app/verify/       deterministic gates + LLM-as-judge (Stream B)
app/render/       Briefing -> HTML (Stream C)
evals/            gold set, DeepEval suites, CI gates (Stream C)
tests/fixtures/   real captured data; the convergence substrate
tests/contract/   the convergence guarantee — runs in every worktree
```

Not clinical decision support. Not a medical device. No PHI.
