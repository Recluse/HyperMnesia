# Installing HyperMnesia

HyperMnesia is a set of small, boring parts around **one Postgres database**:

- **Postgres 16 + pgvector >= 0.8.2** — the only required datastore (vectors + full-text + the
  structural model all live here). *(0.8.2 fixes a security issue; 0.8.5 fixes HNSW corruption --
  use the newest you can.)*
- **An embedder** producing `bge-m3` (1024-dim) vectors — via **Ollama** (`bge-m3`, GPU/Apple-Silicon
  friendly) or **TEI** (HuggingFace text-embeddings-inference, CPU-capable). Both are cosine-compatible.
- **(Optional) a reranker** — `bge-reranker-v2-m3` cross-encoder for a precision boost on search.
- **(Optional) a small LLM** for distilling session transcripts into memories — any
  OpenAI-compatible endpoint, a local Ollama model, or a CLI like `codex`/`claude`.
- **An MCP client** — Claude Code (also drives the memory hooks), or any Model Context Protocol client.

Pick a deployment below. All four use the same schema and code; they differ only in **where the
Postgres and the embedder run**.

## The short version

If you have Docker and want the single-box stack, the wrapper runs the documented steps in the
order that works:

```bash
./hm init                        # .env with a generated password, compose up, both schemas loaded
export DATABASE_URL=...          # init prints the exact line
./hm ingest ~/code/myrepo myrepo # ingest -> embed -> build the ANN index -> check
./hm doctor                      # any time you suspect the store is answering worse than it should
```

`hm` is not a different way to install anything — it shells the same scripts as the manual steps
below. It exists because the *order* is load-bearing and getting it wrong is silent: the ANN index
must be built after the first bulk embed, and a re-ingest without a known-hashes snapshot deletes
the scope's documents and every embedding with them. `hm ingest` takes that snapshot for you when
the scope already has documents.

Read on for the manual steps, for the non-Docker paths, and for Kubernetes.

## Hardware / OS / software requirements

| | **A. Laptop / local** | **B. Single server** | **C. Kubernetes** | **D. CPU-only minimal** |
|---|---|---|---|---|
| **Use case** | dev, personal, 1 user | homelab / small team | existing cluster, HA-ish | cheapest, no GPU |
| **CPU** | 4+ cores | 4-8 cores | per-node | 2-4 cores |
| **RAM** | 8 GB (16 w/ reranker) | 8-16 GB | 4-8 GB / node | 4 GB |
| **Disk** | ~5 GB + your corpus | ~10 GB | PV ~20 GB | ~3 GB |
| **GPU / accel** | Apple MPS or NVIDIA (nice, not required) | optional | optional | none |
| **OS** | macOS 13+ / Linux | Linux (Docker) | any k8s 1.27+ | Linux |
| **Embedder** | Ollama `bge-m3` | TEI or Ollama (compose) | TEI Deployment | TEI on CPU (~3-5 s/chunk) |
| **Reranker** | local (`rerank/server.py`, ~4 GB RAM when active) | optional compose service | optional Deployment | **off** (RRF only) |
| **Postgres** | docker (pgvector image) | docker-compose | in-cluster, local PV | docker |

> **Bulk embedding is the only heavy step.** On CPU, `bge-m3` is ~3-5 s/chunk; on a GPU or Apple
> Silicon (Ollama) it's ~10-100x  faster. Embed once; queries only embed the (short) query text.
> If you have a GPU/Mac, do bulk embedding there even if you serve from a small CPU box.

---

## A. Laptop / local (Apple Silicon or Linux)

```bash
# 1. Postgres + pgvector (docker) — or a native install with the pgvector extension
docker run -d --name hm-pg -e POSTGRES_PASSWORD=hm -e POSTGRES_DB=hypermnesia \
  -p 5432:5432 pgvector/pgvector:0.8.5-pg16
export DATABASE_URL="postgresql://postgres:hm@localhost:5432/hypermnesia"

# 2. Schema
psql "$DATABASE_URL" -f sql/schema.sql -f sql/schema_mem.sql

# 3. Embedder: Ollama
ollama pull bge-m3            # 1.2 GB
export EMBED_BACKEND=ollama   # http://localhost:11434

# 4. Ingest a repo's markdown, then embed
python ingest/ingest_repo.py ~/code/myrepo myrepo /tmp/myrepo.sql
psql "$DATABASE_URL" -f /tmp/myrepo.sql
python ingest/embed_chunks.py               # fills chunks.embedding

# 5. Build the ANN index — AFTER the first bulk embed, and do not skip it.
#    schema.sql leaves this commented on purpose: building HNSW before the rows exist is far
#    slower than building it once they do. Skip it and search still WORKS, so nothing complains
#    — the dense leg just sequential-scans every vector for the rest of the install's life.
psql "$DATABASE_URL" -c "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw \
  ON chunks USING hnsw (embedding vector_cosine_ops)"

# 6. (optional) reranker on your GPU/Mac
python -m venv rerank/.venv && rerank/.venv/bin/pip install torch transformers sentencepiece
rerank/.venv/bin/python rerank/server.py &  # 127.0.0.1:8091, lazy-loads, idle-unloads
                                            # HM_RERANK_BIND=0.0.0.0 to serve it beyond localhost

# 7. Point your MCP client at mcp-server (see "MCP client" below)

# 8. Check it. Every fault this catches leaves a system that still answers -- an unbuilt index,
#    unembedded chunks, two embedding models in one table, a scope name off by case.
python3 ci/doctor.py
```

## B. Single server (docker-compose) — recommended for a homelab

Everything (Postgres, TEI embedder, optional reranker) in one compose stack:

```bash
cp deploy/docker/.env.example deploy/docker/.env    # set POSTGRES_PASSWORD etc.
docker compose -f deploy/docker/docker-compose.yml up -d          # + --profile rerank for the reranker
docker compose -f deploy/docker/docker-compose.yml exec postgres \
  psql -U hm -d hypermnesia -f /sql/schema.sql -f /sql/schema_mem.sql
```

Then ingest/embed as in A steps 4 and 5 — including the ANN index, which is easy to miss and
costs you a sequential scan on every query if you do. Point the embedder at TEI first, since this
stack has no Ollama: `export EMBED_BACKEND=tei TEI_URL=http://localhost:8080`. See
`deploy/docker/docker-compose.yml` for the services and ports. TEI serves `bge-m3` on CPU; add a
GPU runtime to the compose service for speed.

## C. Kubernetes

The stack is small; adapt the compose services into your own manifests. **[deploy/k8s/README.md](../deploy/k8s/README.md)**
describes the shape: a Postgres+pgvector Deployment with a PVC (pin to a node if the PV is
node-local), a TEI `bge-m3` Deployment, ingest/embed as one-shot Jobs, and the MCP server
running client-side reaching Postgres via a port-forward or routable Service. Load the schema
with `kubectl exec ... psql < sql/schema.sql`.

## D. CPU-only minimal (no GPU, no reranker)

Same as B but skip the reranker (`HM_RERANK` unset -> search returns plain RRF, which already
fuses vector + full-text). Use TEI on CPU for embeddings. This is the cheapest, fully-functional
setup; you lose the reranker's precision bump but nothing else.

---

## MCP client (Claude Code)

Add HyperMnesia's MCP server to your client. For Claude Code, in the repo's `.mcp.json`
(absolute paths; the server shells to `psql` and the bundled python scripts):

```json
{ "mcpServers": { "hypermnesia": {
  "command": "/opt/hypermnesia/mcp-server/target/release/hypermnesia-mcp",
  "env": {
    "HM_REPO": "myrepo",
    "DATABASE_URL": "postgresql://hm:pass@localhost:5432/hypermnesia",
    "EMBED_BACKEND": "ollama",
    "HM_SEARCH":  "/opt/hypermnesia/ingest/search.py",
    "HM_MEM_OPS": "/opt/hypermnesia/ingest/mem_ops.py",
    "HM_RERANK":  "/opt/hypermnesia/rerank/search_reranked.py"
  } } } }
```

Build the server once: `cd mcp-server && cargo build --release`. Needs the `psql` client on
PATH (Tier 0/1 map/constraints/get_document read the DB via `psql "$DATABASE_URL"`); the
`HM_SEARCH`/`HM_MEM_OPS` scripts need `psycopg2` on `HM_PYTHON` (default `python3`). Omit
`HM_RERANK` to skip reranking (plain RRF).

## Personal-memory hooks (optional, Claude Code)

To auto-capture/inject personal memory, register the hooks in Claude Code settings — see
[docs/MEMORY.md](MEMORY.md) for the SessionStart / UserPromptSubmit / SessionEnd wiring and the
`mem_extract` / `mem_consolidate` schedule. Configure the distiller via `HM_LLM_BACKEND` + `HM_LLM_URL`/`HM_LLM_MODEL` (OpenAI-compatible),
`HM_LLM_MODEL` (Ollama), or `HM_LLM_CMD` (a CLI) — see `hooks/_llm.py`. Everything is fail-open: if the store is unreachable, the agent keeps working.

## Configuration reference

| Env | Default | Meaning |
|-----|---------|---------|
| `DATABASE_URL` | — | Postgres connection string |
| `EMBED_BACKEND` | `ollama` | `ollama` or `tei` |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `TEI_URL` | `http://localhost:8080` | TEI endpoint |
| `HM_RERANK` | unset | path to `rerank/search_reranked.py` (unset = RRF only) |
| `HM_RERANK_URL` | `http://127.0.0.1:8091` | reranker service |
| `HM_LLM_BACKEND` | auto | `openai` \| `ollama` \| `cli` (auto: openai if HM_LLM_URL set, else cli if HM_LLM_CMD, else ollama) |
| `HM_LLM_URL`/`HM_LLM_KEY`/`HM_LLM_MODEL` | — | OpenAI-compatible endpoint (memory extraction/consolidation) |
| `HM_LLM_CMD` | — | CLI distiller (prompt appended as arg, text on stdin) |
| `HM_RERANK_BIND` | `127.0.0.1` | address the reranker listens on (the endpoint has no auth) |
| `HM_DB_TIMEOUT_SECS` | `30` | MCP server: ceiling on one `psql` call |
| `HM_SEARCH_TIMEOUT_SECS` | `60` | MCP server: ceiling on one search |
| `HM_MEMOPS_TIMEOUT_SECS` | `60` | MCP server: ceiling on one memory operation |
| `HM_STATUS_TIMEOUT_SECS` | `120` | MCP server: ceiling on `status` (every check inside is itself bounded) |
| `HM_GRAPH_TTL_SECS` | `300` | how long the MCP server may serve a cached component map before re-reading it |
| `HM_DOC_MAX_CHARS` | `60000` | `get_document` cap; past it the text is cut and the cut is announced |
| `HM_REPO` | cwd basename | which ingested scope this workspace is — an **exact**, case-sensitive match |
