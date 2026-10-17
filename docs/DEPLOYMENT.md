# Deployment architectures

HyperMnesia is a set of independently runnable components around one durable boundary:
Postgres with pgvector. You do not need to put every component on the same machine, and you do
not need an LLM in the request path unless you use a model-backed memory feature.

This page helps choose a topology. Follow [COMPONENTS.md](COMPONENTS.md) to start one component
at a time, [LLM_BACKENDS.md](LLM_BACKENDS.md) to choose the model service, and
[deploy/k8s/README.md](../deploy/k8s/README.md) for Kubernetes.

## What is required

| Component | Required? | State | Can be remote? | Failure behavior |
|---|---:|---|---:|---|
| Postgres 16 + pgvector | yes | all documents, vectors, map, memory | yes | tools cannot read or write memory |
| Ollama or TEI embedder | yes for dense search and memory writes | model cache only | yes | queries fall back to lexical search where supported; new vectors cannot be written |
| HyperMnesia MCP server | yes for agent tools | none | normally runs beside the client | the client has no HyperMnesia tools |
| Ingest and maintenance scripts | yes for initial/update work | small local locks and queues | yes, if they can reach their inputs | the existing index keeps serving but becomes stale |
| Reranker | no | model cache only | yes | search announces the outage and returns plain RRF order |
| LLM endpoint | only for extract, consolidate, reflect, Dreamer and Dialectic | provider-specific cache | yes | jobs retain their inputs or make no destructive decision; `memory_ask` abstains/errors |
| Hooks | no | capture queue and small caches | run on the agent host | tools still work, but delivery is no longer automatic |
| Tray/CLI console | no | local settings only | reads any reachable store | no effect on the data path |

The embedder and the LLM are separate roles. `bge-m3` creates retrieval vectors. An instruct
model turns transcripts into structured memories and powers the reasoning-oriented passes. A
vLLM server is useful for the second role today; it does not replace the configured Ollama/TEI
embedding path without an adapter.

## Topology 1: all local

```text
agent + hooks + MCP + jobs
          │
          ├── Postgres (loopback)
          ├── Ollama / TEI (loopback)
          ├── optional reranker (loopback)
          └── optional Ollama / CLI LLM
```

Best for one developer and for Apple Silicon. The source tree and session transcripts never need
to leave the workstation. Ollama is the simplest supported embedder and LLM service on macOS.
The reranker can use MPS and unload itself after an idle period.

Use [the laptop instructions](INSTALL.md#a-laptop--local-apple-silicon-or-linux).

## Topology 2: one Linux server

```text
workstation: agent + hooks + MCP
                    │
server: Postgres + TEI + optional reranker + optional LLM
```

This keeps client integration and transcript capture on the workstation while moving persistent
storage and model serving to one managed box. The supplied Compose stack starts Postgres, TEI and
the optional reranker. Add Ollama or vLLM as a separate service when the background memory jobs
should also use that box.

Keep services private. The Compose defaults bind to `127.0.0.1`; reach them through SSH tunnels,
a private network, or an authenticated reverse proxy. Postgres contains personal memory, and the
TEI and reranker endpoints do not authenticate requests.

## Topology 3: split compute

```text
small server / cluster: Postgres
GPU or Mac:             bulk embedding + optional reranking + LLM
workstation:             agent + hooks + MCP + query embedding client
```

This is often the most efficient layout. Postgres is small; model weights dominate memory and
compute. Bulk embedding can run on a fast machine and write vectors to a remote Postgres. Query
embedding can use the same model through another compatible Ollama/TEI instance. Keep the model
identity and vector dimension identical; `hm doctor` reports mixed or missing embeddings.

The LLM endpoint does not have to share hardware with the embedder. A GPU Linux host can serve an
instruct model through vLLM while Apple Silicon or TEI serves `bge-m3`.

## Topology 4: Kubernetes data plane, local clients

```text
workstation: agent + hooks + stdio MCP
                    │ private route / port-forward
cluster:    Postgres + TEI + reranker? + vLLM? + scheduled jobs?
```

This is the recommended cluster boundary. The MCP server is a stdio child of each agent client,
so it stays local and connects to cluster services. Postgres is the only mandatory stateful
workload. TEI, reranker and vLLM are replaceable Deployments with model caches. Ingest is a Job;
DB-only maintenance can be a CronJob.

Transcript extraction is different: it needs the capture queue and transcript files created on
the agent host. Run it locally, or explicitly mount/sync that queue into a narrowly scoped worker.
Do not assume an in-cluster extractor can see a laptop transcript path.

See [the Kubernetes runbook](../deploy/k8s/README.md).

## Topology 5: CPU-only minimal

Run Postgres and TEI on CPU, omit the reranker, and either omit model-backed memory jobs or point
them at an external OpenAI-compatible endpoint. Hybrid search still combines vectors and full
text; it simply returns RRF order without the cross-encoder pass.

CPU vLLM exists, but it is usually not the economical choice for a personal background workload.
Use it when API compatibility or batching is the goal and the latency is acceptable. For a small
Mac deployment, Ollama is simpler. For sustained NVIDIA GPU serving, vLLM is the stronger fit.

## Trust boundaries

Treat the four network surfaces differently:

1. **Postgres** holds the durable corpus and personal memory. Use a scoped application role,
   SCRAM credentials, network isolation and backups.
2. **Embedder** receives document chunks and search queries. TEI and Ollama are normally placed
   on a trusted network rather than exposed directly.
3. **Reranker** receives queries and candidate passages and has no built-in authentication.
4. **LLM endpoint** receives redacted transcript slices or grounded memory context. Redaction is
   best-effort, so a remote provider is still a data-boundary decision.

vLLM's `--api-key` protects the documented API prefixes, not every endpoint on the server. Put a
network policy or authenticated proxy in front of it instead of treating the key as a complete
perimeter.

## Choosing quickly

| Situation | Start with |
|---|---|
| Apple Silicon workstation | local Postgres + Ollama; local lazy reranker; Ollama or CLI LLM |
| One Linux server without a GPU | Compose Postgres + TEI; no reranker; external/optional LLM |
| One NVIDIA GPU server | Postgres + TEI, optional reranker, vLLM for model-backed passes |
| Existing GPU Kubernetes cluster | Postgres PVC + TEI + optional vLLM; local stdio MCP |
| Existing CPU-only Kubernetes cluster | Postgres + TEI; run heavy embedding/LLM work elsewhere |
| Strict local-data requirement | all local; no external LLM endpoint |

Whichever layout you choose, verify the same four boundaries: `hm doctor`, one `search_docs`
call, one scoped `memory_search` call, and `project_status({})` with a physical root matching the
client's project.
