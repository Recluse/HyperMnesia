#!/usr/bin/env python3
"""Personal-memory operations -- CRUD over the `mem.*` schema (Postgres + embedder via _common).

Usage: python3 mem_ops.py <write|search|supersede|get|mark|review_add|review_list|
       review_resolve>   with a JSON payload on stdin.

write     {type, content, title?, importance?, confidence?, project?, lang?,
           subject? {namespace, entity_type, name, aliases?},
           valid_from?, valid_to?, event_time?, supersedes_id?,
           source? {source_type, session_id?, channel?, excerpt?},
           assertions? [{predicate, object_text? | object_number?, unit?}]}
search    {query, k?, types? [..], project?, include_inactive?, max_distance?}
supersede {old_id, ...same as write minus type (defaults to old memory's type)}
get       {id}
mark      {id, status}   -- admin op (consolidator): flip status, close validity window

Retrieval defaults to mem.active_memories (A3: superseded/out-of-window facts are
invisible unless include_inactive). Hybrid RRF (bge-m3 vector + composite fts),
recency + importance as tiebreakers.
"""
import re
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import connect, embed_query, vec_literal

FTS_LANG = os.environ.get("HM_FTS_LANG", "english")
if not re.fullmatch(r"[a-z_]+", FTS_LANG):   # it is interpolated into SQL literals
    FTS_LANG = "english"


def embed(text):
    return vec_literal(embed_query(text))


def ensure_entity(cur, subj):
    cur.execute("""INSERT INTO mem.entities (namespace, entity_type, canonical_name, aliases)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (namespace, entity_type, canonical_name)
                   DO UPDATE SET aliases = (SELECT array(SELECT DISTINCT unnest(
                       mem.entities.aliases || EXCLUDED.aliases)))
                   RETURNING id""",
                (subj["namespace"], subj["entity_type"], subj["name"],
                 subj.get("aliases", [])))
    return cur.fetchone()[0]


def do_write(cur, p, supersedes_id=None):
    subj_id = ensure_entity(cur, p["subject"]) if p.get("subject") else None
    supersedes_id = supersedes_id or p.get("supersedes_id")
    mtype = p.get("type")
    if supersedes_id:
        # Lock the target row FOR UPDATE so two concurrent supersedes of the same memory can't
        # both create an active replacement; refuse if it's no longer active.
        cur.execute("SELECT memory_type, status FROM mem.memories WHERE id=%s FOR UPDATE",
                    (supersedes_id,))
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"supersedes_id {supersedes_id} not found")
        if row[1] != "active":
            raise SystemExit(f"supersedes_id {supersedes_id} is already {row[1]}")
        mtype = mtype or row[0]
    cur.execute("""INSERT INTO mem.memories
        (memory_type, content, title, lang, importance, confidence, subject_entity_id,
         project, valid_from, valid_to, event_time, supersedes_id, metadata, embedding)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) RETURNING id""",
        (mtype, p["content"], p.get("title"), p.get("lang", "ru"),
         p.get("importance", 0.5), p.get("confidence", 0.8), subj_id,
         p.get("project"), p.get("valid_from"), p.get("valid_to"), p.get("event_time"),
         supersedes_id, json.dumps(p.get("metadata", {})), embed(p["content"])))
    mid = cur.fetchone()[0]
    if supersedes_id:
        # Close the old fact's validity window at the moment the new one takes over. LEAST (not
        # COALESCE): if the old row already had a *future* valid_to, clamp it down -- it must not
        # keep counting as active past the supersede point.
        cur.execute("""UPDATE mem.memories SET status='superseded',
                       valid_to = LEAST(COALESCE(valid_to, 'infinity'::timestamptz),
                                        COALESCE(%s::timestamptz, now()))
                       WHERE id=%s""", (p.get("valid_from"), supersedes_id))
    src = p.get("source") or {}
    cur.execute("""INSERT INTO mem.sources (memory_id, source_type, session_id, channel, excerpt)
                   VALUES (%s,%s,%s,%s,%s)""",
                (mid, src.get("source_type", "manual"), src.get("session_id"),
                 src.get("channel"), src.get("excerpt")))
    for a in p.get("assertions", []):
        cur.execute("""INSERT INTO mem.assertions
            (memory_id, subject_entity_id, predicate, object_text, object_number, unit)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (mid, subj_id, a["predicate"], a.get("object_text"),
             a.get("object_number"), a.get("unit")))
    return mid


SEARCH_SQL = """
WITH q AS (
  SELECT %s::vector AS emb,
         (replace(websearch_to_tsquery('{lang}',%s)::text,'&','|')::tsquery
          || replace(websearch_to_tsquery('simple', %s)::text,'&','|')::tsquery) AS ts
),
pool AS (
  SELECT * FROM {src} m
  WHERE (%s::text IS NULL OR m.project = %s OR m.project IS NULL)
    AND (%s::text[] IS NULL OR m.memory_type::text = ANY(%s::text[]))
),
semantic AS (
  -- distance gate: an irrelevant query must return NOTHING (abstention), not top-k noise
  SELECT id, RANK() OVER (ORDER BY embedding <=> (SELECT emb FROM q)) AS rank
  FROM pool WHERE embedding IS NOT NULL
    AND embedding <=> (SELECT emb FROM q) < {maxdist}
  ORDER BY embedding <=> (SELECT emb FROM q) LIMIT 30
),
lexical AS (
  SELECT id, RANK() OVER (ORDER BY ts_rank_cd(fts,(SELECT ts FROM q),1) DESC) AS rank
  FROM pool WHERE fts @@ (SELECT ts FROM q)
  ORDER BY ts_rank_cd(fts,(SELECT ts FROM q),1) DESC LIMIT 30
)
SELECT m.id, m.memory_type::text, m.status::text, m.importance, m.confidence,
       m.created_at::date::text, m.valid_from::date::text, m.valid_to::date::text,
       m.project, m.content,
       (COALESCE(1.0/(60+s.rank),0) + COALESCE(1.0/(60+l.rank),0)
        + 0.002*m.importance
        + CASE WHEN m.created_at > now()-interval '30 days' THEN 0.001 ELSE 0 END) AS score
FROM pool m
LEFT JOIN semantic s ON s.id = m.id
LEFT JOIN lexical  l ON l.id = m.id
WHERE s.id IS NOT NULL OR l.id IS NOT NULL
ORDER BY score DESC LIMIT %s;
"""


def do_search(cur, p):
    src = "mem.memories" if p.get("include_inactive") else "mem.active_memories"
    emb = embed(p["query"])
    q = p["query"]
    types = p.get("types") or None
    proj = p.get("project")
    maxdist = float(p.get("max_distance") or os.environ.get("MEM_SEM_MAXDIST", 0.6))
    cur.execute("SET hnsw.ef_search = 100")
    cur.execute("SET hnsw.iterative_scan = relaxed_order")
    cur.execute(SEARCH_SQL.format(src=src, maxdist=maxdist, lang=FTS_LANG),
                (emb, q, q, proj, proj, types, types, p.get("k", 8)))
    rows = cur.fetchall()
    ids = [r[0] for r in rows]
    if ids and not p.get("include_inactive"):
        cur.execute("""UPDATE mem.memories SET access_count=access_count+1,
                       last_accessed_at=now() WHERE id = ANY(%s)""", (ids,))
    return rows


def fmt_row(r):
    mid, mtype, status, imp, conf, created, vfrom, vto, project, content, _ = r
    flags = f"{mtype} imp={imp:.1f}"
    if status != "active":
        flags += f" [{status}]"
    if project:
        flags += f" @{project}"
    window = f" valid {vfrom or '...'}->{vto or '...'}" if (vfrom or vto) else ""
    return f"[#{mid}] ({flags}, {created}{window}) {content}"


def apply_proposal(cur, action, member_ids, proposal):
    """Apply an approved consolidation proposal -- the same effect the consolidator would
    have had at write time (merge -> one canonical memory + supersede members; supersede ->
    mark losers). Returns a human summary."""
    # Re-validate against the current state: if the group was already consolidated by another
    # pass (no members still active), applying now would create a redundant canonical memory.
    cur.execute("SELECT count(*) FROM mem.active_memories WHERE id = ANY(%s)", (member_ids,))
    if cur.fetchone()[0] == 0:
        return f"stale (members {member_ids} no longer active) -- not applied"
    if action == "merge" and proposal.get("content"):
        mid = do_write(cur, {
            "type": proposal.get("type", "semantic"),
            "content": proposal["content"],
            "importance": min(0.7, max(0.0, float(proposal.get("importance", 0.6)))),
            "source": {"source_type": "consolidation", "channel": "review",
                       "excerpt": f"merged from {member_ids}"}})
        for m in member_ids:
            cur.execute("UPDATE mem.memories SET status='superseded', "
                        "valid_to=COALESCE(valid_to, now()) WHERE id=%s AND status='active'", (m,))
        return f"merged {member_ids} -> [#{mid}]"
    if action == "supersede" and proposal.get("winner_id") in member_ids:
        win = proposal["winner_id"]
        for m in member_ids:
            if m != win:
                cur.execute("UPDATE mem.memories SET status='superseded', "
                            "valid_to=COALESCE(valid_to, now()) WHERE id=%s AND status='active'", (m,))
        return f"superseded {[m for m in member_ids if m != win]} (winner #{win})"
    return "no-op (proposal did not match action)"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    p = json.loads(sys.stdin.read() or "{}")
    conn = connect()
    cur = conn.cursor()
    if cmd == "write":
        mid = do_write(cur, p)
        conn.commit()
        print(f"saved [#{mid}]")
    elif cmd == "supersede":
        old = p.pop("old_id")
        mid = do_write(cur, p, supersedes_id=old)
        conn.commit()
        print(f"saved [#{mid}], superseded [#{old}]")
    elif cmd == "search":
        rows = do_search(cur, p)
        conn.commit()
        if not rows:
            print("(no memories found)")
        for r in rows:
            print(fmt_row(r))
    elif cmd == "mark":
        # admin op for the consolidator: flip status without a replacement memory
        status = p["status"]
        assert status in ("active", "superseded", "retracted", "expired")
        cur.execute("UPDATE mem.memories SET status=%s::mem.memory_status, "
                    "valid_to=COALESCE(valid_to, CASE WHEN %s<>'active' THEN now() END) "
                    "WHERE id=%s RETURNING id", (status, status, p["id"]))
        row = cur.fetchone()
        conn.commit()
        print(f"marked [#{p['id']}] {status}" if row else "(not found)")
    elif cmd == "get":
        cur.execute("""SELECT m.id, m.memory_type::text, m.status::text, m.importance,
                       m.confidence, m.created_at::date::text, m.valid_from::date::text,
                       m.valid_to::date::text, m.project, m.content, 0.0,
                       m.title, m.supersedes_id,
                       (SELECT id FROM mem.memories s WHERE s.supersedes_id = m.id LIMIT 1)
                       FROM mem.memories m WHERE m.id=%s""", (p["id"],))
        r = cur.fetchone()
        if not r:
            print("(not found)")
        else:
            print(fmt_row(r[:11]))
            if r[12]:
                print(f"  supersedes: #{r[12]}")
            if r[13]:
                print(f"  superseded by: #{r[13]}")
            cur.execute("""SELECT predicate, coalesce(object_text, object_number::text),
                           coalesce(unit,'') FROM mem.assertions WHERE memory_id=%s""",
                        (p["id"],))
            for pred, obj, unit in cur.fetchall():
                print(f"  assert: {pred} = {obj}{(' ' + unit) if unit else ''}")
            cur.execute("""SELECT source_type, coalesce(channel,''), coalesce(session_id,''),
                           coalesce(excerpt,'') FROM mem.sources WHERE memory_id=%s""",
                        (p["id"],))
            for st, ch, sid, exc in cur.fetchall():
                print(f"  source: {st}{(' via ' + ch) if ch else ''}"
                      f"{(' -- ' + exc[:120]) if exc else ''}")
    elif cmd == "review_add":
        cur.execute("""INSERT INTO mem.review_queue (action, member_ids, proposal, confidence)
                       VALUES (%s,%s,%s,%s) RETURNING id""",
                    (p["action"], p["member_ids"], json.dumps(p["proposal"]),
                     float(p.get("confidence", 0.0))))
        rid = cur.fetchone()[0]
        conn.commit()
        print(f"review [#{rid}] queued ({p['action']}, conf={p.get('confidence')})")
    elif cmd == "review_list":
        cur.execute("""SELECT id, action, member_ids, confidence, proposal, created_at::date
                       FROM mem.review_queue WHERE status='pending' ORDER BY created_at""")
        rows = cur.fetchall()
        if not rows:
            print("(no pending reviews)")
        for rid, action, members, conf, proposal, created in rows:
            summary = proposal.get("content") or f"winner #{proposal.get('winner_id')}"
            print(f"[R#{rid}] {action} conf={conf:.2f} members={members} ({created})\n    {summary}")
    elif cmd == "review_resolve":
        decision = p["decision"]
        assert decision in ("approved", "rejected")
        # Atomic claim: only one concurrent resolve can flip a pending row, so a merge can't be
        # applied twice. The row is claimed to the final status here and RETURNING gives us the
        # proposal to apply.
        cur.execute("UPDATE mem.review_queue SET status=%s, resolved_at=now() "
                    "WHERE id=%s AND status='pending' RETURNING action, member_ids, proposal",
                    (decision, p["id"]))
        row = cur.fetchone()
        if not row:
            conn.commit()
            print(f"(review #{p['id']} not found or already resolved)")
        else:
            action, members, proposal = row
            msg = apply_proposal(cur, action, members, proposal) if decision == "approved" else "rejected"
            cur.execute("UPDATE mem.review_queue SET note=%s WHERE id=%s", (msg, p["id"]))
            conn.commit()
            print(f"review [#{p['id']}] {decision}: {msg}")
    else:
        raise SystemExit("usage: mem_ops.py write|search|supersede|get|mark|"
                         "review_add|review_list|review_resolve  (JSON on stdin)")
    conn.close()


if __name__ == "__main__":
    main()
