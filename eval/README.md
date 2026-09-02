# Eval

## `mem_probes.py` — personal-memory quality probes

The memory-tier counterpart to a doc-RAG recall@k score. Instead of a number, it asserts the
properties a bi-temporal memory store must have or it is *silently* wrong — the LongMemEval failure
modes, checked mechanically against the **live serving path** (whatever `DATABASE_URL` +
`EMBED_BACKEND` point at). Self-cleaning: probe rows are tagged `metadata.probe=true` and
hard-deleted afterwards.

```bash
DATABASE_URL=... EMBED_BACKEND=ollama python3 eval/mem_probes.py    # exit 1 if any probe fails
```

Probes:

1. **staleness** — a superseded fact must not surface in default search; the new one must; the old
   one stays reachable with `include_inactive`.
2. **abstention** — an unknown-topic query must return nothing, not top-k noise.
3. **temporal** — a fact whose `valid_to` is in the past is hidden by default, visible with
   `include_inactive`.
4. **recall** — a paraphrased query finds a just-written distinctive fact in the top-3.

Run it after any change to the memory retrieval path. It needs an embedder (Ollama/TEI) and `psql`
on `PATH`; point `HM_PYTHON` / `HM_MEM_OPS` at your interpreter / `mem_ops.py` if not the defaults.

### Finding (fixed): abstention vs. the lexical leg

Running this against a populated store surfaced two real weaknesses, both now fixed in
`ingest/mem_ops.py` and worth recording so the design intent survives:

1. **The lexical leg had no relevance floor.** The hybrid query is OR-converted (deliberately, so
   identifiers and unstemmed forms match), and the composite `tsvector` includes a `simple`
   (no-stopword) component. So a query sharing *one* incidental token with a memory — a stopword
   like `на`/`the`, or a content word like `high` — produced a lexical hit that bypassed the
   semantic distance gate entirely. A Russian "recipe for borscht…" returned five unrelated infra
   memories. Fix: a lexical-only hit is kept only if it is also semantically plausible
   (`embedding <=> query < MEM_LEX_MAXDIST`, which defaults to `MEM_SEM_MAXDIST` itself — no slack, since unrelated memories start right above the gate). The lexical leg's job
   is to rescue near-misses of the semantic gate, not to admit strangers. The simple-leg *query*
   also drops stopwords and ≤2-char tokens to cut ranking noise.
2. **The semantic gate was loose for bge-m3.** Measured on a populated store: genuine paraphrase
   recall tops out ~0.45 (8 queries: 0.34–0.45), unrelated queries start ~0.52 (6 queries:
   0.52–0.68). The old default of 0.6 let topically-adjacent-but-unrelated queries through as
   noise. `MEM_SEM_MAXDIST` now defaults to **0.5**, in the measured gap; tune it per embedder with
   the same kind of measurement (`mem_ops nearest` gives the distance).

The abstention probe deliberately uses a query that shares stopwords with typical memories, so it
regresses if either fix is undone.
