#!/usr/bin/env python3
"""`hm ingest` must pick the incremental path when the scope already has documents.

The full path emitted by ingest_repo.py OPENS with DELETE FROM documents WHERE repo=<scope>,
which cascades to chunks and so to every embedding in that scope. Re-running it over a live
corpus is therefore a full re-embed that nothing reports -- the SQL succeeds, the rows come
back, only the vectors are gone and have to be recomputed. The whole reason `hm ingest` exists
is to take the known-hashes snapshot for you, so getting that branch wrong would be worse than
having no wrapper at all.

psql and python are both stubbed and record their argv, so this needs no database.

    python3 tests/test_hm_cli.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HM = os.path.join(ROOT, "hm")

PSQL_STUB = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["STUB_LOG"]
argv = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(["psql"] + argv) + "\n")
sql = ""
if "-c" in argv:
    sql = argv[argv.index("-c") + 1]
if "count(*)" in sql:
    print(os.environ.get("STUB_DOCCOUNT", "0"))
elif "content_hash" in sql:
    print("docs/a.md\tdeadbeef")
    print("docs/b.md\tcafebabe")
sys.exit(0)
'''

PY_STUB = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["STUB_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(["python", os.environ.get("EMBED_REPO", ""),
                        os.environ.get("HM_REPO", "")] + sys.argv[1:]) + "\n")
# ingest_repo.py is expected to leave a .sql file behind for the next step.
for a in sys.argv[1:]:
    if a.endswith(".sql"):
        open(a, "w").write("-- stub\n")
sys.exit(0)
'''

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def write_stub(path, body):
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def run_ingest(doccount):
    """Run `hm ingest` against stubs; returns the recorded argv lines."""
    with tempfile.TemporaryDirectory() as tmp:
        write_stub(os.path.join(tmp, "psql"), PSQL_STUB)
        write_stub(os.path.join(tmp, "pystub"), PY_STUB)
        log = os.path.join(tmp, "log.jsonl")
        src = os.path.join(tmp, "src")
        os.makedirs(src)
        env = dict(os.environ,
                   PATH=tmp + os.pathsep + os.environ.get("PATH", ""),
                   HM_PYTHON=os.path.join(tmp, "pystub"),
                   STUB_LOG=log,
                   STUB_DOCCOUNT=str(doccount),
                   DATABASE_URL="postgresql://stub/stub")
        p = subprocess.run(["sh", HM, "ingest", src, "myrepo"],
                           capture_output=True, timeout=120, env=env)
        lines = [json.loads(l) for l in open(log, encoding="utf-8")] if os.path.exists(log) else []
    return lines, p


def main():
    print("== first ingest of an empty scope ==")
    lines, p = run_ingest(0)
    check("exits 0", p.returncode == 0, p.stderr.decode()[-300:])
    ingest = next((l for l in lines if any("ingest_repo.py" in a for a in l)), None)
    check("calls the ingester", ingest is not None)
    check("no --known-hashes on a first ingest", "--known-hashes" not in (ingest or []),
          "an empty scope has no snapshot to take")

    print("== re-ingest of a scope that already holds documents ==")
    lines, p = run_ingest(12)
    check("exits 0", p.returncode == 0, p.stderr.decode()[-300:])
    ingest = next((l for l in lines if any("ingest_repo.py" in a for a in l)), None)
    check("passes --known-hashes", "--known-hashes" in (ingest or []),
          "without it the re-ingest deletes the scope and every embedding in it")
    idx = (ingest or []).index("--known-hashes") + 1 if "--known-hashes" in (ingest or []) else None
    snap = (ingest or [])[idx] if idx else ""
    check("the snapshot is a file it produced", snap.endswith("known.tsv"), snap)

    print("== the steps that are easy to forget ==")
    embed = next((l for l in lines if any("embed_chunks.py" in a for a in l)), None)
    check("embeds after loading", embed is not None)
    check("scopes the embedder to this repo", (embed or ["", ""])[1] == "myrepo",
          "an unscoped embed walks the whole store")
    create = next((l for l in lines if any("CREATE INDEX" in a for a in l)), None)
    check("builds the ANN index", create is not None,
          "skipping it leaves search working and sequential-scanning forever")
    check("the index build is a no-op when it exists",
          any("IF NOT EXISTS" in a for a in (create or [])))
    order = [i for i, l in enumerate(lines)
             if any("embed_chunks.py" in a for a in l) or any("CREATE INDEX" in a for a in l)]
    check("index is built AFTER the embed, not before", len(order) == 2 and order[0] < order[1],
          "building HNSW on an empty table then filling it is far slower")
    doctor = next((l for l in lines if any("doctor.py" in a for a in l)), None)
    check("finishes by checking the result", doctor is not None)

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
