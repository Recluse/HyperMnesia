# Design

HyperMnesia is deliberately small. The bet is that the hard part of agent memory is not
storage but **delivery** — getting the right context in front of the agent at the right
moment, deterministically where possible. Everything below serves that.

## One store

All of it lives in **one Postgres database** with the `pgvector` extension: dense vectors,
full-text, the structural model, and personal memory. No separate vector DB, graph DB, or
cache. Reasons:

- A personal / small-team memory is not at a scale where a dedicated vector store earns its
  operational cost. Postgres HNSW is plenty, and you get transactions, joins, and one backup.
- Keeping the structural graph, the docs, and their embeddings in the same place lets a single
  query fuse them. The moment they live in three systems you spend your time syncing them.

## The three tiers

Retrieval is layered from **deterministic** to **fuzzy**, cheapest and most reliable first:

- **Tier 0 — project map + global invariants.** A pinned, human-curated `components` tree and
  the `must`/`should` constraints that always apply. Injected on orientation; no search.
- **Tier 1 — path -> component -> constraints.** A glob resolver maps a file path to its
  component (longest-match wins, `priority` breaks ties), then pulls that component's
  constraints plus one hop along the dependency graph. This answers "what rules apply to *this*
  file?" **before** an edit, with no model in the loop. It is the highest-value, lowest-variance
  part of the system, and it is entirely hand-authored (see the example seed).
- **Tier 2 — hybrid search.** For everything not covered by the map. Reciprocal Rank Fusion of
  two legs:
  - *dense*: bge-m3 cosine over `pgvector` HNSW;
  - *lexical*: a composite `tsvector` = `<lang>` (stemmed) `|| simple` (exact tokens). The
    `simple` half is what makes code identifiers, IDs, and non-English terms findable — the
    stemmer alone mangles them.

  RRF (`1/(60+rank)`) needs no score calibration between the legs. A cross-encoder reranker is
  an optional final pass over the fused top-N; on our eval set it moved recall@1 by +0.16.

Why RRF and not a weighted score blend: the two legs produce incomparable scores (cosine
distance vs `ts_rank`). Rank fusion sidesteps calibration and is robust. Two deliberate lexical
choices carry it: OR-convert the tsquery (AND is too strict for recall) and length-normalize
the rank (`ts_rank_cd(...,1)`) so long documents don't dominate.

## Multi-repo

One installation serves many projects. `documents`, `components`, and `constraints` all carry a
`repo` column; search and the map are scoped by it (`HM_REPO`, or the MCP server's working-dir
name). Component slugs are unique per repo, not globally.

## Fail-open, everywhere

Memory is an enhancement, never a gate. The personal-memory hooks return empty and exit 0 on any
error. Doc search degrades gracefully: if the embedder is down it falls back to lexical-only (a slow
embedder can't hang it — short query timeout), and if Postgres is down the MCP tool surfaces the
error rather than blocking. A broken memory path must never stop the agent from working.

## What it is not

- Not a RAG framework — it ingests markdown docs, not arbitrary connectors.
- Not an autonomous agent — it stores and serves; the agent decides.
- Not multi-tenant/auth'd out of the box — it assumes a trusted local/homelab boundary. Put it
  behind your own network controls before exposing Postgres.
