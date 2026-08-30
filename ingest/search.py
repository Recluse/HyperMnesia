#!/usr/bin/env python3
"""Hybrid search (Tier 2) -- Reciprocal Rank Fusion of vector + lexical.

Embeds the query (bge-m3), then fuses:
  * semantic: bge-m3 cosine (HNSW) top-50
  * lexical:  composite fts (<HM_FTS_LANG> || simple), OR-converted query, length-normalized rank
RRF score = 1/(60+rank_sem) + 1/(60+rank_lex). OR-conversion + length-norm are deliberate
(AND is too strict for recall; length-norm stops long docs dominating the lexical leg).

Usage:  search.py "your query" [k] [repo]      # CLI
        search.py [k] [repo]  <query on stdin>  # MCP mode
        search.py --json <pool> [repo]          # candidate pool for an external reranker
"""
import re
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import connect, embed_query, vec_literal

FTS_LANG = os.environ.get("HM_FTS_LANG", "english")
if not re.fullmatch(r"[a-z_]+", FTS_LANG):   # it is interpolated into SQL literals
    FTS_LANG = "english"

RRF_SQL = f"""
WITH q AS (
  SELECT %s::vector AS emb,
         (replace(websearch_to_tsquery('{FTS_LANG}',%s)::text,'&','|')::tsquery
          || replace(websearch_to_tsquery('simple', %s)::text,'&','|')::tsquery) AS ts,
         %s::text AS repo
),
semantic AS (
  SELECT c.id, RANK() OVER (ORDER BY c.embedding <=> (SELECT emb FROM q)) AS rank
  FROM chunks c JOIN documents d ON d.id = c.document_id
  WHERE c.embedding IS NOT NULL
    AND (SELECT emb FROM q) IS NOT NULL   -- embedder down -> no query vector -> lexical-only
    AND ((SELECT repo FROM q) IS NULL OR d.repo = (SELECT repo FROM q))
  ORDER BY c.embedding <=> (SELECT emb FROM q) LIMIT 50
),
lexical AS (
  SELECT c.id, RANK() OVER (ORDER BY ts_rank_cd(c.fts,(SELECT ts FROM q),1) DESC) AS rank
  FROM chunks c JOIN documents d ON d.id = c.document_id
  WHERE c.fts @@ (SELECT ts FROM q)
    AND ((SELECT repo FROM q) IS NULL OR d.repo = (SELECT repo FROM q))
  ORDER BY ts_rank_cd(c.fts,(SELECT ts FROM q),1) DESC LIMIT 50
)
SELECT d.repo||':'||d.path AS doc, COALESCE(c.heading_path,'') AS heading,
       round((COALESCE(1.0/(60+s.rank),0)+COALESCE(1.0/(60+l.rank),0))::numeric,4) AS score,
       s.rank AS srank, l.rank AS lrank, left(c.content,110) AS snippet
FROM chunks c
LEFT JOIN semantic s ON s.id=c.id
LEFT JOIN lexical  l ON l.id=c.id
JOIN documents d ON d.id=c.document_id
WHERE s.id IS NOT NULL OR l.id IS NOT NULL
ORDER BY score DESC
LIMIT %s;
"""


def _tune(cur):
    # ef_search must exceed the semantic leg's LIMIT 50; iterative scan keeps the leg full
    # when the repo filter discards candidates. No-ops gracefully if no HNSW index yet.
    try:
        cur.execute("SET hnsw.ef_search = 100")
        cur.execute("SET hnsw.iterative_scan = relaxed_order")
    except Exception:
        pass


def _query_vec(query):
    # Fail-open: if the embedder is down/slow, return None -> the SQL gets NULL::vector, the
    # semantic leg yields nothing, and search degrades to lexical-only instead of erroring.
    try:
        return vec_literal(embed_query(query))
    except Exception:
        return None


def search(query, k=8, repo=None):
    emb = _query_vec(query)
    conn = connect(); cur = conn.cursor()
    _tune(cur)
    cur.execute(RRF_SQL, (emb, query, query, repo, max(k * 8, 40)))
    per_doc, rows = {}, []
    for r in cur.fetchall():                 # cap 2 chunks/doc so one doc can't monopolise top-k
        doc = r[0]
        if per_doc.get(doc, 0) >= 2:
            continue
        per_doc[doc] = per_doc.get(doc, 0) + 1
        rows.append(r)
        if len(rows) >= k:
            break
    try:                                     # log for recall tuning (n_results=0 => a gap)
        cur.execute("INSERT INTO query_log (query, n_results, top_doc) VALUES (%s,%s,%s)",
                    (query, len(rows), rows[0][0] if rows else None))
        conn.commit()
    except Exception:
        conn.rollback()
    conn.close()
    return rows


def candidates(query, pool, repo):
    """Raw RRF pool (no per-doc cap, wider text) for an external reranker."""
    emb = _query_vec(query)
    conn = connect(); cur = conn.cursor()
    _tune(cur)
    cur.execute(RRF_SQL.replace("left(c.content,110)", "left(c.content,512)"),
                (emb, query, query, repo, pool))
    out = [{"doc": doc, "heading": heading,
            "score": float(score) if score is not None else None,
            "srank": srank, "lrank": lrank, "text": snippet}
           for doc, heading, score, srank, lrank, snippet in cur.fetchall()]
    conn.close()
    return out


def main():
    args = sys.argv[1:]
    if args and args[0] == "--json":
        pool = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        repo = args[2] if len(args) > 2 else (os.environ.get("HM_REPO") or None)
        if repo and repo.upper() == "ALL":
            repo = None
        query = sys.stdin.readline().strip()
        print(json.dumps({"query": query, "repo": repo,
                          "candidates": candidates(query, pool, repo) if query else []},
                         ensure_ascii=False))
        return
    if args and not args[0].isdigit():
        query = args[0]
        k = int(args[1]) if len(args) > 1 and args[1].isdigit() else 8
        repo = args[2] if len(args) > 2 else None
    else:
        k = int(args[0]) if args else 8
        repo = args[1] if len(args) > 1 else None
        query = sys.stdin.readline().strip()
    repo = repo or os.environ.get("HM_REPO") or None
    if repo and repo.upper() == "ALL":
        repo = None
    if not query:
        print("(no query)"); return
    print(f"== query: {query!r}  [scope: {repo or 'ALL repos'}] ==")
    for doc, heading, score, srank, lrank, snippet in search(query, k, repo):
        legs = f"sem#{srank if srank is not None else '-'} lex#{lrank if lrank is not None else '-'}"
        print(f"[{score}] ({legs}) {doc} # {heading}")
        print(f"    {snippet.strip()[:100]}")


if __name__ == "__main__":
    main()
