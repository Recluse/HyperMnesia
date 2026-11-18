#!/usr/bin/env python3
"""Render Kubernetes templates in minimal and full modes, then parse every document."""
import json
import pathlib
import os
import subprocess
import tempfile

import jinja2
import yaml

ROOT = pathlib.Path(__file__).parents[1]
DEFAULTS = yaml.safe_load(
    (ROOT / "roles/hypermnesia_kubernetes/defaults/main.yml").read_text()
)
DEFAULTS.update({
    "hypermnesia_kubeconfig": "/tmp/kubeconfig",
    "hypermnesia_storage_class": "fast-rwo",
    "hypermnesia_postgres_image": "pgvector@sha256:" + "a" * 64,
    "hypermnesia_tei_image": "tei@sha256:" + "b" * 64,
    "hypermnesia_reranker_image": "reranker@sha256:" + "c" * 64,
    "hypermnesia_vllm_image": "vllm@sha256:" + "d" * 64,
    "hypermnesia_worker_image": "worker@sha256:" + "e" * 64,
    "hypermnesia_migration_id": "release-012345",
})
env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(ROOT / "roles/hypermnesia_kubernetes/templates"),
    undefined=jinja2.StrictUndefined,
    autoescape=False,
)
env.filters["to_json"] = json.dumps


def render(name, values):
    text = env.get_template(name).render(**values)
    docs = [doc for doc in yaml.safe_load_all(text) if doc]
    assert docs and all("apiVersion" in doc and "kind" in doc for doc in docs)
    return docs


minimal = render("data-plane.yml.j2", DEFAULTS)
assert {(x["kind"], x["metadata"]["name"]) for x in minimal} >= {
    ("Deployment", "postgres"), ("Deployment", "tei"), ("Service", "postgres")
}

full = dict(DEFAULTS)
full.update({
    "hypermnesia_reranker_enabled": True,
    "hypermnesia_vllm_enabled": True,
    "hypermnesia_vllm_model": "org/model",
    "hypermnesia_memory_cronjobs": [{
        "name": "refresh-memory",
        "schedule": "17 * * * *",
        "args": ["hm", "refresh"],
    }],
})
full_docs = render("data-plane.yml.j2", full)
names = {x["metadata"]["name"] for x in full_docs}
assert {"reranker", "vllm", "refresh-memory", "postgres-clients", "model-clients"} <= names

# Probe/Service traffic arrives through the Pod IP, not its loopback interface.
reranker = next(d for d in full_docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "reranker")
assert {e["name"]: e["value"] for e in reranker["spec"]["template"]["spec"]["containers"][0]["env"]}["HM_RERANK_BIND"] == "0.0.0.0"
pg = next(d for d in minimal if d["kind"] == "Deployment" and d["metadata"]["name"] == "postgres")
pod = pg["spec"]["template"]["spec"]
assert pod["securityContext"]["runAsUser"] == 999
assert pod["securityContext"]["runAsGroup"] == pod["securityContext"]["fsGroup"] == 999
assert pod["securityContext"]["runAsNonRoot"] is True
container = pod["containers"][0]
with tempfile.TemporaryDirectory() as raw:
    tmp = pathlib.Path(raw)
    volume = tmp / "volume"
    volume.mkdir()
    entry = tmp / "docker-entrypoint.sh"
    entry.write_text('#!/bin/sh\nprintf "%s\\n" "$PGDATA"\n')
    entry.chmod(0o755)
    script = container["args"][0].replace("/var/lib/postgresql/data", str(volume))
    child_env = dict(os.environ, PATH=str(tmp) + os.pathsep + os.environ["PATH"])
    def start():
        return subprocess.run([*container["command"], script], env=child_env,
                              capture_output=True, text=True, timeout=5)
    # Fresh volume: the unprivileged process creates its own data directory.
    assert start().stdout.strip() == str(volume / "pgdata")
    # Existing volume: its database is reused; never initialize next to it.
    (volume / "PG_VERSION").write_text("16\n")
    assert start().stdout.strip() == str(volume)
    (volume / "pgdata").mkdir()
    (volume / "pgdata/PG_VERSION").write_text("16\n")
    ambiguous = start()
    assert ambiguous.returncode != 0 and "Two PostgreSQL" in ambiguous.stderr


job = render("migration-job.yml.j2", full)
assert job[0]["kind"] == "Job"
assert job[0]["metadata"]["name"] == "hypermnesia-migrate-release-012345"
print("Kubernetes template contract: PASS")
