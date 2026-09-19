# Workstreams

Three streams build in parallel in git worktrees off one repo. This document is
the whole coordination protocol. Read your stream's brief, then start.

## The two rules that make this converge

**1. `app/models.py` is frozen and single-owner.** Every stream imports it; no
stream edits it. Add whatever private types you like *inside your own package*.
If you genuinely need a change to the shared contract, ask the contract owner —
a unilateral edit breaks the other two streams silently.

**2. No two streams write the same file.** Ownership is by directory and it is
disjoint. Merges are then trivial by construction — there is nothing to
conflict over. `pyproject.toml`, `Makefile`, `tests/conftest.py`,
`tests/fixtures/**` and `tests/contract/**` are contract-owned; if you need a
dependency that is not already pinned, ask rather than adding it.

## Ownership map

| Stream | Owns (writes) | Reads | External deps |
|---|---|---|---|
| **A — Retrieval** | `app/sources/**`, `app/corpus/**`, `app/resolve/**`, `tests/sources/**`, `tests/resolve/**` | contract, fixtures | CT.gov, NCBI, iCite, openFDA |
| **B — Intelligence** | `app/rank/**`, `app/synthesize/**`, `app/verify/**`, `tests/rank/**`, `tests/synthesize/**`, `tests/verify/**` | contract, fixtures | Anthropic API |
| **C — Surface & proof** | `app/render/**`, `web/**`, `evals/**`, `tests/render/**` | contract, fixtures | none (evals: Anthropic) |
| *Integration* | `app/api/**`, `app/pipeline.py`, `app/cli.py`, `Dockerfile`, compose | everything | — |

## Fixtures: build against these, not against each other

Nobody waits for anybody. `tests/fixtures/` holds **real data captured from
live APIs**, committed:

- `raw/*.json` — verbatim upstream responses (CT.gov, PubMed+iCite, openFDA)
- `records/*.json` — those responses parsed into `SourceRecord`
- `briefing__multiple_myeloma.json` — a valid `Briefing` whose citation offsets
  genuinely slice back to the real record text
- `briefing__broken.json` — the same briefing with **four seeded defects**

Load them through `tests/fixtures/__init__.py`:

```python
from tests.fixtures import load_corpus, load_briefing, load_broken_briefing, load_raw

corpus   = load_corpus("multiple_myeloma")   # list[SourceRecord]
briefing = load_briefing()                   # Briefing
```

Two conditions are captured on purpose: **multiple myeloma** (fast-moving
oncology, crowded late-phase pipeline) and **heart failure** (stable
cardiology, dense guidelines, and — importantly — recent major guidelines with
*zero* citations). They exercise different ranking behaviour.

**Do not hand-build `SourceRecord` or `Briefing` objects in your tests.** If
each stream invents its own test data, all three suites pass and none of the
code integrates.

## Working in your worktree

```bash
cd ~/cb-<stream>          # your worktree; you are already on your branch
uv sync --extra dev
make check                # lint + contract tests + unit tests — must be green
git commit -am "..."      # commit to your branch; do not merge to main
```

`make check` must pass before you hand work back. `make contract` alone runs the
16 contract tests — if those fail, you have drifted from the shared contract.

## Definition of done, every stream

- Public functions typed, returning contract types.
- Unit tests against the shared fixtures; `make check` green.
- Network-hitting tests marked `@pytest.mark.network`; billed Anthropic calls
  marked `@pytest.mark.billed`. Neither runs in `make check`.
- No edits outside your owned directories.
- A short `NOTES-<stream>.md` in your worktree root: what you assumed, what you
  deferred, anything you want the contract owner to change.

---

# Stream A — Retrieval

**You own:** `app/sources/**`, `app/corpus/**`, `app/resolve/**`

Build the deterministic retrieval layer. No LLM calls anywhere in this stream
except one narrow case in `resolve/` (below). This is the layer that makes the
whole system's anti-fabrication claim true: facts are pulled from primary
registries by ordinary HTTP code and frozen **before any model call happens**.

### A1 — `app/sources/`

One module per source, all producing `SourceRecord`:

| Module | Source | Notes |
|---|---|---|
| `base.py` | — | Shared async client: httpx, retry via tenacity, rate limiting |
| `ct_gov.py` | ClinicalTrials.gov v2 | No auth. Paginate via `nextPageToken` |
| `pubmed.py` | NCBI E-utilities | **3 req/s without an API key**, 10 with. esearch → esummary → efetch (abstracts, XML) |
| `icite.py` | NIH iCite | Enriches `LiteratureMeta` in place. Batch PMIDs |
| `openfda.py` | openFDA drug label | Also device 510(k)/PMA where relevant |
| `guidelines.py` | Open guideline bodies | Anthropic `web_search_20260209` with `allowed_domains`, then `web_fetch_20260209` |

**Your target is executable, not a matter of taste.** `tests/fixtures/_capture.py`
contains a reference parser for CT.gov, PubMed+iCite and openFDA. Your
production parsers must turn `fixtures/raw/*.json` into records equal to
`fixtures/records/*.json`. Write that as your first test — it is the spec.

Critical details, learned from live probes:

- **`SourceRecord.text` is the citable document text.** Every citation's
  character offsets index into it, so its construction is part of the contract.
  Match `_render_*_text()` in the capture script exactly. Structured fields are
  spelled out as prose-like lines (`Status: RECRUITING`) specifically so a model
  can cite a span that carries a verifiable fact.
- **`LiteratureMeta.rcr is None` means "not yet accrued", never "zero impact".**
  Never coerce it to `0.0`. Stream B's age-tiering depends on the distinction,
  and a contract test guards it.
- **Guidelines get co-published** across journals (the 2026 AHA/ACC/ADA/ASN
  guideline appears in both JACC and Circulation). Populate
  `LiteratureMeta.copublication_of` when you can detect it; Stream B collapses
  them.
- **Licensing boundary:** NCCN, UpToDate and Elsevier are not licensed. Do not
  scrape or quote them. Link the public landing page instead.

### A2 — `app/corpus/`

Immutable per-run snapshot store. SQLite + JSON blobs on a volume, Postgres-ready.

- `CorpusSnapshot` in, `CorpusSnapshot` out, keyed by `run_id`.
- Snapshots are **immutable** — a briefing must be regenerable and auditable
  against exactly the evidence it was built from, months later.
- Compute `FieldVelocity` (trials and publications per year over ~5 years) and
  derive `half_life_months` from it. Stream B consumes this for
  condition-adaptive recency; oncology should come out materially faster than
  heart failure, and that contrast is worth asserting in a test.

### A3 — `app/resolve/`

Free text → `ConditionProfile`. This is where retrieval recall is won or lost:
a literal query for "heart attack" finds almost nothing, while the MeSH
descriptor *Myocardial Infarction* plus entry terms plus ICD-10 I21–I22 finds
the field.

- MeSH descriptor + entry terms via E-utilities (`db=mesh`).
- ICD-10 codes; SNOMED only if a UMLS licence is obtained.
- **The one LLM call in this stream:** ask Claude for colloquial and trade
  synonyms, then **validate every suggestion against MeSH/RxNorm**. Validated
  ones go in `synonyms`; the rest go in `unvalidated_synonyms` and are shown to
  the analyst but never used for retrieval.
- Expect over-retrieval and do not try to fix it here. A live probe for heart
  failure guidelines returned a paediatric leukaemia cardiotoxicity paper as the
  top hit — technically MeSH-linked, not a heart failure guideline. Precision is
  recovered by the analyst review screen and by Stream B's ranking, not by
  narrowing the expansion.

---

# Stream B — Intelligence

**You own:** `app/rank/**`, `app/synthesize/**`, `app/verify/**`

The hardest judgment work in the system. Everything you need already exists in
fixtures — you never wait for Stream A.

### B1 — `app/rank/`

Deterministic scoring. `list[SourceRecord] + FieldVelocity → list[RecordScore]`.
No model calls. Weights in config, not code.

**Credibility signals:**

| Signal | Where from |
|---|---|
| Publication-type hierarchy | `LiteratureMeta.pub_types` — Practice Guideline > Meta-Analysis > Systematic Review > RCT > Observational > Case Report |
| Peer-reviewed vs preprint | `is_preprint` — preprints admitted but capped, never sole support for a claim |
| Journal standing | `medline_indexed`; SJR quartile. **Journal Impact Factor is Clarivate-licensed — out of scope** |
| Replication / influence | iCite `rcr`, `clinical_citation_count` |
| Corroboration | Appears in a systematic review or meta-analysis in corpus |
| Translation to practice | Literature linkable to a registered trial by intervention name |

Trials score on phase, enrollment, status, sponsor class, `has_results`.

**The age tier is the part to get right.** A naive credibility × recency blend
fails, and the fixtures prove it: the 2026 CCS acute heart failure guideline has
`rcr=None` and zero citations because it is months old, not because it is weak.

- Under ~18 months → `ScoreTier.PROVENANCE_ONLY`. Citation metrics are
  **ignored**, not scored as zero.
- Over ~18 months → `PROVENANCE_AND_IMPACT`. RCR and clinical citations activate.

A recent major-society guideline must never be outranked by an older one for
want of citations it has not had time to accrue. Assert that on the heart
failure fixture.

Also: collapse co-publications (`copublication_of`, or detect by title) into one
logical source with multiple URLs, setting `superseded_by`. Otherwise one
guideline double-counts as corroboration.

Recency decay half-life comes from `FieldVelocity`, so oncology discounts old
evidence harder than stable cardiology.

Populate `RecordScore.components` fully — an analyst asking "why did this rank
here" must get a real answer.

### B2 — `app/synthesize/`

Selected records → cited prose, via the **Citations API**. This is the mechanism
that makes the anti-fabrication claim structural rather than aspirational.

- Records go in as `document` content blocks with `citations: {enabled: true}`.
- Responses split into text blocks; cited blocks carry a `citations` array with
  `cited_text` and character offsets. Map offsets back to `source_id` and emit
  contract `Citation` objects, setting `text_sha` from the record.
- **Citations is incompatible with `output_config.format`** — returns 400. So
  structure comes from orchestration (one call per section), not a JSON schema.
  Whether tool use with `strict: true` coexists with citations is undocumented:
  test it, do not assume.
- **Citations are all-or-none per request** — every document block must enable it.
- **Prompt-cache the corpus prefix** across section calls; it is 150–300K tokens
  reused 6+ times. Verify via `usage.cache_read_input_tokens` — if it is zero,
  something volatile leaked into the prefix. Assert non-zero in a test.
- Model `claude-opus-5`, adaptive thinking, streaming, `output_config: {effort: "high"}`.

Also compute `EvidenceDensity` per section and set `sparse_notice` below
threshold. Sparse means the section says so plainly — "three Phase I trials and
no guidelines" is a useful answer; a fluent page implying more is known than is
known is not. When sparse, the implications section is **suppressed**, not asked
to speculate.

### B3 — `app/verify/`

Two layers. Both ship.

**Layer 1 — deterministic, no model, must pass:**

1. Every citation resolves to a real corpus record, and `record.text[start:end]`
   equals `cited_text`.
2. Every NCT ID in prose exists in the corpus.
3. Every drug and company named appears in the corpus — use `SourceRecord.entities()`.
4. Trial phase and status in prose match the CT.gov fields at snapshot time.
   This is what catches "terminated trial described as promising".

`tests/fixtures/briefing__broken.json` seeds one defect for each. **Your gates
must fail on it and pass on the good fixture.** A verifier that passes both is
not a verifier.

**Layer 2 — LLM-as-judge:**

| Decision | Choice |
|---|---|
| Model | `claude-opus-5`, `effort: high` — judging with a weaker model than the generator is a known anti-pattern |
| Input | The claim **and its cited span only** — not the corpus, not the surrounding briefing. Minimises anchoring on fluent context |
| Framing | Adversarial: *find the reason this claim is not supported* |
| Output | `strict: true` tool schema → `JudgeResult` |
| Independence | One call per claim; no batching that lets one verdict contaminate another |
| Disposition | `NOT_SUPPORTED` / `SPAN_IRRELEVANT` → strip. `PARTIAL` → flag for analyst review, never silently pass |

Ship a **calibration harness**: score the judge against human-labelled
claim/span pairs and report accuracy and Cohen's κ. Until that runs, the judge
is advisory. An uncalibrated judge is not a control — it is a second opinion
from the same model family, and self-preference bias is well documented. The
labelled set is being curated; build the harness and a loader for it now.

---

# Stream C — Surface & proof

**You own:** `app/render/**`, `web/**`, `evals/**`

Zero coupling to retrieval, ranking or synthesis. You consume `Briefing` JSON
and nothing else, which means you can iterate freely without regenerating
anything — and without spending a cent on API calls.

### C1 — `app/render/` + `web/`

`Briefing` → HTML. Jinja2 is pinned.

The design job is communicating **two tiers of epistemic status** at a glance:

- **Evidence** — every claim carries a citation, hyperlinked to its primary
  source. A reader must be able to get from any sentence to the NCT record or
  PMID that supports it in one click.
- **Implications** — labeled analysis, visually and structurally distinct.
  Valuable, and *not* sourced fact. Never let the two blur; that separation is
  what makes the briefing defensible to whoever asks where a number came from.

Also render: the `as_of` date prominently (briefings go stale), evidence-density
and sparse notices, per-claim verdict state where `Claim.judge` is populated, and
a sources appendix from `briefing.sources`.

Expect the UX to iterate — the strict `Briefing` contract exists so it can, so
keep template logic thin and put nothing stream-specific into the contract.
Render `briefing__broken.json` too: defective claims must be visibly flagged,
not silently dropped.

### C2 — `evals/`

Three layers. Only Layer 1 blocks CI.

**Layer 1 — deterministic gates (blocking).** Citation validity 100 %,
staleness errors 0, unsupported entities 0. Run against the gold set in CI.
Implementation lives in Stream B's `app/verify/`; you own the *harness*, the
gold-set loader and the CI wiring. Until B lands, drive it off the fixtures.

**Layer 2 — RAG metrics, tracked not gated.** DeepEval as the harness (it treats
evals as pytest tests, which is the shape CI needs), RAGAS metric definitions as
the metric set. It is pinned under the `evals` extra: `uv sync --extra evals`.

This is not classic embedding RAG — retrieval is deterministic API querying — so
the metrics map as:

| Metric | Means here |
|---|---|
| Context recall | Did retrieval find the expert must-include trials/therapies? **The metric that matters most** |
| Context precision | Did ranking put credible, current evidence on top? |
| Faithfulness | Cross-check only — the Citations API enforces this structurally. Disagreement with Layer 1 is a bug signal |
| Answer relevancy | Does each section answer its own question |
| Instruction following | Evidence/implications separation held; sparse path honoured |

Every Layer-2 metric is itself LLM-judged and will drift. Track and hill-climb
them; never gate CI on them.

**Layer 3 — human.** Analyst usefulness rating (1–5) and clinician spot-checks.
Build the capture path and storage.

**Gold set** — five conditions spanning therapeutic areas *and* evidence
densities: HFrEF, multiple myeloma, sickle cell disease, MASH/NASH, and one rare
disease (Duchenne or a lysosomal storage disorder) to exercise graceful
degradation. Two are already in fixtures. Build the loader and schema now; a
clinician is curating the must-include lists separately, so define the format
they will fill in and make it boring to fill.
