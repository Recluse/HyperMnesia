#!/usr/bin/env python3
"""Health check: is this install actually working, or only appearing to?

Every failure mode this catches is one that leaves a *working-looking* system. Search still
returns rows when the ANN index was never built -- it just sequential-scans forever. Search
still returns rows when half the corpus has no embedding -- from the lexical leg only. The
invariant hook still exits 0 when HM_REPO names a scope nothing was ingested under -- and the
agent reads the silence as "no rules apply". None of that raises an error anywhere, so nothing
tells you until you go looking. This is the going-looking.

    python3 ci/doctor.py            # human-readable, exit 1 if anything FAILED
    python3 ci/doctor.py --json     # same findings as JSON (what the MCP `status` tool serves)

Reads the same env as everything else: DATABASE_URL, EMBED_BACKEND/OLLAMA_URL/TEI_URL,
HM_RERANK_URL, HM_REPO. Checks are read-only and each one is bounded by a short timeout, so a
dead dependency costs seconds, not a hang.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://hm@localhost:5432/hypermnesia")
BACKEND = os.environ.get("EMBED_BACKEND", "ollama").lower()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
TEI_URL = os.environ.get("TEI_URL", "http://localhost:8080").rstrip("/")
RERANK_URL = os.environ.get("HM_RERANK_URL", "").rstrip("/")
REPO = os.environ.get("HM_REPO", "")

OK, WARN, FAIL = "ok", "warn", "fail"


def finding(level, title, detail="", fix=""):
    return {"level": level, "title": title, "detail": detail, "fix": fix}


# -- talking to Postgres ------------------------------------------------------
# Deliberately via the `psql` binary rather than psycopg2: that is the path the HOOKS use, and
# the hooks are what break silently. A doctor that proved the library works while the binary is
# missing would be a doctor for the wrong patient. ON_ERROR_STOP matters just as much here --
# without it psql prints an error to stderr, exits 0, and an SQL failure reads as an empty
# result, which is the exact class of bug this file exists to catch.
def q(sql, timeout=10):
    """Run one query. Returns (rows, error): rows is a list of column-lists."""
    if not shutil.which("psql"):
        return None, "psql is not on PATH"
    try:
        p = subprocess.run(["psql", DATABASE_URL, "-tAX", "-F", "\x1f", "-v", "ON_ERROR_STOP=1"],
                           input=sql.encode(), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as e:
        return None, str(e)
    if p.returncode != 0:
        return None, (p.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
    text = p.stdout.decode("utf-8", "replace").strip()
    return [line.split("\x1f") for line in text.splitlines() if line], None


def http_ok(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 400, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:                      # URLError, timeout, bad host, refused
        return False, str(e)


# -- the checks ---------------------------------------------------------------
def check_database(out):
    rows, err = q("SELECT 1")
    if err:
        out.append(finding(FAIL, "Postgres unreachable", f"{err}",
                           "Check DATABASE_URL and that the server is up. Everything below "
                           "depends on this, so the remaining checks are skipped."))
        return False
    out.append(finding(OK, "Postgres reachable"))
    return True


def check_schema(out):
    want = {"documents", "chunks", "components", "constraints", "relationships"}
    rows, err = q("SELECT table_name FROM information_schema.tables "
                  "WHERE table_schema='public'")
    if err:
        out.append(finding(FAIL, "Cannot read the schema", err))
        return
    have = {r[0] for r in rows}
    missing = sorted(want - have)
    if missing:
        out.append(finding(FAIL, "Schema incomplete", f"missing: {', '.join(missing)}",
                           "Load sql/schema.sql (and sql/schema_mem.sql for personal memory)."))
    else:
        out.append(finding(OK, "Doc schema present"))

    rows, err = q("SELECT 1 FROM information_schema.tables "
                  "WHERE table_schema='mem' AND table_name='memories'")
    if err is None and not rows:
        out.append(finding(WARN, "Personal memory not installed",
                           "schema `mem` has no memories table",
                           "Load sql/schema_mem.sql, or ignore this if you only want doc-RAG."))
    elif err is None:
        out.append(finding(OK, "Personal-memory schema present"))


def check_ann_index(out):
    # The one that costs you nothing visible. schema.sql leaves the HNSW index commented out on
    # purpose (building it before the rows exist is far slower), so an install that follows the
    # guide and skips the step ends up sequential-scanning every vector on every query, forever,
    # with correct results and no complaint.
    rows, err = q("SELECT indexdef FROM pg_indexes "
                  "WHERE tablename='chunks' AND indexdef ILIKE '%hnsw%'")
    if err:
        return
    if rows:
        out.append(finding(OK, "ANN index present", rows[0][0].split(" USING ")[-1]))
    else:
        out.append(finding(WARN, "No ANN index on chunks.embedding",
                           "the dense leg is a sequential scan over every vector",
                           'psql "$DATABASE_URL" -c "CREATE INDEX chunks_embedding_hnsw ON '
                           'chunks USING hnsw (embedding vector_cosine_ops)"'))


def check_embeddings(out):
    rows, err = q("SELECT count(*), count(*) FILTER (WHERE embedding IS NULL) FROM chunks")
    if err:
        return
    total, nulls = int(rows[0][0]), int(rows[0][1])
    if total == 0:
        out.append(finding(WARN, "Corpus is empty", "no chunks stored",
                           "Run ingest/ingest_repo.py, load the SQL, then ingest/embed_chunks.py."))
        return
    if nulls:
        out.append(finding(FAIL if nulls == total else WARN,
                           f"{nulls} of {total} chunks have no embedding",
                           "those chunks can only ever be found by the lexical leg",
                           "python3 ingest/embed_chunks.py"))
    else:
        out.append(finding(OK, f"All {total} chunks embedded"))


def check_embedding_model(out):
    # Vectors from two models do not share a space: a query embedded by one cannot find rows
    # embedded by the other, and the symptom is "the corpus does not cover that" rather than an
    # error. One row in this GROUP BY means one model; two means part of the store is already
    # unreachable and needs re-embedding, not ranking tweaks.
    rows, err = q("SELECT coalesce(embedding_model,'(null)'), count(*) FROM chunks "
                  "WHERE embedding IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")
    if err:
        return
    if not rows:
        return
    if len(rows) == 1:
        out.append(finding(OK, "One embedding model in the store", rows[0][0]))
    else:
        detail = ", ".join(f"{m} ({n})" for m, n in rows)
        out.append(finding(FAIL, "Mixed embedding models in one store", detail,
                           "Vectors from different models are not comparable. Re-embed the "
                           "minority rows with the model you serve queries from."))


def check_map(out):
    rows, err = q("SELECT repo, count(*) FROM components GROUP BY repo ORDER BY repo")
    if err:
        return
    if not rows:
        out.append(finding(WARN, "No component map", "Tier 0/1 is inert without one",
                           "Seed one -- see examples/seed_example.sql."))
        return
    scopes = [r[0] for r in rows]
    out.append(finding(OK, f"Map covers {len(scopes)} scope(s)",
                       ", ".join(f"{r[0]} ({r[1]})" for r in rows)))
    # The scope is matched as an exact string, so a folder named `Infra` whose docs went in as
    # `infra` resolves to nothing on every edit -- silently, which is why it is worth a check.
    if REPO and REPO not in scopes:
        near = [s for s in scopes if s.lower() == REPO.lower()]
        out.append(finding(FAIL, f"HM_REPO={REPO!r} matches no scope",
                           ("did you mean " + repr(near[0]) + "? the match is case-sensitive")
                           if near else "nothing is mapped under that name",
                           "Set HM_REPO to one of the scopes above, or seed a map for this repo."))
    elif REPO:
        lit = "'" + REPO.replace("'", "''") + "'"
        rows, err = q(f"SELECT count(*) FROM constraints WHERE repo={lit} "
                      f"AND status='active' AND severity='must'")
        if err is None and rows and int(rows[0][0]) == 0:
            out.append(finding(WARN, f"Scope {REPO!r} has no active `must` constraints",
                               "the pre-edit hook will never inject anything here",
                               "`should`/`info` are not injected by design -- if you meant them "
                               "to be enforced, raise the severity."))


def check_embedder(out):
    if BACKEND == "tei":
        ok, how = http_ok(f"{TEI_URL}/health")
        name, url = "TEI", TEI_URL
    else:
        ok, how = http_ok(f"{OLLAMA_URL}/api/tags")
        name, url = "Ollama", OLLAMA_URL
    if ok:
        out.append(finding(OK, f"Embedder reachable ({name})", url))
    else:
        out.append(finding(FAIL, f"Embedder unreachable ({name})", f"{url}: {how}",
                           "Search still answers without it -- from the lexical leg only, "
                           "which is a quiet halving of recall, not an error."))


def check_reranker(out):
    if not RERANK_URL:
        return                                  # optional by design; not configured is not a fault
    ok, how = http_ok(f"{RERANK_URL}/health")
    if ok:
        out.append(finding(OK, "Reranker reachable", RERANK_URL))
    else:
        out.append(finding(WARN, "Reranker unreachable", f"{RERANK_URL}: {how}",
                           "Search fails OPEN to plain RRF order, so this looks like working "
                           "search with quietly worse ranking rather than an error."))


CHECKS = [check_schema, check_ann_index, check_embeddings, check_embedding_model, check_map]


def run():
    out = []
    if check_database(out):
        for c in CHECKS:
            c(out)
    check_embedder(out)
    check_reranker(out)
    return out


def main(argv):
    out = run()
    if "--json" in argv:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        mark = {OK: "ok  ", WARN: "WARN", FAIL: "FAIL"}
        for f in out:
            print(f"{mark[f['level']]}  {f['title']}" + (f"  -- {f['detail']}" if f["detail"] else ""))
            if f["fix"] and f["level"] != OK:
                print(f"        {f['fix']}")
        bad = sum(1 for f in out if f["level"] == FAIL)
        warn = sum(1 for f in out if f["level"] == WARN)
        print(f"\n{len(out)} checks: {len(out) - bad - warn} ok, {warn} warning(s), {bad} failure(s)")
    return 1 if any(f["level"] == FAIL for f in out) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
