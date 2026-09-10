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
import json
import os
import stat
import subprocess
import sys
import tempfile

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


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run(answers, env_extra=None, fail=False):
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

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
