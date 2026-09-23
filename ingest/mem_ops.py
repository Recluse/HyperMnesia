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
from _common import EMBED_MODEL, connect, embed_query, vec_literal
from _redact import scrub

FTS_LANG = os.environ.get("HM_FTS_LANG", "english")
if not re.fullmatch(r"[a-z_]+", FTS_LANG):   # it is interpolated into SQL literals
    FTS_LANG = "english"


# The write path reuses the QUERY embedder, whose 6000-character cut exists because a query is
# short and must be fast. A memory is neither: a reflect page or a long procedural note runs
# past it, and the row is then stored in full, full-text indexed in full, and findable
# semantically only by its opening -- with no error and nothing recorded. Cap it here at the
# model's real budget instead, and say so on the rare occasion it bites.
MEM_EMBED_MAX_CHARS = int(os.environ.get("MEM_EMBED_MAX_CHARS", "12000"))


def embed(text):
    text = text or ""
    if len(text) > MEM_EMBED_MAX_CHARS:
        sys.stderr.write(
            f"mem_ops: WARNING embedding only the first {MEM_EMBED_MAX_CHARS} of "
            f"{len(text)} characters -- the tail of this memory will be findable by exact word "
            f"but not by meaning. Split it, or raise MEM_EMBED_MAX_CHARS.\n")
        text = text[:MEM_EMBED_MAX_CHARS]
    return vec_literal(embed_query(text))


def _scrub_deep(obj):
    """scrub() every string inside a nested JSON-ish structure (used for metadata)."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [_scrub_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub_deep(v) for k, v in obj.items()}
    return obj



def _proj_key(name):
    return "".join(ch for ch in (name or "").lower() if ch not in "-_ ")


def canon_project(cur, name):
    """Map a project tag to its canonical spelling.

    The extractor emits whatever the model wrote, so one project ends up split across
    'My_Project' / 'My-Project' / 'my-project' and project-scoped recall silently drops the
    variants. Canonical = the structural map's repo tag when one matches (case/punctuation
    insensitive), else the spelling already carried by the most memories, else the name as given.
    """
    if not name:
        return name
    key = _proj_key(name)
    cur.execute("SELECT repo FROM components GROUP BY repo")
    for (repo,) in cur.fetchall():
        if _proj_key(repo) == key:
            return repo
    cur.execute("""SELECT project FROM mem.memories WHERE project IS NOT NULL
                   GROUP BY project ORDER BY count(*) DESC""")
    for (proj,) in cur.fetchall():
        if _proj_key(proj) == key:
            return proj
    return name


def ensure_entity(cur, subj):
    cur.execute("""INSERT INTO mem.entities (namespace, entity_type, canonical_name, aliases)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (namespace, entity_type, canonical_name)
                   DO UPDATE SET aliases = (SELECT array(SELECT DISTINCT unnest(
                       mem.entities.aliases || EXCLUDED.aliases)))
                   RETURNING id""",
                (subj["namespace"], subj["entity_type"], scrub(subj["name"]),
                 [scrub(a) for a in (subj.get("aliases") or [])]))
    return cur.fetchone()[0]


# -- who is asking -----------------------------------------------------------
# `author` says who learned a fact and `scope` says who may see it (sql/mem_multiuser.sql).
# This is where the caller's identity reaches the connection, so mem.active_memories and the
# row-level policies in mem_app_role.sql have something to filter by.
#
# It arrives in the payload as `_identity`, put there by the hooks from the environment. Never
# from a tool argument and never from the searched text: an identity a caller can choose is an
# identity a caller can borrow.
def set_reader(cur, p):
    """Scope this connection. Returns the identity, or None if the caller did not say."""
    who = (p.get("_identity") or "").strip() or None
    if who:
        cur.execute("SELECT set_config('mem.reader', %s, false)", (who,))
    return who


def require_author(who):
    """Writes need a name. A row whose author is a guess is worse than a row not written --
    it is unattributable for ever, and consolidation would later merge it into somebody's."""
    who = (who or "").strip() or None
    if not who:
        raise SystemExit("refusing to write: nobody said who is writing (MEM_AUTHOR is unset). "
                         "A memory with no author cannot be scoped, shared or revoked.")
    return who


# `preference` is about ONE person and is actively wrong injected into somebody else's session.
# Everything else, when it carries a project tag, is about the project -- and that is the whole
# reason a second engineer having memory is worth anything. A row with no project has no
# audience to define, so it stays private whatever its type.
def default_scope(mtype, project):
    if not project or mtype == "preference":
        return "private"
    return "project"


def do_write(cur, p, supersedes_id=None):
    # Redact structured secrets BEFORE persist/embed. This is the single chokepoint for every
    # write path (hook extract, MCP memory_write, supersede, consolidation merge), so a leaked
    # key from a coding session can't land in mem.* or its embedding.
    content = scrub(p["content"])
    title = scrub(p.get("title"))
    subj_id = ensure_entity(cur, p["subject"]) if p.get("subject") else None
    supersedes_id = supersedes_id or p.get("supersedes_id")
    mtype = p.get("type")
    if supersedes_id:
        # Lock the target row FOR UPDATE so two concurrent supersedes of the same memory can't
        # both create an active replacement; refuse if it's no longer active.
        cur.execute("SELECT memory_type, status, author, scope::text, project FROM mem.memories "
                    "WHERE id=%s FOR UPDATE", (supersedes_id,))
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"supersedes_id {supersedes_id} not found")
        if row[1] != "active":
            raise SystemExit(f"supersedes_id {supersedes_id} is already {row[1]}")
        # Superseding is "that fact is no longer what I know". Across authors it is somebody
        # ELSE deciding what you know, and it happens silently -- the old row leaves every
        # retrieval path at once.
        me = (p.get("_identity") or "").strip() or None
        if row[2] and row[2] != me:
            # A row you may READ gets the explanation; one you may not gets the answer a
            # missing id gets. Three distinguishable replies made this an oracle: walking the
            # id space told you, for every row, whether it exists, whether it is private, and
            # who wrote it.
            cur.execute("SELECT mem.may_read(%s, %s::mem.memory_scope, %s)",
                        (row[2], row[3], row[4]))
            if not cur.fetchone()[0]:
                raise SystemExit(f"supersedes_id {supersedes_id} not found")
            raise SystemExit(f"#{supersedes_id} was written by {row[2]}, not by you. Write your "
                             f"own memory, or ask them to supersede theirs -- replacing another "
                             f"author's fact silently is not something this store does.")
        mtype = mtype or row[0]
    author = require_author(p.get("_identity"))
    project = canon_project(cur, p.get("project"))
    # An explicit scope is honoured; otherwise the type and the project decide. A value that is
    # neither known word is NOT coerced to the default -- a scope nobody understood must not
    # quietly become an audience.
    scope = p.get("scope") or default_scope(mtype, project)
    if scope not in ("private", "project"):
        raise SystemExit(f"unknown scope {scope!r}: expected 'private' or 'project'")
    # Publishing into a project you do not belong to puts a row where its members can read it
    # and you cannot -- a way into other people's sessions from outside their project. The
    # policies in mem_app_role.sql refuse it too; this is the readable error.
    if scope == "project":
        cur.execute("SELECT mem.may_publish(%s)", (project,))
        if not cur.fetchone()[0]:
            raise SystemExit(
                f"refusing to write: {author} is not a member of {project!r}, so this memory "
                f"cannot be shared with it. Write it private, or ask for membership.")
    cur.execute("""INSERT INTO mem.memories
        (memory_type, content, title, lang, importance, confidence, subject_entity_id,
         project, valid_from, valid_to, event_time, supersedes_id, metadata, embedding,
         embedding_model, author, scope)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s) RETURNING id""",
        (mtype, content, title, p.get("lang", "ru"),
         p.get("importance", 0.5), p.get("confidence", 0.8), subj_id,
         project, p.get("valid_from"), p.get("valid_to"), p.get("event_time"),
         supersedes_id, json.dumps(_scrub_deep(p.get("metadata", {}))), embed(content),
         EMBED_MODEL, author, scope))
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
                (mid, src.get("source_type", "manual"), scrub(src.get("session_id")),
                 scrub(src.get("channel")), scrub(src.get("excerpt"))))
    for a in p.get("assertions", []):
        cur.execute("""INSERT INTO mem.assertions
            (memory_id, subject_entity_id, predicate, object_text, object_number, unit)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (mid, subj_id, scrub(a["predicate"]), scrub(a.get("object_text")),
             a.get("object_number"), scrub(a.get("unit"))))
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
-- a lexical-only hit must still be semantically plausible: the lexical leg exists to rescue
-- near-misses of the semantic gate (identifiers, unstemmed forms), not to admit a memory that
-- merely shares one incidental token with the query. Without this floor, OR-converted lexical
-- matching defeats abstention.
WHERE s.id IS NOT NULL
   OR (l.id IS NOT NULL AND (m.embedding IS NULL OR m.embedding <=> (SELECT emb FROM q) < {lexdist}))
ORDER BY score DESC LIMIT %s;
"""


# -- abstention: keep the lexical leg honest -------------------------------------------------
# The composite tsvector has a `simple` (no-stopword) component so identifiers and unstemmed
# words match. But the query is OR-converted, so a `simple` query built from raw text matches any
# memory that shares a single stopword ("на", "с", "the", "at") -- a lexical hit with no relevance
# floor that bypasses the semantic distance gate and defeats abstention. Fix at the source: the
# simple-leg QUERY drops stopwords and <=2-char tokens; the stemmed leg is untouched (its config
# already strips stopwords). Identifiers/code tokens (>=3 chars, not stopwords) still get through.
_STOP = set("""a an and are as at be by for from has he in is it its of on or that the to was were
will with this these those there their they them then than not no but if so do does did can could
would should may might into onto over under out up down about after before between through during
я ты он она оно мы вы они и а но или что это как так же для на в во с со к ко по о об от до из за
над под при про без через между у не ни ли бы же то все всё вот еще ещё уже только очень был была
были быть есть нет да их его её ему ей них нем нём них мой моя мое моё твой наш ваш свой""".split())

# Compound tokens (example.com, docs/ops/x.md, search.py) are stored by Postgres as ONE lexeme,
# so splitting them asked the index for lexemes it does not contain and the exact match for every
# host, path and dotted filename silently stopped working. Keep them whole, and exempt them from
# the length/stopword filter, which is there to stop common WORDS from OR-matching the store.
_COMPOUND = re.compile(r"[\w\-]+(?:[./][\w\-]+)+", flags=re.U)
_WORD = re.compile(r"[\w\-]+", flags=re.U)


def _no_neg(q):
    """Strip leading/trailing dashes from every token so nothing becomes a NEGATED lexeme.

    search.py has had this on the stemmed leg for a while; memory search did not, and passed the
    raw prompt straight to websearch_to_tsquery. A prompt containing a `-flag`-style token ("why
    did -O2 break the build") therefore produced a negated lexeme, and the & -> | rewrite turned
    the lexical leg into "every memory NOT containing that word": the lexical ranks feeding RRF
    became arbitrary. Bounded by the lexical distance floor, so the symptom is quietly worse
    ordering rather than an error -- which is why it survived.
    """
    toks = [t.strip("-") for t in _WORD.findall(q or "")]
    toks = [t for t in toks if t]
    return " ".join(toks) if toks else "zzz-no-lexemes-zzz"


def _simple_query(q):
    q, out, pos = q or "", [], 0
    for m in _COMPOUND.finditer(q):
        out += [(t, False) for t in _WORD.findall(q[pos:m.start()])]
        out.append((m.group(0), True))
        pos = m.end()
    out += [(t, False) for t in _WORD.findall(q[pos:])]
    toks = [t if comp else t.strip("-") for t, comp in out
            if comp or (len(t) > 2 and t.lower() not in _STOP)]
    toks = [t for t in toks if t]
    return " ".join(toks) if toks else "zzz-no-lexemes-zzz"   # nothing meaningful -> matches nothing


def do_search(cur, p):
    # `mem.all_memories`, NOT the base table. Swapping in `mem.memories` takes the rule out of
    # the query entirely, and `include_inactive` is a documented tool parameter -- any model
    # turn can set it. History is a narrower question than "everything".
    src = "mem.all_memories" if p.get("include_inactive") else "mem.active_memories"
    emb = embed(p["query"])
    q = p["query"]
    types = p.get("types") or None
    proj = canon_project(cur, p.get("project"))
    # 0.5 (was 0.6): measured on a populated bge-m3 store -- genuine paraphrase recall tops out
    # ~0.45 (8 samples: 0.34-0.45) while unrelated queries start ~0.52 (6 samples: 0.52-0.68);
    # 0.6 let topically-adjacent-but-unrelated queries through as noise instead of abstaining.
    maxdist = float(p.get("max_distance") or os.environ.get("MEM_SEM_MAXDIST", 0.5))
    # lexical-only hits must ALSO sit inside the semantic gate (see SEARCH_SQL). No slack: unrelated
    # memories start ~0.52 on bge-m3, so a looser lexical gate re-admits exactly the noise the
    # semantic gate rejects. The lexical leg still re-ranks (RRF) and can surface plausible hits
    # outside the semantic top-30; identifier queries sit well inside the gate anyway.
    # `.get(KEY) or maxdist`, not `.get(KEY, 0) or maxdist`: the second form made an explicit
    # MEM_LEX_MAXDIST=0 fall back to maxdist, because the default 0 it returned was an int and
    # falsy -- so the console displayed a setting of 0 as in force while 0.5 was in force.
    lexdist = float(os.environ.get("MEM_LEX_MAXDIST") or maxdist)
    cur.execute("SET hnsw.ef_search = 100")
    cur.execute("SET hnsw.iterative_scan = relaxed_order")
    cur.execute(SEARCH_SQL.format(src=src, maxdist=maxdist, lexdist=lexdist, lang=FTS_LANG),
                (emb, _no_neg(q), _simple_query(q), proj, proj, types, types, p.get("k", 8)))
    rows = cur.fetchall()
    ids = [r[0] for r in rows]
    if ids and not p.get("include_inactive"):
        cur.execute("""UPDATE mem.memories SET access_count=access_count+1,
                       last_accessed_at=now() WHERE id = ANY(%s)""", (ids,))
    return rows


def do_nearest(cur, p):
    """Top-1 semantic neighbour among ACTIVE memories + its cosine distance, as JSON.
    Purpose-built for the extractor's novelty gate (isolated from `search` so its text format is
    untouched). Returns {"distance": float, "content": str} or {} if the store is empty."""
    emb = embed(scrub(p["content"]))   # scrub so a would-be-redacted candidate matches its stored form
    proj = canon_project(cur, p.get("project"))
    # exclude synthesized pages: a page summarizes its sources, so it sits close to each of them --
    # counting it here would make the novelty gate suppress capture of the very memories it's built from.
    cur.execute("""SELECT content, (embedding <=> %s::vector) AS dist
                   FROM mem.active_memories
                   WHERE embedding IS NOT NULL
                     AND (metadata->>'kind') IS DISTINCT FROM 'page'
                     AND (%s::text IS NULL OR project = %s OR project IS NULL)
                   ORDER BY embedding <=> %s::vector LIMIT 1""",
                (emb, proj, proj, emb))
    r = cur.fetchone()
    return {"distance": float(r[1]), "content": r[0]} if r else {}


# -- reflect / knowledge pages ----------------------------------------------------------------
def do_reflect_targets(cur, p):
    """Projects with >= min active non-page memories -> JSON [{project, n}]."""
    cur.execute("""SELECT coalesce(json_agg(json_build_object('project',project,'n',n)),'[]')
                   FROM (SELECT project, count(*) n FROM mem.active_memories
                         WHERE (metadata->>'kind') IS DISTINCT FROM 'page' AND project IS NOT NULL
                         GROUP BY project HAVING count(*) >= %s) t""",
                (int(p.get("min", 5)),))
    return cur.fetchone()[0]


def do_reflect_group(cur, p):
    """Active non-page PROJECT-SCOPED memories for one project -> [{id, type, imp, content}].

    `scope='project'` is the whole point of the filter. A knowledge page is written back as a
    project-scoped memory and, where the document side is in use, mirrored into the document
    corpus -- which has no scope at all. A page summarising the author's private rows therefore
    publishes them, in paraphrase, to everyone the page reaches. The page is shared by
    construction, so only shared material may go into it.
    """
    cur.execute("""SELECT coalesce(json_agg(json_build_object(
                     'id',id,'type',memory_type,'imp',importance,'content',content)
                     ORDER BY importance DESC),'[]')
                   FROM mem.active_memories
                   WHERE (metadata->>'kind') IS DISTINCT FROM 'page'
                     AND scope = 'project' AND project = %s""",
                (canon_project(cur, p["project"]),))
    return cur.fetchone()[0]


def do_page_upsert(cur, p):
    """Write the project's knowledge page, superseding its prior page so it never goes stale
    (each reflect run rebuilds it from the current active memories)."""
    proj = canon_project(cur, p.get("project"))
    # The author's OWN prior page. Superseding by id alone would walk around the cross-author
    # refusal do_write makes one line later, and retire a second author's page for the project.
    cur.execute("""SELECT id FROM mem.active_memories
                   WHERE (metadata->>'kind')='page' AND project IS NOT DISTINCT FROM %s
                     AND author = %s
                   ORDER BY id DESC LIMIT 1""",
                (proj, (p.get("_identity") or "").strip() or None))
    row = cur.fetchone()
    # Explicitly project-scoped, never inherited from a default: a page exists to be read by
    # the project, so "private page" is not a state this should reach by accident either way.
    payload = {**p, "project": proj, "type": "semantic", "scope": "project",
               "metadata": {**(p.get("metadata") or {}), "kind": "page"},
               "importance": p.get("importance", 0.6), "confidence": p.get("confidence", 0.7)}
    return do_write(cur, payload, supersedes_id=row[0] if row else None)



def do_stale_list(cur, p):
    """Active memories that are old and have never been re-confirmed or displaced.

    The design has no decay on purpose -- correctness comes from validity windows and explicit
    supersede. But when nothing ever contradicts a fact, an entry whose truth quietly expired
    (hardware swapped, a project phase ended, a preference changed without a negation word)
    keeps scoring like a fresh one and keeps being injected as current. This is the fallback:
    surface the old, never-touched entries so a human can confirm or retire them.

    Preferences and procedures are excluded -- those are stated rules, not facts about a
    mutable world, and they do not rot the same way. Pages are excluded (regenerated anyway).
    """
    days = int(p.get("days", 180))
    cur.execute("""SELECT coalesce(json_agg(json_build_object(
                     'id', id, 'type', memory_type, 'age_days', extract(day from now()-created_at)::int,
                     'project', project, 'content', left(content, 160)) ORDER BY created_at), '[]')
                   FROM mem.active_memories
                   WHERE created_at < now() - make_interval(days => %s)
                     AND memory_type IN ('semantic','episodic','prospective')
                     AND (metadata->>'kind') IS DISTINCT FROM 'page'
                     -- never recalled, or not recalled within the same window: a fact nobody
                     -- has touched in months is exactly the one most likely to have gone stale
                     AND (last_accessed_at IS NULL
                          OR last_accessed_at < now() - make_interval(days => %s))
                   LIMIT %s""", (days, days, int(p.get("limit", 50))))
    return cur.fetchone()[0]


def fmt_row(r):
    mid, mtype, status, imp, conf, created, vfrom, vto, project, content, _ = r
    flags = f"{mtype} imp={imp:.1f}"
    if status != "active":
        flags += f" [{status}]"
    if project:
        flags += f" @{project}"
    window = f" valid {vfrom or '...'}->{vto or '...'}" if (vfrom or vto) else ""
    return f"[#{mid}] ({flags}, {created}{window}) {content}"


def _group_scope(cur, member_ids):
    """The audience a merged row inherits: `private` if ANY member is private. A merge restates
    what was already there; it must not widen who may read it."""
    cur.execute("SELECT bool_or(scope = 'private') FROM mem.memories WHERE id = ANY(%s)",
                (member_ids,))
    r = cur.fetchone()
    return "private" if (r and r[0]) else "project"


def _group_project(cur, member_ids):
    """The project a merged row inherits -- NULL if the members disagree, and then the scope
    above has already kept it private."""
    cur.execute("SELECT DISTINCT project FROM mem.memories WHERE id = ANY(%s)", (member_ids,))
    projects = [x for (x,) in cur.fetchall() if x]
    return projects[0] if len(projects) == 1 else None


def apply_proposal(cur, action, member_ids, proposal, who=None):
    """Apply an approved consolidation proposal -- the same effect the consolidator would
    have had at write time (merge -> one canonical memory + supersede members; supersede ->
    mark losers). Returns a human summary."""
    # Re-validate against the current state: if the group was already consolidated by another
    # pass (no members still active), applying now would create a redundant canonical memory.
    cur.execute("SELECT count(*) FROM mem.active_memories WHERE id = ANY(%s)", (member_ids,))
    if cur.fetchone()[0] == 0:
        return f"stale (members {member_ids} no longer active) -- not applied"
    # Consolidation ends with every member row superseded -- gone from every retrieval path --
    # and, for a merge, one new row carrying what they all said. Across authors that is one
    # person quietly rewriting what another knows, which is what a cross-author supersede is
    # refused for. The group has to be of one mind before it can be of one memory.
    cur.execute("SELECT DISTINCT author FROM mem.memories WHERE id = ANY(%s)", (member_ids,))
    authors = sorted(a for (a,) in cur.fetchall() if a)
    if len(authors) > 1:
        return (f"not applied: {member_ids} were written by {', '.join(authors)}. Merging "
                f"across authors would supersede one person's memory in another's name.")
    if authors and who and authors[0] != who:
        return f"not applied: {member_ids} belong to {authors[0]}, not to {who}."
    # A group spanning two projects has no single audience to inherit, and a project-scoped row
    # with no project fails the CHECK -- which would abort the whole review transaction rather
    # than decline the one proposal.
    if action == "merge" and _group_scope(cur, member_ids) == "project" \
            and _group_project(cur, member_ids) is None:
        return (f"not applied: {member_ids} are shared with different projects, so the merged "
                f"memory would have no audience of its own. Merge within one project.")
    if action == "merge" and proposal.get("content"):
        mid = do_write(cur, {
            "type": proposal.get("type", "semantic"),
            "content": proposal["content"],
            "importance": min(0.7, max(0.0, float(proposal.get("importance", 0.6)))),
            "metadata": {"merged_from": list(member_ids)},
            # The GROUP decides, not the proposal. `proposal` is the consolidating model's
            # output -- untrusted text -- and reading scope/project from it lets a merge of
            # private rows come back shared, or land under a different project's tag.
            "_identity": authors[0] if authors else who,
            "scope": _group_scope(cur, member_ids),
            "project": _group_project(cur, member_ids),
            "source": {"source_type": "consolidation", "channel": "review",
                       "excerpt": f"merged from {member_ids}"}})
        for m in member_ids:
            cur.execute("UPDATE mem.memories SET status='superseded', "
                        "valid_to=COALESCE(valid_to, now()), "
                        "metadata = metadata || jsonb_build_object('superseded_by', %s::bigint) "
                        "WHERE id=%s AND status='active'", (mid, m))
        return f"merged {member_ids} -> [#{mid}]"
    if action == "supersede" and proposal.get("winner_id") in member_ids:
        win = proposal["winner_id"]
        for m in member_ids:
            if m != win:
                cur.execute("UPDATE mem.memories SET status='superseded', "
                            "valid_to=COALESCE(valid_to, now()), "
                            "metadata = metadata || jsonb_build_object('superseded_by', %s::bigint) "
                            "WHERE id=%s AND status='active'", (win, m))
        return f"superseded {[m for m in member_ids if m != win]} (winner #{win})"
    return "no-op (proposal did not match action)"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    p = json.loads(sys.stdin.read() or "{}")
    conn = connect()
    cur = conn.cursor()
    # Before ANY statement: every read surface filters on this, and a connection that never set
    # it sees only what is shared with projects it belongs to -- which for an unidentified
    # caller is nothing.
    set_reader(cur, p)
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
    elif cmd == "nearest":
        print(json.dumps(do_nearest(cur, p)))
    elif cmd == "stale_list":
        print(json.dumps(do_stale_list(cur, p)))
    elif cmd == "reflect_targets":
        # psycopg2 deserializes a json column to a Python object -> re-serialize to real JSON
        print(json.dumps(do_reflect_targets(cur, p)))
    elif cmd == "reflect_group":
        print(json.dumps(do_reflect_group(cur, p)))
    elif cmd == "page_upsert":
        mid = do_page_upsert(cur, p)
        conn.commit()
        print(f"saved [#{mid}]")
    elif cmd == "mark":
        # admin op for the consolidator: flip status without a replacement memory
        status = p["status"]
        assert status in ("active", "superseded", "retracted", "expired")
        # Provenance: record WHAT displaced this memory. The row surviving is not enough --
        # without this link "what replaced this fact, and why" is unanswerable, which was the
        # case for nearly every superseded row before this (the consolidator marks losers separately from
        # writing the replacement, so supersedes_id stays NULL on that path).
        by = p.get("by")
        # Yours only. `mark` retracts a memory -- it leaves every retrieval path at once -- and
        # it took an id and nothing else, so any caller could retire any row in the store by
        # number, including one they could not read.
        cur.execute("SELECT author FROM mem.memories WHERE id=%s FOR UPDATE", (p["id"],))
        owner = cur.fetchone()
        me = (p.get("_identity") or "").strip() or None
        if not owner:
            # Also the answer when row-level security makes it invisible, which is the same
            # answer `get` gives: "you may not touch #123" tells the caller #123 exists.
            print("(not found)")
        elif owner[0] != me:
            raise SystemExit(f"#{p['id']} was written by {owner[0]}, not by you -- "
                             f"retracting another author's memory is not something this does.")
        else:
            cur.execute("UPDATE mem.memories SET status=%s::mem.memory_status, "
                        "valid_to=COALESCE(valid_to, CASE WHEN %s<>'active' THEN now() END), "
                        "metadata = metadata || CASE WHEN %s::bigint IS NULL THEN '{}'::jsonb "
                        "ELSE jsonb_build_object('superseded_by', %s::bigint) END "
                        "WHERE id=%s RETURNING id", (status, status, by, by, p["id"]))
            row = cur.fetchone()
            conn.commit()
            print(f"marked [#{p['id']}] {status}" if row else "(not found)")
    elif cmd == "get":
        # Reads the base table deliberately -- `get` must still show a superseded or
        # out-of-window row, which mem.active_memories excludes by design -- but through
        # mem.may_read, the same rule the views use. Writing the rule out by hand here instead
        # of calling it is how a first version came to check the scope and forget membership,
        # leaving every project-scoped row in every project fetchable by number.
        cur.execute("""SELECT m.id, m.memory_type::text, m.status::text, m.importance,
                       m.confidence, m.created_at::date::text, m.valid_from::date::text,
                       m.valid_to::date::text, m.project, m.content, 0.0,
                       m.title, m.supersedes_id,
                       (SELECT id FROM mem.memories s WHERE s.supersedes_id = m.id
                          AND mem.may_read(s.author, s.scope, s.project) LIMIT 1),
                       m.author, m.scope::text
                       FROM mem.memories m
                       WHERE m.id=%s
                         AND mem.may_read(m.author, m.scope, m.project)""", (p["id"],))
        r = cur.fetchone()
        if not r:
            # Deliberately the same answer as a missing id. "You may not see #123" tells a
            # caller that #123 exists and whose it is -- most of what they wanted.
            print("(not found)")
        else:
            print(fmt_row(r[:11]))
            if r[14]:
                print(f"  author: {r[14]} ({r[15]})")
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
        cur.execute("""INSERT INTO mem.review_queue
                         (action, member_ids, proposal, confidence, author)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (p["action"], p["member_ids"], json.dumps(p["proposal"]),
                     float(p.get("confidence", 0.0)),
                     require_author(p.get("_identity"))))
        rid = cur.fetchone()[0]
        conn.commit()
        print(f"review [#{rid}] queued ({p['action']}, conf={p.get('confidence')})")
    elif cmd == "review_list":
        # Yours. The proposal text is a paraphrase of the memories in the group, so an
        # unfiltered list hands every reader a summary of rows they may not see.
        cur.execute("""SELECT id, action, member_ids, confidence, proposal, created_at::date
                       FROM mem.review_queue
                       WHERE status='pending'
                         AND author IS NOT DISTINCT FROM current_setting('mem.reader', true)
                       ORDER BY created_at""")
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
        # ...and only your own may be claimed. Resolving is destructive: it consumes the row
        # and, on approval, rewrites the memories it names.
        cur.execute("UPDATE mem.review_queue SET status=%s, resolved_at=now() "
                    "WHERE id=%s AND status='pending' "
                    "  AND author IS NOT DISTINCT FROM current_setting('mem.reader', true) "
                    "RETURNING action, member_ids, proposal",
                    (decision, p["id"]))
        row = cur.fetchone()
        if not row:
            conn.commit()
            print(f"(review #{p['id']} not found or already resolved)")
        else:
            action, members, proposal = row
            msg = (apply_proposal(cur, action, members, proposal,
                                  (p.get("_identity") or "").strip() or None)
                   if decision == "approved" else "rejected")
            cur.execute("UPDATE mem.review_queue SET note=%s WHERE id=%s", (msg, p["id"]))
            conn.commit()
            print(f"review [#{p['id']}] {decision}: {msg}")
    else:
        raise SystemExit("usage: mem_ops.py write|search|supersede|get|mark|"
                         "review_add|review_list|review_resolve  (JSON on stdin)")
    conn.close()


if __name__ == "__main__":
    main()
