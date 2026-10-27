#!/usr/bin/env python3
"""Tier 0/1 map against a LIVE Postgres: the real GRAPH_SQL, cross-repo edges, constraint
sources that survive a re-ingest, freshness, and the doc-search loudness knobs.

Needs DATABASE_URL set explicitly, `psql` on PATH and psycopg2 importable; skipped (exit 0)
otherwise. It loads sql/schema.sql + sql/migrate_constraint_sources.sql, RESETS the scopes
`myapp` and `otherapp`, and creates a throwaway login role -- point it at a scratch database:

    DATABASE_URL=postgresql://hm:hm@localhost:5432/hypermnesia python3 tests/test_arch_live.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
from _arch import GRAPH_SQL, get_constraints  # noqa: E402

URL = os.environ.get("DATABASE_URL", "")
if not URL or shutil.which("psql") is None:
    print("SKIP: needs DATABASE_URL and psql (a scratch database; this test resets scopes)")
    sys.exit(0)
try:
    import psycopg2  # noqa: F401
except ImportError:
    print("SKIP: psycopg2 is not importable by this interpreter")
    sys.exit(0)

failures, ran = [], 0


def check(name, cond, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def psql(sql=None, file=None, url=URL, ok=True):
    args = ["psql", url, "-tAX", "-q", "-v", "ON_ERROR_STOP=1"]
    args += ["-f", file] if file else []
    p = subprocess.run(args, input=(sql or "").encode(), capture_output=True, timeout=60)
    if ok and p.returncode != 0:
        raise SystemExit(f"psql failed: {p.stderr.decode()}\n--- {file or sql}")
    return p.stdout.decode().strip()


def run_py(args, env=None, cwd=ROOT, stdin=""):
    e = dict(os.environ, DATABASE_URL=URL, **(env or {}))
    return subprocess.run([sys.executable] + args, input=stdin, capture_output=True, text=True,
                          env=e, cwd=cwd, timeout=120)


def ingest(repo_dir, scope):
    """The same incremental path `hm ingest` takes."""
    out = os.path.join(repo_dir, ".hm-out.sql")
    known = os.path.join(repo_dir, ".hm-known.tsv")
    rows = psql(f"SELECT path || E'\\t' || content_hash FROM documents WHERE repo='{scope}'")
    with open(known, "w") as fh:
        fh.write(rows + ("\n" if rows else ""))
    argv = [os.path.join(ROOT, "ingest", "ingest_repo.py"), repo_dir, scope, out]
    if rows:
        argv += ["--known-hashes", known]
    p = run_py(argv)
    assert p.returncode == 0, p.stderr
    return out


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


print("== schema + migration (twice: idempotent) ==")
psql(file=os.path.join(ROOT, "sql", "schema.sql"))
for _ in range(2):
    psql(file=os.path.join(ROOT, "sql", "migrate_constraint_sources.sql"))
check("constraints has source_repo/source_path/source_hash",
      psql("SELECT count(*) FROM pg_attribute WHERE attrelid='constraints'::regclass "
           "AND attname IN ('source_repo','source_path','source_hash')") == "3")

# A clean slate for the two scopes this test owns.
psql("DELETE FROM constraints WHERE repo IN ('myapp','otherapp');"
     "DELETE FROM relationships WHERE src_component_id IN (SELECT id FROM components WHERE repo IN ('myapp','otherapp'))"
     " OR dst_component_id IN (SELECT id FROM components WHERE repo IN ('myapp','otherapp'));"
     "DELETE FROM components WHERE repo IN ('myapp','otherapp');"
     "DELETE FROM documents WHERE repo IN ('myapp','otherapp');")

tmp = tempfile.mkdtemp(prefix="hm-arch-live-")
try:
    repo = os.path.join(tmp, "myapp")
    for rel, body in {
        "docs/architecture.md": "# Architecture\n\nAll DB access goes through the data layer.\n",
        "src/api/users.py": "x = 1\n", "src/routes/r.py": "", "src/db/models.py": "",
        "migrations/0001.sql": "", "config/app.yml": "a: 1\n", "config/extra.yml": "b: 2\n",
    }.items():
        os.makedirs(os.path.dirname(os.path.join(repo, rel)), exist_ok=True)
        open(os.path.join(repo, rel), "w").write(body)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-qm", "init"], check=True)

    print("== seed BEFORE the docs exist; the first ingest links the rules ==")
    psql(file=os.path.join(ROOT, "examples", "seed_example.sql"))
    check("seed names its source without a doc row yet",
          psql("SELECT count(*) FROM constraints WHERE repo='myapp' AND source_path='docs/architecture.md' "
               "AND source_doc_id IS NULL AND source_hash IS NULL") == "2")

    # A throwaway login with what the app role has on the corpus: it can write documents but
    # NOT constraints. The re-link must still happen (definer trigger).
    psql("DO $$ BEGIN CREATE ROLE hm_live_ingest LOGIN PASSWORD 'live'; "
         "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
         "GRANT USAGE ON SCHEMA public TO hm_live_ingest;"
         "GRANT SELECT, INSERT, UPDATE, DELETE ON documents, chunks TO hm_live_ingest;"
         "GRANT SELECT ON components, constraints, relationships TO hm_live_ingest;"
         "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hm_live_ingest;")
    u = urllib.parse.urlsplit(URL)
    app_url = urllib.parse.urlunsplit((u.scheme, f"hm_live_ingest:live@{u.hostname}:{u.port or 5432}",
                                       u.path, u.query, ""))
    psql(file=ingest(repo, "myapp"), url=app_url)
    h0 = sha(os.path.join(repo, "docs/architecture.md"))
    doc0 = psql("SELECT id FROM documents WHERE repo='myapp' AND path='docs/architecture.md'")
    check("ingest as a login without UPDATE on constraints linked both rules",
          psql(f"SELECT count(*) FROM constraints WHERE repo='myapp' AND source_doc_id={doc0} "
               f"AND source_hash='{h0}'") == "2")
    check("that login really cannot update constraints itself",
          subprocess.run(["psql", app_url, "-tAXq", "-v", "ON_ERROR_STOP=1", "-c",
                          "UPDATE constraints SET source_hash='x' WHERE repo='myapp'"],
                         capture_output=True).returncode != 0)

    print("== a second repo with the SAME slugs, plus one cross-repo edge ==")
    psql("""
    INSERT INTO components (repo,slug,name,responsibility,key_paths,priority) VALUES
      ('otherapp','myapp-api','API','other api',ARRAY['src/api/**'],0),
      ('otherapp','myapp-docs','Docs','other docs',ARRAY['docs/**'],0),
      ('myapp','myapp-cfg','Config','config',ARRAY['config/app.yml'],0);
    INSERT INTO relationships VALUES
      ((SELECT id FROM components WHERE repo='otherapp' AND slug='myapp-api'),
       (SELECT id FROM components WHERE repo='otherapp' AND slug='myapp-docs'),'depends_on'),
      ((SELECT id FROM components WHERE repo='myapp' AND slug='myapp-api'),
       (SELECT id FROM components WHERE repo='otherapp' AND slug='myapp-docs'),'feeds');
    INSERT INTO constraints (repo,kind,scope,component_id,title,statement,severity) VALUES
      ('myapp','convention','component',(SELECT id FROM components WHERE repo='myapp' AND slug='myapp-docs'),
       'Docs rule (myapp)','Docs follow the template.','must'),
      ('otherapp','convention','component',(SELECT id FROM components WHERE repo='otherapp' AND slug='myapp-docs'),
       'Docs rule (otherapp)','Other docs rule.','must'),
      ('myapp','invariant','component',(SELECT id FROM components WHERE repo='myapp' AND slug='myapp-root'),
       'Root rule','Attached to a grouping node.','must');
    """)

    print("== the real GRAPH_SQL ==")
    graph = json.loads(psql(GRAPH_SQL))
    rels = graph["relationships"]
    check("every edge carries the repo of both endpoints",
          all("repo" in e and "dst_repo" in e for e in rels) and len(rels) >= 3, rels)
    check("the cross-repo edge is visible as such",
          any(e["repo"] == "myapp" and e["dst_repo"] == "otherapp" for e in rels))
    r = get_constraints(["src/api/users.py"], graph, "myapp")
    titles = [c["title"] for c in r["constraints"]]
    check("myapp gets its global must and the api should",
          "All DB access goes through the data layer" in titles
          and "Handlers return the response envelope" in titles, titles)
    check("otherapp's same-slug api->docs edge does not pull docs rules into myapp",
          "Docs rule (myapp)" not in titles and "Docs rule (otherapp)" not in titles, titles)
    check("1-hop follows myapp's own api->db edge", r["graph_only"] == {"myapp-db"}, r["graph_only"])
    r2 = get_constraints(["src/api/x.py"], graph, "otherapp")
    check("otherapp follows its own edge", [c["title"] for c in r2["constraints"]] == ["Docs rule (otherapp)"],
          r2["constraints"])

    binary = os.path.join(ROOT, "mcp-server", "target", "release", "hypermnesia-mcp")
    if os.path.exists(binary):
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "get_constraints",
                          "arguments": {"paths": [os.path.join(repo, "src/api/users.py")]}}}
        p = subprocess.run([binary], input=json.dumps(req) + "\n", text=True, capture_output=True,
                           env=dict(os.environ, HM_REPO="myapp", HM_ROOT=repo), timeout=60)
        text = json.loads(p.stdout)["result"]["content"][0]["text"]
        check("the Rust server (absolute path, HM_ROOT) selects the same rules",
              "src/api/users.py -> myapp-api" in text and "Docs rule" not in text
              and all(t in text for t in titles), text)
    else:
        print("  (release binary not built -- Rust leg skipped)")

    print("== freshness: shape checks ==")
    p = run_py([os.path.join(ROOT, "ci", "freshness.py"), repo, "myapp"])
    out = p.stdout
    check("exit 1 because of the cross-repo edge", p.returncode == 1, out + p.stderr)
    check("names the cross-repo edge", "myapp:myapp-api -feeds-> otherapp:myapp-docs" in out, out)
    check("no map orphans", "MAP ORPHANS (0)" in out, out)
    check("sources unchanged after the first ingest", "CONSTRAINT SOURCE CHANGED since the rule was written: 0" in out, out)
    check("the grouping node's rule is never deliverable",
          "'Root rule': component myapp-root has no key_paths" in out, out)
    check("components with no constraint are listed", "myapp-db" in out.split("COMPONENTS WITH NO CONSTRAINT")[1], out)
    check("config/extra.yml is inside a mapped directory and matches nothing",
          "UNMAPPED FILES inside mapped directories (informational): 1" in out and "config/extra.yml" in out, out)

    print("== edit the source document, re-ingest: the link survives, the drift is flagged ==")
    open(os.path.join(repo, "docs/architecture.md"), "a").write("\nRaw SQL is fine now.\n")
    psql(file=ingest(repo, "myapp"))
    doc1 = psql("SELECT id FROM documents WHERE repo='myapp' AND path='docs/architecture.md'")
    check("the document row was replaced", doc1 != doc0)
    check("both rules re-linked to the new row, old hash kept",
          psql(f"SELECT count(*) FROM constraints WHERE repo='myapp' AND source_doc_id={doc1} "
               f"AND source_hash='{h0}'") == "2")
    p = run_py([os.path.join(ROOT, "ci", "freshness.py"), repo, "myapp"])
    check("freshness flags both rules as changed since written",
          "CONSTRAINT SOURCE CHANGED since the rule was written: 2" in p.stdout, p.stdout)
    p = run_py([os.path.join(ROOT, "ci", "freshness.py"), repo, "myapp", "--accept-sources"])
    check("--accept-sources stamps them", "stamped 2 constraint source(s)" in p.stdout, p.stdout)
    p = run_py([os.path.join(ROOT, "ci", "freshness.py"), repo, "myapp"])
    check("... and the flag clears", "CONSTRAINT SOURCE CHANGED since the rule was written: 0" in p.stdout, p.stdout)

    print("== delete the source document ==")
    os.remove(os.path.join(repo, "docs/architecture.md"))
    p = run_py([os.path.join(ROOT, "ci", "freshness.py"), repo, "myapp"])
    check("freshness reports the source as gone",
          "CONSTRAINT SOURCE GONE (the named document no longer exists): 2" in p.stdout, p.stdout)

    print("== doc search: distance floor, unembedded notice, honest query_log ==")
    open(os.path.join(repo, "docs/architecture.md"), "w").write("# Architecture\n\nZebra alpha.\n")
    open(os.path.join(repo, "docs/other.md"), "w").write("# Other\n\nQuokka beta.\n")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)   # the ingester reads the index
    psql(file=ingest(repo, "myapp"))
    e1 = "[" + ",".join(["1"] + ["0"] * 1023) + "]"
    e2 = "[" + ",".join(["0", "1"] + ["0"] * 1022) + "]"
    psql(f"UPDATE chunks SET embedding='{e1}' WHERE document_id=(SELECT id FROM documents WHERE repo='myapp' AND path='docs/architecture.md');"
         f"UPDATE chunks SET embedding='{e2}' WHERE document_id=(SELECT id FROM documents WHERE repo='myapp' AND path='docs/other.md');")
    driver = (
        "import sys, json; sys.path.insert(0, sys.argv[1]); import search;"
        f"search._query_vec = lambda q: '{e1}';"
        "rows = search.search('nothingmatcheslexically', 8, 'myapp');"
        "print(json.dumps({'docs': [r[0] for r in rows], 'unembedded': search.UNEMBEDDED}))")
    ingest_dir = os.path.join(ROOT, "ingest")
    p = run_py(["-c", driver, ingest_dir])
    res = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else {}
    check("without a floor the dense leg returns the unrelated doc too",
          {"myapp:docs/architecture.md", "myapp:docs/other.md"} <= set(res.get("docs", [])), p.stdout + p.stderr)
    p = run_py(["-c", driver, ingest_dir], env={"HM_DOC_MAXDIST": "0.5"})
    res = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else {}
    check("HM_DOC_MAXDIST=0.5 keeps only the near chunk", res.get("docs") == ["myapp:docs/architecture.md"],
          p.stdout + p.stderr)
    logged = psql("SELECT n_results FROM query_log WHERE query='nothingmatcheslexically' ORDER BY id DESC LIMIT 1")
    check("query_log records the number actually returned", logged == "1", logged)
    check("no unembedded chunks counted when all have vectors", res.get("unembedded") == 0, res)
    psql("UPDATE chunks SET embedding=NULL WHERE document_id=(SELECT id FROM documents WHERE repo='myapp' AND path='docs/other.md')")
    p = run_py([os.path.join(ingest_dir, "search.py"), "Quokka", "5", "myapp"],
               env={"OLLAMA_URL": "http://127.0.0.1:9", "EMBED_BACKEND": "ollama"})
    check("search prints the unembedded-chunk notice with its count",
          "!! 1 chunk(s) in this scope have no embedding yet" in p.stdout, p.stdout + p.stderr)
    check("... and still finds the chunk lexically", "myapp:docs/other.md" in p.stdout, p.stdout)
    p = run_py([os.path.join(ingest_dir, "search.py"), "--json", "30", "myapp", "8"],
               env={"OLLAMA_URL": "http://127.0.0.1:9", "EMBED_BACKEND": "ollama"}, stdin="Quokka\n")
    check("--json reports unembedded too", json.loads(p.stdout or "{}").get("unembedded") == 1, p.stdout + p.stderr)

    print("== embed_chunks fails loudly ==")
    p = run_py([os.path.join(ingest_dir, "embed_chunks.py")],
               env={"OLLAMA_URL": "http://127.0.0.1:9", "EMBED_BACKEND": "ollama", "EMBED_REPO": "myapp"})
    check("exits non-zero when a batch was skipped", p.returncode == 1, p.stdout + p.stderr)
    check("says how many were not embedded", "were not embedded" in p.stderr, p.stderr)

    print("== re-pointing a rule at another document moves its source identity ==")
    other_id = psql("SELECT id FROM documents WHERE repo='myapp' AND path='docs/other.md'")
    other_hash = psql("SELECT content_hash FROM documents WHERE repo='myapp' AND path='docs/other.md'")
    psql(f"UPDATE constraints SET source_doc_id={other_id} "
         f"WHERE repo='myapp' AND title='Handlers return the response envelope'")
    check("source_repo/path/hash follow the new document",
          psql("SELECT source_repo||':'||source_path||':'||source_hash FROM constraints "
               "WHERE repo='myapp' AND title='Handlers return the response envelope'")
          == f"myapp:docs/other.md:{other_hash}")
    open(os.path.join(repo, "docs/architecture.md"), "a").write("\nMore.\n")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    psql(file=ingest(repo, "myapp"))
    check("re-inserting the OLD document does not link the rule back to it",
          psql("SELECT source_doc_id FROM constraints WHERE repo='myapp' "
               "AND title='Handlers return the response envelope'") == other_id)
    check("... while the rule still naming it is re-linked as before",
          psql("SELECT d.path FROM constraints c JOIN documents d ON d.id=c.source_doc_id "
               "WHERE c.repo='myapp' AND c.title='All DB access goes through the data layer'")
          == "docs/architecture.md")

    print("== blank globs are no globs ==")
    psql("""INSERT INTO components (repo,slug,name,responsibility,key_paths,priority)
            VALUES ('myapp','blank','Blank','blank globs',ARRAY['', '  '],0);
            INSERT INTO constraints (repo,kind,scope,component_id,title,statement,severity)
            VALUES ('myapp','invariant','component',(SELECT id FROM components WHERE repo='myapp' AND slug='blank'),
                    'Blank rule','Never delivered.','must');""")
    p = run_py([os.path.join(ROOT, "ci", "freshness.py"), repo, "myapp"])
    check("a component whose globs are all blank makes its rule never-deliverable",
          "'Blank rule': component blank has no key_paths" in p.stdout, p.stdout)
    check("blank globs are flagged by name", "BLANK GLOBS" in p.stdout and "blank" in p.stdout.split("BLANK GLOBS")[1], p.stdout)
    check("and are not reported as orphans", "! blank:" not in p.stdout, p.stdout)

    print("== an unreadable source is a finding, not a crash ==")
    arch = os.path.join(repo, "docs/architecture.md")
    os.chmod(arch, 0)
    try:
        p = run_py([os.path.join(ROOT, "ci", "freshness.py"), repo, "myapp"])
    finally:
        os.chmod(arch, 0o644)
    if os.geteuid() == 0:
        print("  (running as root: permissions do not bite, check skipped)")
    else:
        check("reports the source it could not read", "CONSTRAINT SOURCE COULD NOT BE READ: 1" in p.stdout,
              p.stdout + p.stderr)
        check("... and still prints every other finding", "UNMAPPED FILES" in p.stdout and "Traceback" not in p.stderr,
              p.stdout + p.stderr)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    psql("DELETE FROM constraints WHERE repo IN ('myapp','otherapp');"
         "DELETE FROM relationships WHERE src_component_id IN (SELECT id FROM components WHERE repo IN ('myapp','otherapp'))"
         " OR dst_component_id IN (SELECT id FROM components WHERE repo IN ('myapp','otherapp'));"
         "DELETE FROM components WHERE repo IN ('myapp','otherapp');"
         "DELETE FROM documents WHERE repo IN ('myapp','otherapp');"
         "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM hm_live_ingest;"
         "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM hm_live_ingest;"
         "REVOKE ALL ON SCHEMA public FROM hm_live_ingest; DROP ROLE IF EXISTS hm_live_ingest;", ok=False)

print(f"\n{ran - len(failures)}/{ran} checks passed"
      + (f"; FAILED: {', '.join(failures[:4])}" if failures else ""))
sys.exit(1 if failures else 0)
