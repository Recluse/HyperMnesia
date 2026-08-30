"""Shared plumbing for HyperMnesia's Python tools: a direct Postgres connection from
DATABASE_URL and a pluggable embedder (Ollama or TEI). No cluster/ssh assumptions -- this
runs wherever it can reach the DB and the embedder over the network.

Env:
  DATABASE_URL   postgresql://user:pass@host:5432/db
  EMBED_BACKEND  'ollama' (default) | 'tei'
  OLLAMA_URL     http://localhost:11434     (+ EMBED_MODEL, default 'bge-m3')
  TEI_URL        http://localhost:8080
"""
import json, os, urllib.request

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://hm@localhost:5432/hypermnesia")
BACKEND = os.environ.get("EMBED_BACKEND", "ollama").lower()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
TEI_URL = os.environ.get("TEI_URL", "http://localhost:8080").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")


def connect():
    return psycopg2.connect(DATABASE_URL)


def _post(url, payload, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def embed_texts(texts, timeout=180):
    """Return a list of 1024-dim vectors (bge-m3) for the given texts. Ollama and TEI both
    serve bge-m3 and are cosine-compatible, so you can bulk-embed on one and query on the other."""
    if not texts:
        return []
    if BACKEND == "tei":
        return _post(f"{TEI_URL}/embed", {"inputs": texts, "truncate": True}, timeout=timeout)
    return _post(f"{OLLAMA_URL}/api/embed", {"model": EMBED_MODEL, "input": texts},
                 timeout=timeout)["embeddings"]


def embed_query(text):
    # Short timeout: a query embedding must be fast; the caller (search) falls back to
    # lexical-only if this raises, so a slow/down embedder can't hang an interactive search.
    return embed_texts([text[:6000]], timeout=int(os.environ.get("EMBED_QUERY_TIMEOUT", "20")))[0]


def vec_literal(v):
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"
