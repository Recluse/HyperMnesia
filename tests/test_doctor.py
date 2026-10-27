#!/usr/bin/env python3
"""ci/doctor.py must report the faults that leave a working-LOOKING system.

Each case here is a store that answers every query without error and still gives wrong or
degraded results: no ANN index, two embedding models in one table, a scope name that differs
only in case. If the doctor calls any of those healthy it is worse than not existing, because
it converts "I never checked" into "I checked and it was fine".

The store is a stubbed `psql` on PATH that answers by matching the SQL on stdin, so no
database, no embedder and no network are needed.

    python3 tests/test_doctor.py
"""
import contextlib
import hashlib
import http.server
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCTOR = os.path.join(ROOT, "ci", "doctor.py")

# A healthy store. Values are lists of rows; each row is a list of column values.
HEALTHY = {
    "SELECT 1": [["1"]],
    "table_schema='public'": [["documents"], ["chunks"], ["components"],
                              ["constraints"], ["relationships"]],
    "table_schema='mem'": [["1"]],
    "hnsw": [["CREATE INDEX chunks_embedding_hnsw ON public.chunks USING hnsw (embedding vector_cosine_ops)"]],
    "FILTER (WHERE embedding IS NULL)": [["1680", "0"]],
    "embedding_model": [["bge-m3", "1680"]],
    "FROM components GROUP BY repo": [["myapp", "12"]],
    "FROM constraints": [["4"]],
}

STUB = r'''#!/usr/bin/env python3
import json, os, sys
answers = json.load(open(os.environ["STUB_ANSWERS"]))
sql = sys.stdin.read()
if os.environ.get("STUB_FAIL"):
    sys.stderr.write("psql: could not connect to server\n")
    sys.exit(2)
for needle, rows in answers.items():
    if needle in sql:
        for r in rows:
            print("\x1f".join(r))
        sys.exit(0)
sys.exit(0)                      # unknown query -> empty result, like a real empty table
'''

failures, ran = [], 0
TEI_SEEN = []


@contextlib.contextmanager
def tei(vector):
    """A one-vector TEI stand-in: /health answers, /embed returns `vector` for every input."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            TEI_SEEN.append(body["inputs"])
            out = json.dumps([vector for _ in body["inputs"]]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run(answers, env_extra=None, fail=False, queue=None):
    """Run the doctor against a stubbed store; returns (findings, exit_code)."""
    with tempfile.TemporaryDirectory() as tmp:
        shim = os.path.join(tmp, "psql")
        with open(shim, "w", encoding="utf-8") as f:
            f.write(STUB)
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        apath = os.path.join(tmp, "answers.json")
        with open(apath, "w", encoding="utf-8") as f:
            json.dump(answers, f)
        env = dict(os.environ,
                   PATH=tmp + os.pathsep + os.environ.get("PATH", ""),
                   STUB_ANSWERS=apath,
                   DATABASE_URL="postgresql://stub/stub",
                   # port 9 (discard) refuses instantly: the embedder check must not reach a
                   # real Ollama that happens to be running on the machine under test.
                   OLLAMA_URL="http://127.0.0.1:9",
                   EMBED_BACKEND="ollama")
        env.pop("HM_REPO", None)
        env.pop("HM_RERANK_URL", None)
        # A private HOME: no settings file, no identity and no extraction queue leak in from
        # the machine running the test.
        env.pop("MEM_AUTHOR", None)
        env["HOME"] = tmp
        if queue is not None:
            os.makedirs(os.path.join(tmp, ".claude"), exist_ok=True)
            with open(os.path.join(tmp, ".claude", "mem-queue.jsonl"), "w") as f:
                for i in range(queue):
                    f.write(json.dumps({"transcript_path": f"/nonexistent/t{i}.jsonl"}) + "\n")
        if fail:
            env["STUB_FAIL"] = "1"
        env.update(env_extra or {})
        p = subprocess.run([sys.executable, DOCTOR, "--json"],
                           capture_output=True, timeout=120, env=env)
    return json.loads(p.stdout.decode() or "[]"), p.returncode


def by_title(findings, needle):
    return next((f for f in findings if needle.lower() in f["title"].lower()), None)


def main():
    print("== a healthy store ==")
    f, code = run(HEALTHY)
    check("Postgres reported reachable", by_title(f, "Postgres reachable") is not None)
    check("ANN index reported present", (by_title(f, "ANN index present") or {}).get("level") == "ok")
    check("all chunks embedded", (by_title(f, "chunks embedded") or {}).get("level") == "ok")
    check("one embedding model", (by_title(f, "One embedding model") or {}).get("level") == "ok")
    check("map scopes listed", (by_title(f, "Map covers") or {}).get("level") == "ok")
    # The embedder is deliberately unreachable in this fixture, so the exit code is 1; what
    # matters is that nothing about the STORE was called a failure.
    store_fails = [x for x in f if x["level"] == "fail" and "mbedder" not in x["title"]]
    check("no false alarms about the store", not store_fails, str(store_fails))

    print("== the ANN index was never built ==")
    ans = dict(HEALTHY); ans.pop("hnsw")
    f, _ = run(ans)
    idx = by_title(f, "No ANN index")
    check("flags the missing index", idx is not None)
    check("says what it costs", "sequential scan" in (idx or {}).get("detail", ""))
    check("hands over the exact CREATE INDEX", "hnsw (embedding vector_cosine_ops)" in (idx or {}).get("fix", ""))

    print("== two embedding models in one store ==")
    ans = dict(HEALTHY); ans["embedding_model"] = [["bge-m3", "1600"], ["nomic-embed-text", "80"]]
    f, code = run(ans)
    mixed = by_title(f, "Mixed embedding")
    check("flags mixed models as a failure", (mixed or {}).get("level") == "fail")
    check("names both models", "nomic-embed-text" in (mixed or {}).get("detail", ""))
    check("exit code is non-zero", code == 1)

    print("== HM_REPO differs from the ingested scope only in case ==")
    # The scope match is an exact string comparison, so this resolves to nothing on every edit
    # while looking like a correctly configured workspace.
    f, _ = run(HEALTHY, env_extra={"HM_REPO": "MyApp"})
    scope = by_title(f, "matches no scope")
    check("flags the unmatched scope", (scope or {}).get("level") == "fail")
    check("points at the near-miss", "myapp" in (scope or {}).get("detail", ""),
          (scope or {}).get("detail", ""))

    print("== a scope with no must-constraints injects nothing ==")
    ans = dict(HEALTHY); ans["FROM constraints"] = [["0"]]
    f, _ = run(ans, env_extra={"HM_REPO": "myapp"})
    check("warns that the hook has nothing to inject",
          (by_title(f, "no active `must`") or {}).get("level") == "warn")

    print("== the store is unreachable ==")
    f, code = run(HEALTHY, fail=True)
    check("reports Postgres unreachable", (by_title(f, "Postgres unreachable") or {}).get("level") == "fail")
    check("skips the checks that depend on it", by_title(f, "ANN index") is None)
    check("still checks the embedder", by_title(f, "Embedder") is not None)
    check("exit code is non-zero", code == 1)

    print("== migrations ==")
    order = subprocess.run(["sh", os.path.join(ROOT, "hm"), "migrate", "--list"],
                           capture_output=True, text=True).stdout.split()
    sums = {}
    for name in order:
        path = os.path.join(ROOT, "sql", name)
        if os.path.isfile(path):
            sums[name] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    tracked = dict(HEALTHY)
    tracked["to_regclass('public.hm_schema_migrations')"] = [["t"]]
    tracked["FROM public.hm_schema_migrations"] = [[n, c] for n, c in sums.items()]
    f, _ = run(tracked)
    check("every file recorded with its checksum is reported current",
          (by_title(f, "migration(s) current") or {}).get("level") == "ok", str(f)[:300])
    gap = dict(tracked)
    gap["FROM public.hm_schema_migrations"] = [[n, c] for n, c in sums.items()
                                               if n != "migrate_dreamer.sql"]
    f, code = run(gap)
    miss = by_title(f, "not applied")
    check("a missing migration is a failure that names it",
          (miss or {}).get("level") == "fail" and "migrate_dreamer.sql" in miss.get("detail", ""),
          str(miss))
    edited = dict(tracked)
    edited["FROM public.hm_schema_migrations"] = [[n, "0" * 64 if n == "schema.sql" else c]
                                                  for n, c in sums.items()]
    f, _ = run(edited)
    check("a file changed since it was applied is a warning that names it",
          "schema.sql" in (by_title(f, "changed since applied") or {}).get("detail", ""))
    with tempfile.TemporaryDirectory() as tmp:
        # A checkout without a file the store never had: ./hm migrate refuses it, and the
        # doctor must not quietly leave it out of the count.
        for name in sums:
            if name != "migrate_dreamer.sql":
                with open(os.path.join(tmp, name), "wb") as out:
                    out.write(open(os.path.join(ROOT, "sql", name), "rb").read())
        f, code = run(gap, env_extra={"HM_SQL_DIR": tmp})
        absent = by_title(f, "neither applied nor in this checkout")
        check("a migration absent from the checkout AND the store is a failure that names it",
              (absent or {}).get("level") == "fail"
              and "migrate_dreamer.sql" in absent.get("detail", "")
              and not by_title(f, "migration(s) current"), str(f)[:400])
    untracked = dict(HEALTHY)
    untracked["to_regclass('public.hm_schema_migrations')"] = [["f"]]
    f, _ = run(untracked)
    check("a store with no version table says migrations are untracked",
          (by_title(f, "not tracked") or {}).get("level") == "warn")

    print("== who the connection is ==")
    owner = dict(HEALTHY)
    owner["FROM pg_roles r"] = [["hm", "t", "f", "t"]]
    owner["count(DISTINCT author)"] = [["2"]]
    f, _ = run(owner)
    rls = by_title(f, "Row-level security is bypassed")
    check("a superuser connection on a two-author store is a failure",
          (rls or {}).get("level") == "fail", str(rls))
    owner["count(DISTINCT author)"] = [["1"]]
    f, _ = run(owner)
    check("on a one-author store it is a warning",
          (by_title(f, "Row-level security is bypassed") or {}).get("level") == "warn")
    app = dict(HEALTHY)
    app["FROM pg_roles r"] = [["hm_alice", "f", "f", "f"]]
    app["FROM mem.whoami()"] = [["hm_alice", "alice", "enforced"]]
    f, _ = run(app, env_extra={"MEM_AUTHOR": "alice"})
    check("an enforced login is reported as enforced",
          (by_title(f, "Identity enforced") or {}).get("level") == "ok")
    f, _ = run(app, env_extra={"MEM_AUTHOR": "bob"})
    check("MEM_AUTHOR disagreeing with the mapped author is a failure",
          (by_title(f, "disagrees") or {}).get("level") == "fail")
    off = dict(app)
    off["NOT c.relrowsecurity"] = [["memories"]]
    f, _ = run(off, env_extra={"MEM_AUTHOR": "alice"})
    check("row-level security switched off on a memory table is a failure, not 'enforced'",
          (by_title(f, "switched off") or {}).get("level") == "fail"
          and not by_title(f, "Identity enforced"), str([x["title"] for x in f]))
    app["FROM mem.whoami()"] = [["hm_ghost", "", "unmapped"]]
    f, _ = run(app)
    check("an unmapped member of hm_app is a failure",
          (by_title(f, "no author mapping") or {}).get("level") == "fail")
    app["FROM pg_roles r"] = [["hm_app", "f", "f", "f"]]
    app["FROM mem.whoami()"] = [["hm_app", "alice", "filter"]]
    f, _ = run(app, env_extra={"MEM_AUTHOR": "alice"})
    shared = by_title(f, "Shared login")
    check("the shared login is reported honestly: claimed, not enforced",
          (shared or {}).get("level") == "ok" and "does not tell people apart"
          in shared.get("detail", ""), str(shared))

    print("== unembedded chunks, per scope ==")
    ans = dict(HEALTHY)
    ans["FILTER (WHERE embedding IS NULL)"] = [["1680", "12"]]
    ans["WHERE c.embedding IS NULL"] = [["api", "9"], ["notes", "3"]]
    f, _ = run(ans)
    detail = (by_title(f, "have no embedding") or {}).get("detail", "")
    check("the count is broken down by scope", "api (9)" in detail and "notes (3)" in detail,
          detail)

    print("== embedding fingerprint ==")
    stored = [0.6, 0.8, 0.0]
    fp = dict(HEALTHY)
    fp["json_build_object('p'"] = [[json.dumps({"p": "Doc > H", "c": "text",
                                                "e": json.dumps(stored)})]]
    for vec, level, title in ((stored, "ok", "matches the stored"),
                              ([0.8, 0.6, 0.0], "warn", "differs from the stored")):
        with tei(vec) as url:
            f, _ = run(fp, env_extra={"EMBED_BACKEND": "tei", "TEI_URL": url})
        got = by_title(f, title)
        check(f"re-embedding stored text: {title} -> {level}",
              (got or {}).get("level") == level, str([x["title"] for x in f]))
        if vec is stored:
            check("the embedder saw the stored text with its context prefix",
                  TEI_SEEN and TEI_SEEN[-1] == ["Doc > H\ntext"], str(TEI_SEEN[-1:]))
    f, _ = run(fp)
    check("an unreachable embedder skips the check and says so",
          (by_title(f, "fingerprint not checked") or {}).get("level") == "ok",
          str([x["title"] for x in f]))

    print("== extraction pipeline ==")
    ext = dict(HEALTHY)
    ext["channel = 'extractor'"] = [[str(10 * 24 * 3600)]]
    f, _ = run(ext, env_extra={"MEM_AUTHOR": "alice"}, queue=4)
    stall = by_title(f, "stalled")
    check("a ten-day-old newest extraction with a queue is flagged",
          (stall or {}).get("level") == "warn" and "4 transcript(s) queued"
          in stall.get("detail", ""), str(stall))
    ext["channel = 'extractor'"] = [["600"]]
    f, _ = run(ext, env_extra={"MEM_AUTHOR": "alice"}, queue=0)
    check("a fresh one is current", (by_title(f, "Extraction is current") or {}).get("level") == "ok")
    f, code = run(ext, env_extra={"MEM_AUTHOR": "alice", "HM_EXTRACT_MAX_AGE_HOURS": "three"},
                  queue=0)
    check("a malformed age limit is reported, and the rest of the report still arrives",
          (by_title(f, "not a positive number") or {}).get("level") == "warn"
          and by_title(f, "Extraction is current") and by_title(f, "Postgres reachable"),
          str([x["title"] for x in f]))
    f, _ = run(HEALTHY, queue=3)
    check("a queue with no identity to write it is flagged",
          (by_title(f, "wait for extraction") or {}).get("level") == "warn")

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
