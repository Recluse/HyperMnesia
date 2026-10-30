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

Reads the same env as everything else: DATABASE_URL, EMBED_BACKEND/OLLAMA_URL/TEI_URL/OPENAI_EMBED_URL,
HM_RERANK_URL, HM_REPO, MEM_AUTHOR (and the shared settings file). Checks are read-only and each
one is bounded by a short timeout, so a dead dependency costs seconds, not a hang.
HM_EXTRACT_MAX_AGE_HOURS (default 72) is how old the newest extracted memory may be before it
counts as a stalled pipeline.
"""
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The settings file and the identity rule, from the one module that defines them. Optional: a
# doctor that cannot import the hooks still checks everything that does not need an identity.
sys.path.insert(0, os.path.join(ROOT, "hooks"))
try:
    import _mem_common                          # noqa: E402  (applies the settings file)
except Exception:                               # pragma: no cover - a partial deployment
    _mem_common = None

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://hm@localhost:5432/hypermnesia")
BACKEND = os.environ.get("EMBED_BACKEND", "ollama").lower()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
TEI_URL = os.environ.get("TEI_URL", "http://localhost:8080").rstrip("/")
OPENAI_EMBED_URL = os.environ.get("OPENAI_EMBED_URL", "http://localhost:1234/v1").rstrip("/")
OPENAI_EMBED_KEY = os.environ.get("OPENAI_EMBED_KEY", "")
# Default it, do not leave it empty. The documented .mcp.json sets HM_RERANK (the script path
# that routes search through the reranker) and never HM_RERANK_URL, whose default lives in
# rerank/search_reranked.py -- so the reranker check was skipped in exactly the configuration
# the docs recommend, and `status`, which advertises "reranker reachable", said nothing at all
# while search quietly fell back to plain RRF.
RERANK_DEFAULT_URL = "http://127.0.0.1:8091"
RERANK_URL = (os.environ.get("HM_RERANK_URL")
              or (RERANK_DEFAULT_URL if os.environ.get("HM_RERANK") else "")).rstrip("/")
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
def identity():
    return _mem_common.identity() if _mem_common else None


def q(sql, timeout=10):
    """Run one query. Returns (rows, error): rows is a list of column-lists.

    As the identity the hooks use, through the same connection option: on the shared login a
    doctor with no identity would see an empty memory store and call that a finding."""
    if not shutil.which("psql"):
        return None, "psql is not on PATH"
    env = dict(os.environ)
    who = identity()
    if who:
        env["PGOPTIONS"] = f"-c mem.reader={who}"
    try:
        p = subprocess.run(["psql", DATABASE_URL, "-tAX", "-F", "\x1f", "-v", "ON_ERROR_STOP=1"],
                           input=sql.encode(), capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as e:
        return None, str(e)
    if p.returncode != 0:
        # Never an empty error: callers test `if err`, and a silent failure would read as success.
        return None, ((p.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
                      or f"psql exited {p.returncode} with no message")
    text = p.stdout.decode("utf-8", "replace").strip()
    return [line.split("\x1f") for line in text.splitlines() if line], None


def http_ok(url, timeout=5, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}),
                                    timeout=timeout) as r:
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
                           'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "CREATE INDEX chunks_embedding_hnsw ON '
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
        # Per scope: a count with no scope sends you to re-embed everything, and the chunks
        # without a vector are usually the newest edits of one repo.
        rows, err = q("SELECT d.repo, count(*) FROM chunks c JOIN documents d "
                      "ON d.id = c.document_id WHERE c.embedding IS NULL "
                      "GROUP BY d.repo ORDER BY 2 DESC, 1")
        by_repo = ", ".join(f"{r[0]} ({r[1]})" for r in rows or []) if not err else ""
        out.append(finding(FAIL if nulls == total else WARN,
                           f"{nulls} of {total} chunks have no embedding",
                           "those chunks can only ever be found by the lexical leg"
                           + (f"; by scope: {by_repo}" if by_repo else ""),
                           "python3 ingest/embed_chunks.py  (EMBED_REPO=<scope> for one scope)"))
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


def check_embedding_fingerprint(out):
    # `embedding_model` is a string from the config, not a fingerprint of the model: an upstream
    # image re-published under the same tag, a changed pooling or normalisation, a quantised
    # build -- all keep the label and move the vectors. Queries then embed into a slightly
    # different space than the corpus and recall degrades with no error. The only real test is
    # to embed stored text again and compare with what is stored.
    rows, err = q("SELECT json_build_object('p', coalesce(context_prefix, ''), 'c', content, "
                  "'e', embedding::text) FROM chunks WHERE embedding IS NOT NULL "
                  "ORDER BY random() LIMIT 3")
    if err or not rows:
        return
    try:
        sample = [json.loads(r[0]) for r in rows]
        stored = [json.loads(x["e"]) for x in sample]
        texts = [f"{x['p']}\n{x['c']}" if x["p"] else x["c"] for x in sample]
    except (ValueError, KeyError, TypeError) as e:
        out.append(finding(WARN, "Embedding fingerprint not checked", f"unreadable sample: {e}"))
        return
    try:
        sys.path.insert(0, os.path.join(ROOT, "ingest"))
        from _common import embed_texts         # noqa: E402  (the embedder ingest uses)
        fresh = embed_texts(texts, timeout=20)
    except Exception as e:                      # unreachable, refused, timed out, no module
        out.append(finding(OK, "Embedding fingerprint not checked",
                           f"the embedder did not answer ({type(e).__name__}: {e})"))
        return

    def cos_dist(a, b):
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return 1.0 - sum(x * y for x, y in zip(a, b)) / (na * nb)

    if len(fresh) != len(stored) or any(len(a) != len(b) for a, b in zip(fresh, stored)):
        out.append(finding(FAIL, "Embedder returns vectors of another shape than the stored ones",
                           "", "The configured embedder is not the one the corpus was built with."))
        return
    worst = max(cos_dist(a, b) for a, b in zip(fresh, stored))
    if worst > 1e-3:
        out.append(finding(WARN, "Embedder output differs from the stored vectors",
                           f"max cosine distance {worst:.2e} over {len(stored)} re-embedded "
                           f"chunk(s); threshold 1e-3",
                           "The model behind the configured name changed (or another backend "
                           "serves it). Queries and corpus no longer share one space: re-embed, or "
                           "point the embedder back at the model the corpus was built with."))
    else:
        out.append(finding(OK, "Embedder matches the stored vectors",
                           f"max cosine distance {worst:.1e} over {len(stored)} chunk(s)"))


def check_migrations(out):
    # The apply order lives in ./hm (one copy); ask it rather than keep a second list here.
    try:
        p = subprocess.run(["sh", os.path.join(ROOT, "hm"), "migrate", "--list"],
                           capture_output=True, text=True, timeout=10)
        order = p.stdout.split() if p.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        order = []
    if not order:
        out.append(finding(WARN, "Migrations not checked", "./hm migrate --list gave no order"))
        return
    rows, err = q("SELECT to_regclass('public.hm_schema_migrations') IS NOT NULL")
    if err or not rows:
        return
    if rows[0][0] != "t":
        out.append(finding(WARN, "Migrations are not tracked",
                           "no hm_schema_migrations table -- nothing can say which sql/ files "
                           "this store has had applied",
                           "./hm migrate  (as the table owner: HM_ADMIN_URL)"))
        return
    rows, err = q("SELECT name, checksum FROM public.hm_schema_migrations")
    if err:
        out.append(finding(WARN, "Cannot read hm_schema_migrations", err,
                           "Re-run ./hm migrate so sql/mem_app_role.sql grants SELECT on it."))
        return
    recorded = {r[0]: r[1] for r in rows}
    missing, changed, absent = [], [], []
    for name in order:
        # The same directory ./hm migrate reads (HM_SQL_DIR overrides it there too).
        path = os.path.join(os.environ.get("HM_SQL_DIR") or os.path.join(ROOT, "sql"), name)
        if not os.path.isfile(path):
            # ./hm migrate refuses an incomplete checkout; skipping the file here instead
            # would let an unapplied migration drop out of the report altogether.
            if name not in recorded:
                absent.append(name)
            continue
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if name not in recorded:
            missing.append(name)
        elif recorded[name] != digest:
            changed.append(name)
    if absent:
        out.append(finding(FAIL, f"{len(absent)} migration(s) neither applied nor in this checkout",
                           ", ".join(absent),
                           "Update this checkout (sql/ is incomplete), then ./hm migrate"))
    if missing:
        out.append(finding(FAIL, f"{len(missing)} migration(s) not applied",
                           ", ".join(missing),
                           "./hm migrate  (as the table owner: HM_ADMIN_URL)"))
    if changed:
        out.append(finding(WARN, f"{len(changed)} migration(s) changed since applied",
                           ", ".join(changed),
                           "./hm migrate --status, then ./hm migrate --reapply <file> after "
                           "reading the change"))
    if not missing and not changed and not absent:
        out.append(finding(OK, f"All {len(recorded)} recorded migration(s) current"))


def check_identity(out):
    # Which trust mode this connection is in -- and whether it is in one at all. A connection as
    # the table owner, a superuser or a BYPASSRLS role skips every policy: it answers every
    # query, correctly, with everybody's rows.
    rows, err = q("SELECT r.rolname, r.rolsuper, r.rolbypassrls, "
                  "coalesce(pg_has_role(current_user, c.relowner, 'MEMBER'), false) "
                  "FROM pg_roles r LEFT JOIN pg_class c ON c.oid = to_regclass('mem.memories') "
                  "WHERE r.rolname = current_user")
    if err or not rows or len(rows[0]) < 4:
        return
    role, sup, bypass, owner = rows[0][0], rows[0][1] == "t", rows[0][2] == "t", rows[0][3] == "t"
    if sup or bypass or owner:
        why = "superuser" if sup else ("BYPASSRLS" if bypass else "owner of mem.memories")
        n = q("SELECT count(DISTINCT author) FROM mem.memories")[0] or [["0"]]
        authors = int(n[0][0]) if n and n[0] and n[0][0].isdigit() else 0
        out.append(finding(FAIL if authors > 1 else WARN,
                           f"Row-level security is bypassed for this connection ({role}: {why})",
                           f"every memory policy is skipped; the store holds {authors} author(s)",
                           "Fine for migrations and restores. The application must connect as "
                           "hm_app or a per-author login (docs/INSTALL.md, \"Two people, one "
                           "store\"); keep this one in HM_ADMIN_URL."))
        return
    # The trust modes below describe what the policies do; with row-level security switched off
    # on a table there are no policies to speak of, for any login but its owner.
    rows, err = q("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                  "WHERE n.nspname = 'mem' AND c.relkind = 'r' AND NOT c.relrowsecurity "
                  "AND c.relname = ANY (ARRAY['memories','entities','assertions','sources',"
                  "'review_queue','derivations','dream_runs','peer_cards']) ORDER BY 1")
    if err:
        # Not knowing is not a pass: everything below would certify policies nobody looked at.
        out.append(finding(WARN, "Row-level security not checked",
                           f"could not read pg_class.relrowsecurity: {err}",
                           "Run ./hm doctor as a role that can read the catalog."))
        return
    if rows and rows[0] and rows[0][0]:
        out.append(finding(FAIL, "Row-level security is switched off",
                           "on mem." + ", mem.".join(r[0] for r in rows) +
                           " -- every login that can read these tables reads every row",
                           "./hm migrate --reapply mem_app_role.sql  (as the table owner)"))
        return
    rows, err = q("SELECT login, coalesce(author, ''), mode FROM mem.whoami()")
    if err or not rows or len(rows[0]) < 3:
        return
    login, author, mode = rows[0]
    who = identity()
    if mode == "enforced":
        if who and who != author:
            out.append(finding(FAIL, "MEM_AUTHOR disagrees with the database",
                               f"login {login} is author {author!r}, MEM_AUTHOR is {who!r}",
                               "Every write under MEM_AUTHOR will be refused. Set MEM_AUTHOR to "
                               "the mapped author."))
        else:
            out.append(finding(OK, "Identity enforced by the database",
                               f"login {login} is author {author!r}; mem.reader is ignored"))
    elif mode == "bypassable":
        why = q("SELECT mem.author_login_problem(session_user)")[0] or [[""]]
        out.append(finding(FAIL, "This author login is not enforced",
                           f"login {login} is mapped to {author!r}, but "
                           f"{(why[0][0] if why and why[0] else '') or 'it holds a role that gets around the mapping'}",
                           "Revoke that role from the login (docs/INSTALL.md, \"Two people, one "
                           "store\"); ./hm doctor re-checks it."))
    elif mode == "unmapped":
        out.append(finding(FAIL, "This login has no author mapping",
                           f"{login} is not hm_app and not in mem.author_roles: it reads "
                           f"nothing and every write is refused",
                           "./hm author add <author> <login>, as the table owner"))
    else:
        out.append(finding(OK, "Shared login: identity is claimed, not enforced",
                           f"login {login} reads as {author or '(nobody)'}; row-level security "
                           f"filters by that claim, it does not tell people apart",
                           "One login per author makes the database enforce it: ./hm author add"))


# Where the hooks keep them: HYPERMNESIA_HOME/state once ci/install.py has created it, ~/.claude
# until then (_mem_common.state_path). A doctor that cannot import the hooks looks where an
# install that never ran the installer keeps them.
def _state_path(name):
    if _mem_common is not None:
        return _mem_common.state_path(name)
    return os.path.expanduser(os.path.join("~", ".claude", name))


QUEUE = _state_path("mem-queue.jsonl")
DONE = _state_path("mem-queue.done")
REEXTRACT_BYTES = 10 * 1024 * 1024              # hooks/mem_extract.py: done_key()


def queue_depth():
    """Transcripts the extractor has not finished, counted the way it counts them."""
    if not os.path.exists(QUEUE):
        return 0
    try:
        with open(DONE, encoding="utf-8") as fh:
            done = set(fh.read().splitlines())
    except OSError:
        done = set()
    pending = set()
    with open(QUEUE, encoding="utf-8") as fh:
        for line in fh:
            try:
                tp = json.loads(line).get("transcript_path")
            except (ValueError, AttributeError):
                continue
            if not tp:
                continue
            try:
                key = f"{tp}#{os.path.getsize(tp) // REEXTRACT_BYTES}"
            except OSError:
                key = tp
            if key not in done:
                pending.add(key)
    return len(pending)


def check_extraction(out):
    # The extractor runs from a timer, so it can stop for good and say so nowhere: the console
    # judges it by its exit code and the store keeps answering from what it already holds.
    try:
        depth = queue_depth()
    except OSError as e:
        depth = None
        out.append(finding(WARN, "Extraction queue unreadable", f"{QUEUE}: {e}"))
    who = identity()
    if not who:
        if depth:
            out.append(finding(WARN, f"{depth} transcript(s) wait for extraction",
                               "and MEM_AUTHOR is unset, so the extractor cannot write any of them",
                               f"Set MEM_AUTHOR ({getattr(_mem_common, 'ENV_FILE', None) or 'hypermnesia.env'})."))
        return
    lit = "'" + who.replace("'", "''") + "'"
    rows, err = q("SELECT coalesce(extract(epoch FROM now() - max(m.created_at))::bigint, -1) "
                  "FROM mem.memories m JOIN mem.sources s ON s.memory_id = m.id "
                  f"WHERE s.channel = 'extractor' AND m.author = {lit}")
    if err or not rows or not rows[0][0].lstrip("-").isdigit():
        return
    age = int(rows[0][0])
    raw = os.environ.get("HM_EXTRACT_MAX_AGE_HOURS", "") or "72"
    try:
        limit_h = float(raw)
        if not limit_h > 0:
            raise ValueError
    except ValueError:
        out.append(finding(WARN, "HM_EXTRACT_MAX_AGE_HOURS is not a positive number",
                           f"got {raw!r}; judging extraction age against 72 h"))
        limit_h = 72.0
    queued = "" if depth is None else f"; {depth} transcript(s) queued"
    if age < 0:
        out.append(finding(WARN if depth else OK, "No extracted memories yet",
                           f"nothing written by the extractor for {who}{queued}",
                           "Run hooks/mem_extract.py, or check its timer." if depth else ""))
    elif age > limit_h * 3600:
        out.append(finding(WARN, "Extraction looks stalled",
                           f"newest extracted memory for {who} is {age / 3600:.0f} h old "
                           f"(limit {limit_h:g} h){queued}",
                           "Run hooks/mem_extract.py by hand and read its output; check its timer."))
    else:
        out.append(finding(OK, "Extraction is current",
                           f"newest extracted memory {age / 3600:.1f} h old{queued}"))


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
    elif BACKEND == "openai":
        # Every OpenAI-style server lists its models here; LM Studio and llama-server answer it
        # without a key, and the ones that want one get it.
        ok, how = http_ok(f"{OPENAI_EMBED_URL}/models",
                          headers={"Authorization": f"Bearer {OPENAI_EMBED_KEY}"} if OPENAI_EMBED_KEY else None)
        name, url = "OpenAI-compatible", OPENAI_EMBED_URL
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
        # Not configured at all is not a fault -- but say that it was not checked, rather than
        # let a silent skip read as a pass in a report whose whole job is to be complete.
        out.append(finding(OK, "Reranker not configured", "neither HM_RERANK nor HM_RERANK_URL is set",
                           ""))
        return
    ok, how = http_ok(f"{RERANK_URL}/health")
    if ok:
        out.append(finding(OK, "Reranker reachable", RERANK_URL))
    else:
        out.append(finding(WARN, "Reranker unreachable", f"{RERANK_URL}: {how}",
                           "Search fails OPEN to plain RRF order, so this looks like working "
                           "search with quietly worse ranking rather than an error."))


CHECKS = [check_schema, check_migrations, check_identity, check_ann_index, check_embeddings,
          check_embedding_model, check_map, check_extraction]


def run():
    out = []
    if check_database(out):
        for c in CHECKS:
            c(out)
    check_embedder(out)
    if out and out[0]["title"] == "Postgres reachable":
        check_embedding_fingerprint(out)
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
