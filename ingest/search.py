#!/usr/bin/env python3
"""Hybrid search (Tier 2) -- Reciprocal Rank Fusion of vector + lexical.

Embeds the query (bge-m3), then fuses:
  * semantic: bge-m3 cosine (HNSW) top-50
  * lexical:  composite fts (<HM_FTS_LANG> || simple), OR-converted query, length-normalized rank
RRF score = 1/(60+rank_sem) + 1/(60+rank_lex). OR-conversion + length-norm are deliberate
(AND is too strict for recall; length-norm stops long docs dominating the lexical leg).

HM_DOC_MAXDIST (optional, default unset = off): a cosine-distance floor for the dense leg. The
dense leg otherwise returns its 50 nearest chunks for ANY query, so a question the corpus does
not cover still gets a full page of results and `query_log` never records a miss. Set it (e.g.
0.5 for bge-m3; calibrate against your own corpus first -- too low and paraphrases go missing)
and the dense leg keeps only chunks at most that far from the query. The lexical leg is
unaffected: an exact token match is evidence on its own.

Chunks with no embedding yet (ingested, embedder not run or failed) are reachable by the lexical
leg only; every search over a scope that has some says how many, in one line.

Usage:  search.py "your query" [k] [repo]      # CLI
        search.py [k] [repo]  <query on stdin>  # MCP mode
        search.py --json <pool> [repo]          # candidate pool for an external reranker
"""
import re
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import connect, embed_query, vec_literal
from _redact import scrub

FTS_LANG = os.environ.get("HM_FTS_LANG", "english")
if not re.fullmatch(r"[a-z_]+", FTS_LANG):   # it is interpolated into SQL literals
    FTS_LANG = "english"

RRF_SQL = f"""
WITH q AS (
  SELECT %s::vector AS emb,
         (replace(websearch_to_tsquery('{FTS_LANG}',%s)::text,'&','|')::tsquery
          || replace(websearch_to_tsquery('simple', %s)::text,'&','|')::tsquery) AS ts,
         %s::text AS repo,
         %s::float8 AS maxdist
),
semantic AS (
  SELECT c.id, RANK() OVER (ORDER BY c.embedding <=> (SELECT emb FROM q)) AS rank
  FROM chunks c JOIN documents d ON d.id = c.document_id
  WHERE c.embedding IS NOT NULL
    AND (SELECT emb FROM q) IS NOT NULL   -- embedder down -> no query vector -> lexical-only
    AND ((SELECT repo FROM q) IS NULL OR d.repo = (SELECT repo FROM q))
    AND ((SELECT maxdist FROM q) IS NULL
         OR c.embedding <=> (SELECT emb FROM q) <= (SELECT maxdist FROM q))
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


def _maxdist():
    """HM_DOC_MAXDIST as a float, or None (off). A value that does not parse is refused
    loudly rather than read as "off": the operator asked for a floor."""
    raw = os.environ.get("HM_DOC_MAXDIST", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        raise SystemExit(f"HM_DOC_MAXDIST={raw!r} is not a number (cosine distance, 0..2)")
    if not 0 < v <= 2:
        raise SystemExit(f"HM_DOC_MAXDIST={raw!r} is outside 0..2 (cosine distance)")
    return v


def _unembedded(cur, repo):
    """How many chunks in scope have no vector -- lexical-only until embed_chunks runs."""
    try:
        cur.execute("SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE c.embedding IS NULL AND (%s::text IS NULL OR d.repo = %s)",
                    (repo, repo))
        return cur.fetchone()[0]
    except Exception:
        cur.connection.rollback()
        return None


def _unembedded_line(n):
    return (f"!! {n} chunk(s) in this scope have no embedding yet -- they are found by exact "
            f"words only (lexical leg). Run ingest/embed_chunks.py (./hm ingest does).")


def _tune(cur):
    # ef_search must exceed the semantic leg's LIMIT 50; iterative scan keeps the leg full
    # when the repo filter discards candidates. No-ops gracefully if no HNSW index yet.
    try:
        cur.execute("SET hnsw.ef_search = 100")
        cur.execute("SET hnsw.iterative_scan = relaxed_order")
    except Exception:
        pass


# -- lexical query hygiene ---------------------------------------------------------------------
# Two defects this fixes, both caused by feeding RAW query text to websearch_to_tsquery and then
# rewriting '&'->'|':
#   1. the `simple` config keeps stopwords, so an OR'd query sharing one stopword ("the", "на")
#      matches essentially every chunk -> 50 arbitrary lexical rows enter the RRF pool as noise;
#   2. a leading '-' becomes a NEGATED lexeme ("!word") which, OR'd with the rest, is true for
#      every document lacking that word -> the lexical leg degenerates to match-everything.
# The stemmed leg drops stopwords itself, so it only needs the negation neutralised.
_STOP = set("""a an and are as at be by for from has he in is it its of on or that the to was were
will with this these those there their they them then than not no but if so do does did can could
would should may might into onto over under out up down about after before between through during
я ты он она оно мы вы они и а но или что это как так же для на в во с со к ко по о об от до из за
над под при про без через между у не ни ли бы же то все всё вот еще ещё уже только очень был была
были быть есть нет да их его её ему ей них нем нём них мой моя мое моё твой наш ваш свой""".split())


# Splitting on every non-word character also splits the tokens Postgres stores WHOLE:
# to_tsvector('simple','example.com') is the single lexeme 'example.com', and a full path is one
# lexeme too. Rewriting the query to `example | com` therefore asked for lexemes the index does
# not contain, and the exact match for every hostname, URL, dotted filename and path silently
# stopped working -- verified against a live store in the production query form. Keep compound
# tokens intact, and exempt them from the length/stopword filter, which exists to stop common
# WORDS from OR-matching every chunk.
_COMPOUND = re.compile(r"[\w\-]+(?:[./][\w\-]+)+", flags=re.U)
_WORD = re.compile(r"[\w\-]+", flags=re.U)


def _tokens(q):
    """[(token, is_compound)] over the query, compound tokens (a.b, a/b/c) kept whole."""
    q = q or ""
    out, pos = [], 0
    for m in _COMPOUND.finditer(q):
        out += [(t, False) for t in _WORD.findall(q[pos:m.start()])]
        out.append((m.group(0), True))
        pos = m.end()
    out += [(t, False) for t in _WORD.findall(q[pos:])]
    return out


def _no_neg(q):
    """Strip leading/trailing dashes from every token so nothing becomes a negated lexeme."""
    toks = [t.strip("-") for t, _ in _tokens(q)]
    toks = [t for t in toks if t]
    return " ".join(toks) if toks else "zzz-no-lexemes-zzz"


def _lex_query(q):
    """Simple-leg query: no negation, no stopwords, no <=2-char tokens. Identifiers survive, and
    so do compound tokens -- `search.py` must not be dropped for being short, and
    `docs/plan/on.md` must not be dropped for containing a stopword."""
    toks = [(t.strip("-"), comp) for t, comp in _tokens(q)]
    toks = [t for t, comp in toks if t and (comp or (len(t) > 2 and t.lower() not in _STOP))]
    return " ".join(toks) if toks else "zzz-no-lexemes-zzz"


DEGRADED = ""   # set when the query could not be embedded: that run was lexical-only


def _query_vec(query):
    # Fail-open: if the embedder is down/slow, return None -> the SQL gets NULL::vector, the
    # semantic leg yields nothing, and search degrades to lexical-only instead of erroring.
    # Failing open is right; failing open SILENTLY is not. Half the retrieval is gone, ranking
    # is worse, and zero hits then look like a gap in the corpus rather than an outage.
    global DEGRADED
    try:
        v = vec_literal(embed_query(query))
        DEGRADED = ""
        return v
    except Exception as exc:
        DEGRADED = f"{type(exc).__name__}: {exc}"
        return None


UNEMBEDDED = None   # set by search()/candidates(): chunks in scope with no vector


def _cap_per_doc(rows, k, doc_of):
    """At most 2 chunks per document, at most k in all -- the cut both entry points apply."""
    per_doc, out = {}, []
    for r in rows:
        doc = doc_of(r)
        if per_doc.get(doc, 0) >= 2:
            continue
        per_doc[doc] = per_doc.get(doc, 0) + 1
        out.append(r)
        if len(out) >= k:
            break
    return out


def search(query, k=8, repo=None):
    global UNEMBEDDED
    emb = _query_vec(query)
    conn = connect(); cur = conn.cursor()
    _tune(cur)
    cur.execute(RRF_SQL, (emb, _no_neg(query), _lex_query(query), repo, _maxdist(),
                          max(k * 8, 40)))
    rows = _cap_per_doc(cur.fetchall(), k, lambda r: r[0])   # one doc can't monopolise top-k
    UNEMBEDDED = _unembedded(cur, repo)
    _log_query(conn, cur, query, len(rows), rows[0][0] if rows else None)
    conn.close()
    return rows


def _log_query(conn, cur, query, n_results, top_doc):
    """Record the search for recall tuning -- n_results=0 is a corpus gap.

    Called from BOTH entry points. It used to be inline in search() only, and the reranked path
    goes through candidates(), so in the configuration the README recommends the table simply
    stayed empty -- which reads as "no searches ran", not as "logging is off on this path", and
    the query it exists to answer ("what did people look for and not find") returned nothing.
    """
    try:
        cur.execute("INSERT INTO query_log (query, n_results, top_doc) VALUES (%s,%s,%s)",
                    (query, n_results, top_doc))
        conn.commit()
    except Exception:
        conn.rollback()


def candidates(query, pool, repo, k=None):
    """Raw RRF pool (no per-doc cap, wider text) for an external reranker.

    `k` is how many results the caller will show. The reranker reorders the pool and then
    applies the same <=2-per-doc, top-k cut, and the size of that cut does not depend on the
    order -- so it is known here, and THAT is what query_log records, not the pool size (which
    made every reranked search look like a 30-result hit). top_doc stays the RRF leader: the
    reranked one is not known yet.
    """
    global UNEMBEDDED
    emb = _query_vec(query)
    conn = connect(); cur = conn.cursor()
    _tune(cur)
    # 3000, not 512. This text IS the passage the cross-encoder scores, and chunks are targeted
    # at 400 tokens -- roughly 1600 characters of English, more of Cyrillic -- so a 512-character
    # cut handed the reranker about a third of each passage, three levels upstream of the model
    # and measured in BYTES. A chunk whose relevant sentence sits after a preamble was scored on
    # the preamble and pushed under the cutoff, and because the reranker fails open there was no
    # way to tell that from a working rerank. Let the reranker's own tokenizer do the truncating
    # at RERANK_MAXLEN, where it is at least token-aware and at the model's configured limit.
    cur.execute(RRF_SQL.replace("left(c.content,110)", "left(c.content,3000)"),
                (emb, _no_neg(query), _lex_query(query), repo, _maxdist(), pool))
    out = [{"doc": doc, "heading": heading,
            "score": float(score) if score is not None else None,
            "srank": srank, "lrank": lrank, "text": snippet}
           for doc, heading, score, srank, lrank, snippet in cur.fetchall()]
    shown = len(_cap_per_doc(out, k, lambda c: c["doc"])) if k else len(out)
    UNEMBEDDED = _unembedded(cur, repo)
    _log_query(conn, cur, query, shown, out[0]["doc"] if out else None)
    conn.close()
    return out


# <private>…</private> spans never become a search: the rule of hooks/_mem_common.py and
# ingest/mem_ops.py strip_private() (nested spans, a stray close dropped, an unclosed tag hiding
# the rest), copied because this script imports neither.
_PRIVATE_TAG = re.compile(r"<(/?)private>", re.I)


def _strip_private(text):
    out, pos, depth = [], 0, 0
    for tag in _PRIVATE_TAG.finditer(text):
        if not tag.group(1):
            if depth == 0:
                out.append(text[pos:tag.start()])
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                pos = tag.end()
        else:
            out.append(text[pos:tag.start()])
            pos = tag.end()
    if depth:
        return "".join(out)
    out.append(text[pos:])
    return "".join(out)


def _read_query():
    """The whole of stdin, as one query.

    `readline()` silently discarded everything after the first newline, and the MCP server sends
    the query verbatim -- so a multi-line question was quietly answered on its first line only,
    with no sign that the rest had been dropped."""
    return " ".join(scrub(_strip_private(sys.stdin.read())).split())


def main():
    args = sys.argv[1:]
    if args and args[0] == "--json":
        pool = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        repo = args[2] if len(args) > 2 else (os.environ.get("HM_REPO") or None)
        if repo and repo.upper() == "ALL":
            repo = None
        k = int(args[3]) if len(args) > 3 and args[3].isdigit() else None
        query = _read_query()
        # candidates() is what SETS DEGRADED, and dict values evaluate left to right -- reading
        # DEGRADED in the same literal captured its pre-call value, so the reranked path always
        # received "" and never printed the LEXICAL-ONLY warning. Call first, then build.
        cands = candidates(query, pool, repo, k) if query else []
        print(json.dumps({"query": query, "repo": repo, "degraded": DEGRADED,
                          "unembedded": UNEMBEDDED or 0, "candidates": cands},
                         ensure_ascii=False))
        return
    if args and not args[0].isdigit():
        query = " ".join(scrub(_strip_private(args[0])).split())
        k = int(args[1]) if len(args) > 1 and args[1].isdigit() else 8
        repo = args[2] if len(args) > 2 else None
    else:
        k = int(args[0]) if args else 8
        repo = args[1] if len(args) > 1 else None
        query = _read_query()
    repo = repo or os.environ.get("HM_REPO") or None
    if repo and repo.upper() == "ALL":
        repo = None
    if not query:
        print("(no query)"); return
    print(f"== query: {query!r}  [scope: {repo or 'ALL repos'}] ==")
    rows = search(query, k, repo)
    if DEGRADED:
        print(f"!! EMBEDDER UNREACHABLE ({DEGRADED}) -- this search was LEXICAL-ONLY: ranking is "
              f"degraded, and few or no results does NOT mean the corpus lacks the topic")
    if UNEMBEDDED:
        print(_unembedded_line(UNEMBEDDED))
    if not rows:
        print("(no results)")
    for doc, heading, score, srank, lrank, snippet in rows:
        legs = f"sem#{srank if srank is not None else '-'} lex#{lrank if lrank is not None else '-'}"
        print(f"[{score}] ({legs}) {doc} # {heading}")
        print(f"    {snippet.strip()[:100]}")


if __name__ == "__main__":
    main()
