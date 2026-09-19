# Stream A — Retrieval: notes

`make check` green: 16 contract + 63 unit tests. 6 live network tests pass
against the real APIs (`make network`).

Built: `app/sources/{base,text,ct_gov,pubmed,icite,openfda,dedup}.py`,
`app/corpus/{store,velocity}.py`, `app/resolve/{mesh,vocab,resolver}.py`.

---

## 1. FOR STREAM B — a brief instruction that the fixtures cannot support

Stream B's brief says oncology "should come out materially faster than heart
failure, and that contrast is worth asserting in a test." **It cannot be
asserted from the fixtures, and a test that tries will fail.**

The capture ran with `pageSize=5` and filtered to RECRUITING Phase 3 only, so
each condition has 5 trials and 6 publications regardless of how fast the field
actually moves. Even the raw `totalCount` values do not show the contrast
(myeloma 59 vs heart failure 66 recruiting Phase 3 trials; PubMed guideline
counts run the *other* way, 25 vs 81, which is consistent with heart failure
being a dense, stable, guideline-heavy field).

`compute_velocity` therefore returns `DEFAULT_HALF_LIFE_MONTHS` for both fixture
corpora by design — below `MIN_RECORDS_FOR_VELOCITY = 20` it refuses to estimate
rather than emit a confidently wrong number from six records.

**Test `half_life_for_rate()` and `recency_weight()` directly with controlled
rates instead** — see `tests/corpus/test_store.py`, which does exactly that.
Real velocity separation needs a production-sized corpus (~200 records).

## 2. FOR STREAM B — `copublication_of` is populated by a pass, not by a parser

The fixtures have `copublication_of = None` on every record and that is correct:
detection needs to see the whole corpus at once, so it cannot live in a parser
that must be reproducible from one raw response.

Call `app.sources.dedup.detect_copublications(records)` before ranking. On the
heart failure fixture it collapses 6 literature records to 4 logical sources,
correctly picking the most-cited copy as canonical:

- `pubmed:42263157` (Circulation, 8 cites) → `pubmed:42265997` (JACC, 14 cites)
- `pubmed:41791738` (Kidney Int, 3 cites) → `pubmed:41793402` (JACC HF, 5 cites)

## 3. Known wart: `clinical_citation_count` collapses zero to `None`

`len(cited_by_clin or []) or None` in the reference parser turns a genuine zero
into `None`, which is precisely the None-vs-zero conflation flagged as dangerous
elsewhere. Production matches it for byte-parity with the committed fixtures.

It is recoverable — `icite_enriched=True` with `clinical_citation_count=None`
means zero, not unknown — so this is a wart, not a blocker. Proposed fix
(contract owner): re-derive the record fixtures **offline from the committed raw
payloads** so only this field changes and no live-API drift enters the diff.
Deliberately not done mid-flight, since it would move bytes under two running
streams.

## 4. Bug found and fixed: silent-empty CT.gov filter

`aggFilters=phase:23` (concatenated) returns **zero studies with HTTP 200** —
not an error. The correct form is space-separated, `phase:2 3`. The first
implementation had `"".join(phases)` and produced empty result sets with no
exception anywhere.

Only the live test caught it. Query construction is now a separate unit-tested
function (`build_search_params`) with a named regression test, because this API
fails quietly and a typo would ship as blank briefings rather than a stack trace.

## 5. Deliberate: dead trials are retrieved

`DEFAULT_STATUSES` includes TERMINATED, WITHDRAWN and SUSPENDED. Filtering to
live trials is tempting and wrong: the deterministic staleness gate catches
"terminated trial described as promising" by comparing prose against registry
status *in the corpus*, so a trial that was never retrieved cannot be checked.
Excluding dead trials would silently disable the gate protecting against the
most expensive failure mode. Guarded by a test.

## Deferred

- **`app/sources/guidelines.py`** — not built. Needs Anthropic `web_search` /
  `web_fetch`, which makes it the only model-touching source and the only one
  that costs money per run. The four structured sources cover standard of care
  via PubMed `Practice Guideline[pt]` (81 hits for heart failure alone), so this
  is additive rather than blocking.
- **SNOMED CT** — requires UMLS licence registration. MeSH + ICD-10 are live.
- **PubMed/openFDA API keys** — plumbed (`api_key=` raises PubMed 3→10 req/s)
  but unset. Worth obtaining before production-sized runs.
- **Async store** — `CorpusStore` is synchronous stdlib `sqlite3`. Fine for
  local disk; integration should wrap in `asyncio.to_thread` under FastAPI.
