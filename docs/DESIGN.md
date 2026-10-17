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

## Memory is a graph, but recall stays bounded

Free text remains the primary memory representation. Two small graphs add the structure needed
for reasoning without turning the store into an ontology: observer-to-observed perspective edges
say whose point of view a row represents, and evidence-to-derived edges say why a conclusion
exists. Identity nodes are author-owned. The evidence trigger checks the full graph for cycles,
while RLS still decides which endpoints a reader may see.

Recall does not traverse that graph without a ceiling. A fixed depth controls whether the result
contains explicit rows, derived conclusions, direct premises, or deeper ancestry. `auto` chooses
among those same fixed policies from retrieval evidence and reports its reason. The Dreamer can
propose new derived rows, but it cannot approve them; the Dialectic can phrase cited claims, but
it cannot keep a claim whose IDs are absent or inactive. Structure narrows the model's freedom
instead of creating a second autonomous writer.

## Fail-open, everywhere

Memory is an enhancement, never a gate. The personal-memory hooks always exit 0 and never block a
tool call — but they do not answer an outage with silence: an unreachable store is announced once
per outage, because "nothing matched" and "nothing answered" read identically to the agent. Doc
search degrades gracefully: if the embedder is down it falls back to lexical-only and says so, since
a silent half-retrieval makes "few results" read as a gap in the corpus (a slow
embedder can't hang it — short query timeout), and if Postgres is down the MCP tool surfaces the
error rather than blocking. A broken memory path must never stop the agent from working.

## What it is not

- Not a RAG framework — it ingests markdown docs, not arbitrary connectors.
- Not an autonomous agent — it stores and serves; the agent decides.
- Not multi-tenant/auth'd out of the box — it assumes a trusted local/homelab boundary. Put it
  behind your own network controls before exposing Postgres.
