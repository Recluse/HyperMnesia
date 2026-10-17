# Running components separately

Each HyperMnesia component has a small network or process contract. This runbook starts and
checks them independently, which is useful for split-host deployments and for diagnosing one
layer without restarting the rest.

Examples assume the repository is `/opt/hypermnesia`. Replace paths and credentials with your
own. Keep Postgres, TEI, the reranker and model servers on loopback or a private network unless
you have added an authenticated boundary.

## 1. Postgres and pgvector

Postgres is the only durable service.

```bash
docker run -d --name hypermnesia-postgres \
  -e POSTGRES_USER=hm \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  -e POSTGRES_DB=hypermnesia \
  -p 127.0.0.1:5432:5432 \
  -v hypermnesia-pgdata:/var/lib/postgresql/data \
  pgvector/pgvector:0.8.5-pg16
```

Wait for `pg_isready`, then apply the schema in order. Every call uses `ON_ERROR_STOP` so a
partially loaded schema cannot look successful.

```bash
export DATABASE_URL="postgresql://hm:$POSTGRES_PASSWORD@127.0.0.1:5432/hypermnesia"
cd /opt/hypermnesia
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v owner="$(id -un)" \
  -f sql/schema.sql \
  -f sql/schema_mem.sql \
  -f sql/mem_multiuser.sql \
  -f sql/migrate_entity_ownership.sql \
  -f sql/migrate_memory_perspectives.sql \
  -f sql/migrate_memory_evidence.sql \
  -f sql/migrate_peer_cards.sql \
  -f sql/migrate_dreamer.sql \
  -f sql/mem_app_role.sql
```

Check it:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "select extversion from pg_extension where extname = 'vector'"
```

For multiple users, finish the scoped application-role setup in
[INSTALL.md#two-people-one-store](INSTALL.md#two-people-one-store) before writing memories.

## 2. Embedder: Ollama

Ollama is the simplest local path, especially on Apple Silicon.

```bash
ollama pull bge-m3
ollama serve
```

Configure HyperMnesia:

```bash
export EMBED_BACKEND=ollama
export EMBED_MODEL=bge-m3
export OLLAMA_URL=http://127.0.0.1:11434
```

Check the exact API HyperMnesia uses:

```bash
curl -fsS http://127.0.0.1:11434/api/embed \
  -H 'content-type: application/json' \
  -d '{"model":"bge-m3","input":["health check"]}'
```

## 3. Embedder: TEI

TEI is convenient on Linux and in containers.

```bash
docker run --rm --name hypermnesia-tei \
  -p 127.0.0.1:8080:80 \
  -v hypermnesia-tei-cache:/data \
  ghcr.io/huggingface/text-embeddings-inference:cpu-1.7 \
  --model-id BAAI/bge-m3 --port 80 --auto-truncate --max-batch-tokens 4096
```

Configure and check it:

```bash
export EMBED_BACKEND=tei
export TEI_URL=http://127.0.0.1:8080
curl -fsS http://127.0.0.1:8080/embed \
  -H 'content-type: application/json' \
  -d '{"inputs":["health check"],"truncate":true}'
```

Ollama and TEI must both serve 1024-dimensional `bge-m3` vectors. Do not change embedding model
inside an existing corpus without re-embedding it.

## 4. Ingest and embed

Install the only non-standard Python dependency used by the core scripts, then ingest one scope:

```bash
python3 -m venv .venv
.venv/bin/pip install psycopg2-binary

./hm ingest /path/to/repository repository-scope
```

`hm ingest` enumerates, hashes and chunks the markdown, writes the scope, embeds only missing
chunks, builds the HNSW index after the first bulk load, and runs diagnostics. Use `--walk` for a
notes folder that Git does not track.

To run the manual pieces for integration work:

```bash
.venv/bin/python ingest/ingest_repo.py /path/to/repository repository-scope /tmp/scope.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f /tmp/scope.sql
.venv/bin/python ingest/embed_chunks.py
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  'CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)'
```

## 5. Search without MCP

This verifies Postgres and the embedder before adding an agent client:

```bash
printf '%s\n' 'deployment architecture' \
  | .venv/bin/python ingest/search.py 5 repository-scope
```

If query embedding is unavailable, search announces lexical-only fallback. That is a degraded
result, not proof that the dense path works.

## 6. Optional reranker

```bash
python3 -m venv rerank/.venv
rerank/.venv/bin/pip install torch transformers sentencepiece
RERANK_IDLE_SEC=600 rerank/.venv/bin/python rerank/server.py
```

The service binds to `127.0.0.1:8091`, lazy-loads on the first request and unloads after the idle
period. Check it:

```bash
curl -fsS http://127.0.0.1:8091/health
curl -fsS http://127.0.0.1:8091/rerank \
  -H 'content-type: application/json' \
  -d '{"query":"memory","docs":["persistent memory","unrelated text"]}'
```

Set `HM_RERANK_URL=http://127.0.0.1:8091` for `rerank/search_reranked.py`. The endpoint has no
authentication. In a container, set `HM_RERANK_BIND=0.0.0.0` only behind network isolation.

## 7. LLM service

The model-backed jobs share one adapter. Choose exactly one mode:

```bash
# Ollama
export HM_LLM_BACKEND=ollama
export HM_LLM_MODEL=qwen2.5:7b
export OLLAMA_URL=http://127.0.0.1:11434

# OpenAI-compatible service, including vLLM
export HM_LLM_BACKEND=openai
export HM_LLM_URL=http://127.0.0.1:8000/v1
export HM_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
export HM_LLM_KEY=local-token

# CLI process
export HM_LLM_BACKEND=cli
export HM_LLM_CMD='your-command --that-reads-stdin'
```

See [LLM_BACKENDS.md](LLM_BACKENDS.md) for vLLM, model sizing and privacy boundaries.

## 8. Background memory jobs

These are ordinary one-shot processes. Run them from cron, systemd, launchd or Kubernetes Jobs;
do not run them in the interactive hook path.

```bash
python3 hooks/mem_extract.py --dry-run --limit 5
python3 hooks/mem_consolidate.py --dry-run
python3 hooks/mem_reflect.py --dry-run
python3 hooks/mem_dream.py --dry-run
python3 hooks/mem_review.py list
```

`mem_extract` needs local transcript files referenced by its queue. Consolidate, reflect and
Dreamer operate on the store and can run wherever Postgres, the embedder and the LLM endpoint are
reachable. Dreamer only creates review proposals; approval is a separate human action.

## 9. MCP server

Build the stdio server:

```bash
cd /opt/hypermnesia/mcp-server
cargo build --release
```

The binary is normally launched by the MCP client, not as a daemon:

```bash
export HM_REPO=repository-scope
export HM_ROOT=/path/to/repository
export HM_SEARCH=/opt/hypermnesia/ingest/search.py
export HM_MEM_OPS=/opt/hypermnesia/ingest/mem_ops.py
export HM_PYTHON=/opt/hypermnesia/.venv/bin/python
/opt/hypermnesia/mcp-server/target/release/hypermnesia-mcp
```

Use the client configuration in [INSTALL.md#MCP-client](INSTALL.md#mcp-client). `HM_REPO` and
`HM_ROOT` belong in a project-scoped descriptor; do not pin one project's values globally.

## 10. Hooks

Hooks are short-lived commands launched by the agent client. Register them; do not daemonize
them. The capture hook writes a queue, profile/recall inject context, and the architecture hook
resolves constraints before an edit. Configuration examples and client caveats are in
[hooks/README.md](../hooks/README.md).

## 11. Console and tray

```bash
cd /opt/hypermnesia/console
cargo build --release
./target/release/hypermnesia-setup
./target/release/hypermnesia-stats
./target/release/hypermnesia --install
```

The console is an operator client. It does not have to run for ingestion, search, MCP or memory
jobs to work.

## Final verification

```bash
cd /opt/hypermnesia
./hm doctor
python3 ci/latency.py
python3 ci/freshness.py /path/to/repository repository-scope
```

Then call `status`, `project_status`, `search_docs` and `memory_search` through the actual MCP
client. Process startup alone does not prove the tools reach the intended store and scope.
