# Kubernetes deployment

This runbook puts HyperMnesia's data plane in Kubernetes while keeping each agent's stdio MCP
process on the client machine:

```text
agent client -> local hypermnesia-mcp -> private Postgres Service
                         │
                         ├-> TEI Service (`bge-m3`)
                         ├-> optional reranker Service
                         └-> optional vLLM Service (model-backed memory passes)
```

Postgres is the only durable component. Model caches are replaceable. Ingest is a one-shot Job or
a client-side command; consolidation, reflection and Dreamer can be CronJobs. Session extraction
normally remains on the agent host because its queue refers to local transcript files.

For topology choices, see [docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md). For model serving, see
[docs/LLM_BACKENDS.md](../../docs/LLM_BACKENDS.md).

## Prerequisites

- Kubernetes 1.27 or newer and a StorageClass;
- private client access through a routed network or `kubectl port-forward`;
- an image containing this checkout, Python 3 and `psycopg2` for in-cluster jobs;
- a GPU node and device plugin only when a model service will use a GPU.

Examples use namespace `hypermnesia`. Pin images by digest after validating them.

## 1. Namespace and credentials

```bash
kubectl create namespace hypermnesia
POSTGRES_PASSWORD=$(openssl rand -hex 24)
kubectl -n hypermnesia create secret generic postgres-auth \
  --from-literal=POSTGRES_USER=hm \
  --from-literal=POSTGRES_DB=hypermnesia \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD"
```

Store the generated value in a secret manager. Do not recover client access by publishing 5432.

## 2. Postgres + pgvector

Apply a PVC, Deployment and ClusterIP service. Set `storageClassName` for your cluster. A
node-local PV also needs required node affinity, or a rescheduled pod can land beside an empty
disk.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: postgres-data, namespace: hypermnesia}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 20Gi}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: postgres, namespace: hypermnesia}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: hypermnesia-postgres}}
  template:
    metadata: {labels: {app: hypermnesia-postgres}}
    spec:
      containers:
        - name: postgres
          image: pgvector/pgvector:0.8.5-pg16
          envFrom: [{secretRef: {name: postgres-auth}}]
          ports: [{name: postgres, containerPort: 5432}]
          volumeMounts: [{name: data, mountPath: /var/lib/postgresql/data}]
          readinessProbe:
            exec: {command: [sh, -c, 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"']}
            periodSeconds: 10
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {memory: 1Gi}
      volumes: [{name: data, persistentVolumeClaim: {claimName: postgres-data}}]
---
apiVersion: v1
kind: Service
metadata: {name: postgres, namespace: hypermnesia}
spec:
  selector: {app: hypermnesia-postgres}
  ports: [{name: postgres, port: 5432, targetPort: postgres}]
```

Wait for `kubectl -n hypermnesia rollout status deploy/postgres --timeout=180s`.

## 3. Load or upgrade the schema

From the repository root, stream every file in order. `ON_ERROR_STOP` prevents a partial load from
looking successful.

```bash
for file in \
  sql/schema.sql sql/schema_mem.sql sql/mem_multiuser.sql \
  sql/migrate_entity_ownership.sql sql/migrate_memory_perspectives.sql \
  sql/migrate_memory_evidence.sql sql/migrate_peer_cards.sql \
  sql/migrate_dreamer.sql sql/mem_app_role.sql
do
  kubectl -n hypermnesia exec -i deploy/postgres -- \
    psql -U hm -d hypermnesia -v ON_ERROR_STOP=1 -v owner="$USER" < "$file"
done
```

The migrations are idempotent, but their order is part of the contract. Back up Postgres before
an upgrade and run `hm doctor` afterward.

## 4. TEI embedder

`bge-m3` uses several GiB on CPU. Give it enough memory without starving Postgres.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: tei-cache, namespace: hypermnesia}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 10Gi}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: tei, namespace: hypermnesia}
spec:
  replicas: 1
  selector: {matchLabels: {app: hypermnesia-tei}}
  template:
    metadata: {labels: {app: hypermnesia-tei}}
    spec:
      containers:
        - name: tei
          image: ghcr.io/huggingface/text-embeddings-inference:cpu-1.7
          args: [--model-id, BAAI/bge-m3, --port, '80', --auto-truncate, --max-batch-tokens, '4096']
          ports: [{name: http, containerPort: 80}]
          volumeMounts: [{name: cache, mountPath: /data}]
          readinessProbe: {httpGet: {path: /health, port: http}, initialDelaySeconds: 20}
          resources:
            requests: {cpu: '2', memory: 7Gi}
            limits: {memory: 10Gi}
      volumes: [{name: cache, persistentVolumeClaim: {claimName: tei-cache}}]
---
apiVersion: v1
kind: Service
metadata: {name: tei, namespace: hypermnesia}
spec:
  selector: {app: hypermnesia-tei}
  ports: [{name: http, port: 80, targetPort: http}]
```

Use a supported accelerator image and device resource for GPU serving; validate the exact TEI
release and runtime instead of changing only the image suffix.

## 5. Optional reranker

Build `rerank/Dockerfile`, publish it to your registry, and deploy it on port 8091. Set
`HM_RERANK_BIND=0.0.0.0`: the host-safe loopback default cannot receive Service traffic inside a
pod. The endpoint has no authentication, so restrict it with NetworkPolicy.

Give a CPU instance up to 5 GiB for loaded weights and set `RERANK_IDLE_SEC=600` to release most
of it when idle. The `/health` endpoint does not load the model. Search fails open to plain RRF and
announces the fallback if reranking is unavailable.

Workers and clients use `HM_RERANK_URL=http://reranker:8091` inside the namespace.

## 6. Optional vLLM service

HyperMnesia can use vLLM for extraction, consolidation, reflection, Dreamer and Dialectic. It
still uses TEI or Ollama for `bge-m3` embeddings.

Create its API key as a Secret before applying the Deployment:

```bash
VLLM_API_KEY=$(openssl rand -hex 24)
kubectl -n hypermnesia create secret generic vllm-auth \
  --from-literal=api-key="$VLLM_API_KEY"
```

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: vllm, namespace: hypermnesia}
spec:
  replicas: 1
  selector: {matchLabels: {app: hypermnesia-vllm}}
  template:
    metadata: {labels: {app: hypermnesia-vllm}}
    spec:
      containers:
        - name: vllm
          image: vllm/vllm-openai:<pinned-version>
          args:
            - Qwen/Qwen2.5-7B-Instruct
            - --host=0.0.0.0
            - --port=8000
            - --generation-config=vllm
            - --api-key=$(VLLM_API_KEY)
          env:
            - name: VLLM_API_KEY
              valueFrom: {secretKeyRef: {name: vllm-auth, key: api-key}}
          ports: [{name: http, containerPort: 8000}]
          startupProbe:
            httpGet: {path: /health, port: http}
            failureThreshold: 60
            periodSeconds: 10
          readinessProbe: {httpGet: {path: /health, port: http}, periodSeconds: 10}
          resources:
            requests: {cpu: '2', memory: 8Gi, nvidia.com/gpu: '1'}
            limits: {memory: 24Gi, nvidia.com/gpu: '1'}
---
apiVersion: v1
kind: Service
metadata: {name: vllm, namespace: hypermnesia}
spec:
  selector: {app: hypermnesia-vllm}
  ports: [{name: http, port: 8000, targetPort: http}]
```

Kubernetes expands `$(VLLM_API_KEY)` in `args`. Model workers use:

```text
HM_LLM_BACKEND=openai
HM_LLM_URL=http://vllm:8000/v1
HM_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
HM_LLM_KEY=<the same secret>
```

Tune model, context and GPU memory flags for actual hardware. For multiple replicas or models,
consider the upstream
[vLLM production stack](https://docs.vllm.ai/projects/production-stack/en/latest/deployment/).
The vLLM API key does not protect every server endpoint, so keep the Service private.

## 7. Ingest and embedding

The simplest path is from the workstation that already has the source:

```bash
kubectl -n hypermnesia port-forward svc/postgres 15432:5432
kubectl -n hypermnesia port-forward svc/tei 18080:80

export DATABASE_URL=postgresql://hm:<password>@127.0.0.1:15432/hypermnesia
export EMBED_BACKEND=tei
export TEI_URL=http://127.0.0.1:18080
./hm ingest /path/to/repository repository-scope
```

Alternatively, run a pinned worker image as a Job. Supply the source through a read-only volume,
an init container that checks out an exact revision, or a verified snapshot upload. Set
`DATABASE_URL`, `EMBED_BACKEND=tei`, `TEI_URL=http://tei`, and
`EMBED_REPO=repository-scope`; then run `./hm ingest /workspaces/repository repository-scope`.

For parallel bulk embedding, start N Jobs with `SHARD_N=N` and distinct `SHARD_I` values. Run one
final `hm doctor` after all shards finish.

## 8. Scheduled memory jobs

Consolidation, reflection and Dreamer can be separate CronJobs. Use
`concurrencyPolicy: Forbid`, a bounded deadline and the scoped application role. A worker needs:

```text
DATABASE_URL=postgresql://hm_app:...@postgres:5432/hypermnesia
EMBED_BACKEND=tei
TEI_URL=http://tei
HM_LLM_BACKEND=openai
HM_LLM_URL=http://vllm:8000/v1
HM_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct
HM_LLM_KEY=<secret>
```

Run one of `python3 hooks/mem_consolidate.py`, `mem_reflect.py`, or `mem_dream.py`. Do not add
automatic review approval: Dreamer proposals must remain human-gated.

`mem_extract.py` is different. Its queue contains local transcript paths, so keep extraction on
the client unless the queue and transcripts are deliberately copied into a private volume.

## 9. MCP clients

`hypermnesia-mcp` is a stdio server launched by the agent client. Do not create a Kubernetes
Service for it unless you also implement a tested stdio-to-network transport.

For a client outside the cluster, forward Postgres and TEI (and the optional reranker), then set
their loopback URLs in the project-scoped MCP descriptor. Set exact `HM_REPO` and physical
`HM_ROOT` there. Never pin one project's repo/root in a global descriptor shared by other repos.

## 10. Network policy

At minimum, permit only these flows:

- MCP and workers to Postgres;
- ingest/search workers to TEI;
- search workers to the reranker, if enabled;
- model-backed memory workers to vLLM, if enabled.

Use default-deny ingress when the CNI enforces NetworkPolicy. A ClusterIP is routing, not
authorization. None of Postgres, TEI, reranker or vLLM needs a public Ingress.

## 11. Verify and operate

```bash
kubectl -n hypermnesia rollout status deploy/postgres
kubectl -n hypermnesia rollout status deploy/tei
kubectl -n hypermnesia get pods,svc,pvc
./hm doctor
python3 ci/latency.py
python3 ci/freshness.py /path/to/repository repository-scope
```

Finally call MCP `status`, `project_status`, `search_docs` and `memory_search`. Confirm that
`project_status.root` is the physical project path and its counts belong to the intended scope.
An initialized MCP process does not prove scope or freshness.

Back up Postgres with `pg_dump` or storage snapshots and test restore. Preserve Secrets separately
from PVCs. Treat model caches as disposable. Apply SQL migrations before clients that need new
tools, then rerun diagnostics and the relevant evals.
