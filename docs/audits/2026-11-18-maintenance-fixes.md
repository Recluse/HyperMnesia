# Maintenance and installation fixes — 18 November 2026

This change addresses ten source-level findings in HyperMnesia. It covers corpus exclusions,
current-memory grounding, job outcomes, incremental ingest, and the Ansible installation path.
It does not assert that a running installation has been upgraded.

## Corrections

| Boundary | Result |
| --- | --- |
| Corpus exclusions | Only a missing optional `.hmignore` is accepted. An unreadable policy, directory or dangling symlink stops generation before replacing the SQL output. |
| Current evidence | Context selects evidence from `active_memories`, including its validity-window rules. Historical provenance remains available through `memory_evidence`. |
| Scheduled jobs | Dreamer and Reflect return a failure exit status for store, model or required-write failures. A failed group cannot be hidden by another group's success. |
| Extractor preview | Dry-run does not mark short or missing transcripts complete. A subsequent real run can still process the queue. |
| Claude registration | The default, hooks-disabled installation still receives explicit write denies. |
| Managed-host smoke | The smoke helper is copied to the managed host and executed there, without referring to a controller-only path. |
| Ansible idempotence | `changed_when` distinguishes `changed` from `unchanged`; a repeated registration reports zero changes. |
| PostgreSQL Pod | Numeric non-root UID/GID and fsGroup are explicit. New volumes use a child data directory; existing root-layout databases keep their path. Ambiguous layouts stop startup. |
| Reranker Pod | The listener accepts Pod-interface traffic, matching the Service and readiness probe. Network exposure remains ClusterIP with the existing NetworkPolicy. |
| Incremental apply | A path/hash snapshot is compared exactly under a write lock, rather than by row count. Concurrent replacements invalidate the plan; unchanged chunks and embeddings survive. |

## Verification

The new `tests/test_audit_regressions.py` runs real ingestion and job entrypoints with isolated
state and controlled transport failures. The live lifecycle test adds current, expired and
future explicit ancestors behind a derived conclusion. It checks both context inclusion and
Dialectic citation eligibility.

The lifecycle suite passed **128/128 checks** on a temporary PostgreSQL 18.6 / pgvector 0.8.6
instance. The same suite against the original implementation failed four expired/future checks.
`test_incremental_ingest_live.py` also passed: stale hashes and renamed paths with unchanged row
counts are rejected, a concurrent writer is blocked during apply, and unchanged embeddings are
preserved. The original ingester accepted the stale-hash case. No live store or model was used.
The existing CI job continues to exercise PostgreSQL 16 / pgvector 0.8.5.

All **32 selected offline Python test scripts** passed. Ansible merger and template tests passed.
The real client role ran twice in a disposable HOME under both Ansible Core 2.19.4 and 2.21.4;
it preserved unrelated configuration, installed denies with hooks disabled, called the copied
MCP smoke, and reported `changed=0` on the second run. Ansible-lint 25.11.0 completed with zero
lint findings. Core 2.19.4 emitted its Python 3.14 support warning; this successful local run
does not establish support beyond the tool's documented versions. Syntax and focused Ruff
checks passed. The new regression tests are included in CI.

## Operational limits

The ingest lock covers `documents` across scopes until commit. Readers remain available, and
embedding computation happens outside that transaction. Regenerate a rejected plan from a
fresh database snapshot.

Kubernetes templates and the data-directory selection script were tested locally; a new Pod on
a real storage driver was not started. Custom images must provide `docker-entrypoint.sh`, and
UID/GID must match the image and existing data. The role does not move a database or repair
volume ownership. See [the Ansible guide](../../deploy/ansible/README.md) before rollout.
