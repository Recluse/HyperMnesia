#!/usr/bin/env python3
"""Search + rerank orchestrator. Drop-in for search.py's human output, with a cross-encoder
rerank stage in the middle:

  1. pull a candidate POOL from the hybrid RRF search (ingest/search.py --json)
  2. rerank the pool via the local reranker service (fail-open to RRF order if it's down)
  3. apply <=2-chunks/doc dedup + top-k, print in search.py's format

The MCP server points `search_docs` at this when HM_RERANK is set. Everything runs on one
machine (search connects to the DB directly), so there is no network dependency beyond the
DB, the embedder, and the local reranker.

Interface (same as search.py MCP mode):  search_reranked.py <k> [repo]  with query on stdin.
Env: HM_RERANK_URL (http://127.0.0.1:8091), RERANK_POOL (30); + search.py's env (DATABASE_URL, ...).
"""
import json, os, subprocess, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SEARCH = os.environ.get("HM_SEARCH", os.path.join(HERE, "..", "ingest", "search.py"))
URL = os.environ.get("HM_RERANK_URL", "http://127.0.0.1:8091").rstrip("/")
POOL = int(os.environ.get("RERANK_POOL", "30"))


def get_candidates(query, k, repo, timeout=60):
    p = subprocess.run([sys.executable, SEARCH, "--json", str(POOL), repo or "ALL"],
                       input=(query + "\n").encode(), capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[-400:])
    return json.loads(p.stdout.decode() or "{}").get("candidates", [])


def rerank_scores(query, docs, timeout=20):
    body = json.dumps({"query": query, "docs": docs}).encode()
    req = urllib.request.Request(URL + "/rerank", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["scores"]


def fmt(c):
    legs = (f"sem#{c['srank'] if c['srank'] is not None else '-'} "
            f"lex#{c['lrank'] if c['lrank'] is not None else '-'}")
    return f"[{c['score']}] ({legs}) {c['doc']} # {c['heading']}\n    {(c['text'] or '').strip()[:100]}"


def main():
    args = sys.argv[1:]
    k = int(args[0]) if args and args[0].isdigit() else 8
    repo = args[1] if len(args) > 1 else (os.environ.get("HM_REPO") or None)
    query = sys.stdin.readline().strip()
    if not query:
        print("(no query)")
        return
    cands = get_candidates(query, k, repo)
    order = cands
    if cands:
        try:  # fail open to RRF order on any reranker problem
            scores = rerank_scores(query, [c["text"] for c in cands])
            if len(scores) == len(cands):
                order = [c for _, c in sorted(zip(scores, cands),
                                              key=lambda x: x[0], reverse=True)]
        except Exception:
            order = cands
    per_doc, picked = {}, []
    for c in order:
        d = c["doc"]
        if per_doc.get(d, 0) >= 2:
            continue
        per_doc[d] = per_doc.get(d, 0) + 1
        picked.append(c)
        if len(picked) >= k:
            break
    print(f"== query: {query!r}  [scope: {repo or 'ALL repos'}] ==")
    if not picked:
        print("(no results -- embeddings may still be indexing)")
    for c in picked:
        print(fmt(c))


if __name__ == "__main__":
    main()
