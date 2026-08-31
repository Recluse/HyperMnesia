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

### Known finding: abstention vs. the lexical leg

Running this against a populated store surfaced a real weakness worth recording. The distance gate
(`MEM_SEM_MAXDIST`, default 0.6) only gates the **semantic** leg of the hybrid search. The
**lexical** leg (`tsvector`) is OR-converted and includes a `simple` (no-stopword) component, so a
query sharing even a single common token with a memory produces a lexical hit that survives with no
relevance floor — defeating abstention for stopword-heavy queries (e.g. a Russian "recipe for
borscht…" returned unrelated infra memories via `на`/`с`/`и`). Separately, even the semantic gate at
0.6 is loose for bge-m3: measured unrelated queries land ~0.51–0.67 while genuine paraphrases sit
~0.34, so a topically-adjacent-but-unrelated query can slip under 0.6.

The probe here uses a query orthogonal enough to pass at the current settings; closing the gap for
real is a retrieval-tuning task (a lexical relevance floor + revisiting `MEM_SEM_MAXDIST`), to be
measured against recall so it doesn't trade abstention for misses.
