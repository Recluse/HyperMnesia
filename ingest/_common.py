"""Shared plumbing for HyperMnesia's Python tools: a direct Postgres connection from
DATABASE_URL and a pluggable embedder (Ollama or TEI). No cluster/ssh assumptions -- this
runs wherever it can reach the DB and the embedder over the network.

Env:
  DATABASE_URL   postgresql://user:pass@host:5432/db
  EMBED_BACKEND  'ollama' (default) | 'tei'
  OLLAMA_URL     http://localhost:11434     (+ EMBED_MODEL, default 'bge-m3')
  TEI_URL        http://localhost:8080
"""
import json, os, sys, urllib.request

# The shared settings file is applied HERE as well, not only in the hooks.
#
# It used to be applied in exactly one place -- hooks/_mem_common, on import -- and neither this
# module nor embed_chunks.py nor mem_ops.py goes through it when it is started by the MCP server
# or by hand. So EMBED_BACKEND, EMBED_MODEL, EMBED_BATCH, MEM_SEM_MAXDIST and MEM_LEX_MAXDIST were
# offered by the console, displayed as "in effect", and read by processes the file never reached.
# This is the module all three import, which makes it the one place that fixes all five.
try:
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))
    from _mem_common import load_env_file   # noqa: E402  (applies the file on import)
    load_env_file()
except Exception:                            # a deployment that ships ingest/ without hooks/
    pass                                     # keeps working on its own environment

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


EMBED_QUERY_MAX_CHARS = int(os.environ.get("EMBED_QUERY_MAX_CHARS", "12000"))


def embed_query(text):
    # Short timeout: a query embedding must be fast; the caller (search) falls back to
    # lexical-only if this raises, so a slow/down embedder can't hang an interactive search.
    #
    # The character cut is for a QUERY, which is short. Callers that embed something longer
    # (mem_ops embeds whole memories through here) must cap it themselves and say when they do:
    # a silent cut leaves content stored and lexically indexed in full but semantically
    # reachable only by its opening.
    return embed_texts([text[:EMBED_QUERY_MAX_CHARS]],
                       timeout=int(os.environ.get("EMBED_QUERY_TIMEOUT", "20")))[0]


def vec_literal(v):
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"
