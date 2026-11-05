#!/usr/bin/env python3
"""Render Kubernetes templates in minimal and full modes, then parse every document."""
import json
import pathlib

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

job = render("migration-job.yml.j2", full)
assert job[0]["kind"] == "Job"
assert job[0]["metadata"]["name"] == "hypermnesia-migrate-release-012345"
print("Kubernetes template contract: PASS")
