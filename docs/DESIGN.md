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
- **Tier 1 — path -> component -> constraints.** A glob resolver maps a file path to every
  component whose globs match it — matches are additive, so a broad and a narrow component both
  apply (the fail-safe reading for `must` rules); specificity (exact path, literal glob
  characters, `priority`) only orders them — then pulls those components' constraints plus one
  hop along the same repo's dependency graph (undirected, any kind). This answers "what rules
  apply to *this* file?" **before** an edit, with no model in the loop. It is the
  highest-value, lowest-variance part of the system, and it is entirely hand-authored (see the
  example seed).
- **Tier 2 — hybrid search.** For everything not covered by the map. Reciprocal Rank Fusion of
  two legs:
  - *dense*: bge-m3 cosine over `pgvector` HNSW;
  - *lexical*: a composite `tsvector` = `<lang>` (stemmed) `|| simple` (exact tokens). The
    `simple` half is what makes code identifiers, IDs, and non-English terms findable — the
    stemmer alone mangles them.

  RRF (`1/(60+rank)`) needs no score calibration between the legs. A cross-encoder reranker is
  an optional final pass over the fused top-N. It once moved recall@1 by +0.16 on a private
  58-query set — the set it was tuned on, measured when the reranker saw only the first 512
  characters of each chunk (now 3000). The repo ships no doc-retrieval eval to reproduce that,
  so measure it on your corpus.

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

## Trust

Who may read a memory is decided inside Postgres, by row-level security built on one rule
(`mem.may_read`): your own rows, plus rows shared with a project you are a member of. What that
rule is worth depends on where the reader's identity comes from, and there are two answers.

- **Shared login (`hm_app`) — a filter, not a boundary between people.** Every person and job
  connects with the same password and *names itself*: `MEM_AUTHOR` on the client becomes the
  `mem.reader` session setting. Row-level security then protects against buggy and forgetful
  queries — a query that leaves out the rule gets no rows instead of everyone's — but anyone
  who holds the `hm_app` password can set the name to anybody's and read and write as them.
  That is the right mode for one person, or for people who would trust each other with each
  other's memory anyway.
- **Per-author logins — the database enforces identity.** `./hm author add <name>` creates a
  login that `mem.author_roles` maps to one author. For that login `mem.reader()` returns the
  mapped author and ignores the setting, so it cannot read another author's private rows or
  write in their name, whatever it claims. The boundary is exactly the passwords: it holds for
  people who hold only their own login's, and not against anyone who also has the `hm_app` or
  the owner's.

Neither mode covers the **document corpus**. `documents` and `chunks` are shared by design and
writable by every application login, shared or per-author: anyone who can ingest can replace any
repo's documents. The component map (`components`, `constraints`, `relationships`) is read-only
to the application and written by the owner.

The table owner, a superuser and any `BYPASSRLS` role skip every policy. They are for
migrations and restores (`HM_ADMIN_URL`), never for the application; `./hm doctor` reports which
mode a connection is in and fails when the application connects as the owner of a store with
more than one author.

Out of scope: this repository contains no network service that authenticates callers. The MCP
server is a stdio process beside the client, takes its identity from the local environment, and
never lets a tool argument choose it. Serving memory to remote callers means putting your own
authenticated service in front of Postgres — and connecting it with one per-author login per
caller, or it is back to the shared-login filter.

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
- Not a multi-tenant service. Per-author logins make the database enforce who reads which
  memory (see Trust); authenticating remote callers, and controlling who may write the shared
  document corpus, are yours. Put it behind your own network controls before exposing
  Postgres.
