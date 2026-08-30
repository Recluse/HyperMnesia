# Kubernetes deployment

There is no one-size-fits-all manifest set here on purpose — clusters differ too much
(storage classes, ingress, node topology). The stack is small, so adapt the `docker-compose.yml`
services into your own manifests. The shape:

- **Postgres + pgvector** — a `Deployment` (or `StatefulSet`) of `pgvector/pgvector:0.8.5-pg16`
  with a `PersistentVolumeClaim`. If the PV is node-local, pin the pod to that node (nodeSelector
  on `kubernetes.io/hostname`) so it doesn't reschedule onto an empty disk. A `ClusterIP` Service
  on 5432. Set `POSTGRES_PASSWORD` from a `Secret`; prefer `scram-sha-256` auth if any pod other
  than your own can reach 5432 (flannel and other CNIs don't enforce NetworkPolicy without a
  policy controller).
- **Embedder (TEI)** — a `Deployment` of `text-embeddings-inference:cpu-1.7` with
  `--model-id BAAI/bge-m3 --port 80 --auto-truncate --max-batch-tokens 4096`, a PVC or hostPath
  for the HF cache, and a Service on 80. One replica is plenty for a personal corpus.
- **Ingest / embed / search** — these are just python scripts against `DATABASE_URL`. Run
  ingest+embed as one-shot `Job`s (or from any pod/host that can reach Postgres and the embedder).
  The **MCP server runs client-side** (on your workstation) and reaches the cluster Postgres via a
  port-forward or a routable Service; point `DATABASE_URL` at it.
- **Reranker (optional)** — a `Deployment` built from `rerank/Dockerfile`, Service on 8091, and
  set `HM_RERANK_URL` accordingly. Skip it for a minimal setup.

Load the schema once Postgres is up:

```bash
kubectl exec -i deploy/postgres -- psql -U hm -d hypermnesia < ../../sql/schema.sql
kubectl exec -i deploy/postgres -- psql -U hm -d hypermnesia < ../../sql/schema_mem.sql
```

If you build a reusable manifest set for your cluster, a PR adding it under `deploy/k8s/<flavor>/`
is welcome.
