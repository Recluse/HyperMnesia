#!/usr/bin/env python3
"""Where the time goes: the per-edit hook, the embedder, the database, the reranker.

Measures only what is reproducible on someone else's machine. There is deliberately no claim
here about answer quality or about whether injected context makes an agent do better work --
that needs a benchmark set and a controlled comparison, and a number published without one is
invented.

Every stage is reported with the corpus it ran against, because a latency figure without a
corpus size is unreadable. Median and worst of N runs, first run discarded: the first touches
cold caches and would make every number a story about page cache instead of about this system.

    python3 ci/latency.py                    # 5 runs per stage against DATABASE_URL
    python3 ci/latency.py -n 20 -q "how does ingest handle renames"
    python3 ci/latency.py --selfcheck        # statistics only, no store needed

Env: DATABASE_URL, EMBED_BACKEND/OLLAMA_URL/TEI_URL, HM_RERANK_URL, HM_REPO.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ingest/search.py imports its siblings by bare name (`from _common import ...`), so the ingest
# directory itself has to be importable -- not just the repo root.
sys.path.insert(0, os.path.join(ROOT, "ingest"))

DEFAULT_QUERY = "how are documents chunked before embedding"
QUERY = DEFAULT_QUERY


def stats(samples):
    """Median, p90 and max in milliseconds. p90 by nearest-rank -- with 5 samples that IS the
    worst one, which is honest; a smoothed percentile over five points invents precision."""
    xs = sorted(samples)
    n = len(xs)
    mid = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    rank = max(1, -(-n * 90 // 100))           # ceil(n*0.9), 1-based
    return {"median_ms": round(mid, 1), "p90_ms": round(xs[rank - 1], 1),
            "max_ms": round(xs[-1], 1), "n": n}


def timed(fn, runs):
    """Run fn runs+1 times, discard the first, return per-run milliseconds."""
    out = []
    for i in range(runs + 1):
        t0 = time.perf_counter()
        fn()
        dt = (time.perf_counter() - t0) * 1000
        if i:
            out.append(dt)
    return out


def corpus_size():
    from _common import connect
    conn = connect(); cur = conn.cursor()
    cur.execute("SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks), "
                "(SELECT count(*) FROM chunks WHERE embedding IS NOT NULL), "
                "(SELECT count(*) FROM components)")
    row = cur.fetchone()
    conn.close()
    return {"documents": row[0], "chunks": row[1], "embedded": row[2], "components": row[3]}


def stage_hook(runs):
    """The whole PreToolUse hook, as a process. This is the one that runs on EVERY edit, and it
    pays for a python start and a psql start each time -- so measuring the function alone would
    flatter it by exactly the part the user actually waits for."""
    repo = os.environ.get("HM_REPO", "")
    event = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                        "cwd": "/tmp/" + (repo or "repo"),
                        "tool_input": {"file_path": f"/tmp/{repo or 'repo'}/src/api/x.py"}})
    hook = os.path.join(ROOT, "hooks", "arch_invariants.py")

    def once():
        subprocess.run([sys.executable, hook], input=event.encode(),
                       capture_output=True, timeout=60)
    return timed(once, runs)


def stage_embed(runs):
    from _common import embed_query
    q = QUERY
    return timed(lambda: embed_query(q), runs)


def stage_rrf(runs):
    """The fused retrieval query alone, with the embedding computed once up front.

    Both legs live in one SQL statement, so this is one number, not two -- splitting the
    production query to measure it would mean measuring something other than production. If this
    is the slow stage, EXPLAIN ANALYZE the same statement for the per-leg breakdown.
    """
    from _common import connect, vec_literal, embed_query
    import search as S
    emb = vec_literal(embed_query(QUERY))
    conn = connect(); cur = conn.cursor()
    S._tune(cur)
    repo = os.environ.get("HM_REPO") or None
    args = (emb, S._no_neg(QUERY), S._lex_query(QUERY), repo, 40)

    def once():
        cur.execute(S.RRF_SQL, args)
        cur.fetchall()
    out = timed(once, runs)
    conn.close()
    return out


def stage_search(runs):
    """search.py end to end, as the MCP server invokes it: process start, embed, query, format."""
    script = os.path.join(ROOT, "ingest", "search.py")
    repo = os.environ.get("HM_REPO", "")

    def once():
        subprocess.run([sys.executable, script, "8"] + ([repo] if repo else []),
                       input=QUERY.encode(), capture_output=True, timeout=120)
    return timed(once, runs)


def stage_rerank(runs):
    url = os.environ.get("HM_RERANK_URL", "").rstrip("/")
    if not url:
        return None
    payload = json.dumps({"query": QUERY,
                          "docs": ["a passage about chunking" for _ in range(30)]}).encode()

    def once():
        req = urllib.request.Request(url + "/rerank", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
    return timed(once, runs)


STAGES = [
    ("hook (PreToolUse, per edit)", stage_hook),
    ("embed one query", stage_embed),
    ("fused RRF query", stage_rrf),
    ("search.py end to end", stage_search),
    ("rerank 30 passages", stage_rerank),
]


def selfcheck():
    # Odd and even counts, and the nearest-rank rule at small n.
    assert stats([10, 20, 30])["median_ms"] == 20
    assert stats([10, 20, 30, 40])["median_ms"] == 25
    assert stats([1, 2, 3, 4, 5])["p90_ms"] == 5, "with 5 samples p90 is the worst one"
    assert stats([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])["p90_ms"] == 9
    assert stats([5])["p90_ms"] == 5 and stats([5])["median_ms"] == 5
    assert stats([3, 1, 2])["max_ms"] == 3, "unsorted input must still work"
    # timed() must discard the warm-up: 3 runs asked for -> 3 samples, not 4.
    assert len(timed(lambda: None, 3)) == 3
    print("ok")
    return 0


def main(argv):
    global QUERY
    if "--selfcheck" in argv:
        return selfcheck()
    runs = int(argv[argv.index("-n") + 1]) if "-n" in argv else 5
    QUERY = argv[argv.index("-q") + 1] if "-q" in argv else DEFAULT_QUERY

    try:
        size = corpus_size()
    except Exception as e:
        print(f"cannot reach the store ({e}) -- latency without a corpus means nothing", file=sys.stderr)
        return 1
    print(f"corpus: {size['documents']} documents, {size['chunks']} chunks "
          f"({size['embedded']} embedded), {size['components']} components")
    print(f"query:  {QUERY!r}")
    print(f"runs:   {runs} per stage, first discarded\n")

    print(f"{'stage':<30} {'median':>9} {'p90':>9} {'max':>9}")
    for name, fn in STAGES:
        try:
            samples = fn(runs)
        except Exception as e:
            print(f"{name:<30}  skipped: {type(e).__name__}: {e}")
            continue
        if samples is None:
            print(f"{name:<30}  not configured")
            continue
        s = stats(samples)
        print(f"{name:<30} {s['median_ms']:>8.1f}ms {s['p90_ms']:>8.1f}ms {s['max_ms']:>8.1f}ms")

    print("\nWhat this is not: a measure of whether any of it helps an agent write better code.\n"
          "That needs a task set and a controlled comparison, and is not measured here.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
