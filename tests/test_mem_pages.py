#!/usr/bin/env python3
"""Knowledge pages: the sibling document tag, chunking, superseding every prior page, and the
SQL that mirrors a page into the document corpus.

Each of these breaks silently:

  * a project's page is published as a document under a SEPARATE tag, `<project>~mem`. Under the
    project's own tag the existing tooling destroys or misreports it (a full re-ingest prunes
    documents the disk walk did not return, ingest_repo.py starts with DELETE ... WHERE repo=<tag>,
    and freshness counts a document with no file as "no longer on disk");
  * do_page_upsert must supersede ALL prior active pages of the project, not the newest one --
    two tags folded into one each bring a page, and a `LIMIT 1` leaves the second active forever;
  * a long page must be chunked below the embedder's cut, or its tail is lexically findable and
    invisible to the vector leg;
  * page_publish once glued an f-string to a plain string, so the literal text '{lang}' reached
    Postgres as a text-search configuration. The configuration must be a known one and bound as
    a parameter.

No database is needed: the SQL text and the pure functions are checked.

    python3 tests/test_mem_pages.py
"""
import _isolate  # noqa: F401  (first: a throwaway HOME and HYPERMNESIA_HOME)
import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("psycopg2", types.SimpleNamespace())
spec = importlib.util.spec_from_file_location("mem_ops_pages", ROOT / "ingest/mem_ops.py")
mem_ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mem_ops)

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main():
    src = (ROOT / "ingest/mem_ops.py").read_text(encoding="utf-8")

    print("== sibling tag ==")
    check("project -> <project>~mem", mem_ops.mem_repo("my-project") == "my-project~mem")
    check("suffix is non-empty and differs from the tag itself",
          mem_ops.mem_repo("X") != "X" and bool(mem_ops.MEM_DOC_SUFFIX))

    print("== do_page_upsert supersedes every prior page ==")
    body = src[src.index("def do_page_upsert("):src.index("MEM_DOC_SUFFIX = ")]
    # The query only: the docstring next to it describes the old behaviour in the same words.
    q = body[body.index("SELECT id FROM mem.active_memories"):]
    q = q[:q.index('"""')]
    check("prior pages are selected without LIMIT", "LIMIT" not in q.upper(), q)
    check("extra pages are explicitly superseded", "status='superseded'" in body)
    check("and they are retired in belief time, at the new page's own instant",
          "retired_at=(SELECT created_at FROM mem.memories WHERE id=%s)" in body)
    check("without claiming the facts stopped holding (event time untouched)",
          "valid_to" not in body[body.index("for extra in prior[1:]"):])
    check("the page write is a trusted internal write", '"_trusted_metadata": True' in body)

    print("== page chunking ==")
    chunks = mem_ops._page_chunks("## Section\ntext\n\n## Other\nmore text")
    check("a short page splits on '## '", len(chunks) == 2, str(chunks))
    check("the heading lands in heading_path",
          chunks[0][0] == "Section" and chunks[1][0] == "Other", str(chunks))
    check("the heading text stays in the body too", "## Section" in chunks[0][1], str(chunks))
    big = "## Big\n" + "\n\n".join("paragraph " + "x" * 300 for _ in range(40))
    chunks = mem_ops._page_chunks(big, limit=1000)
    check("a long section splits on paragraphs", len(chunks) > 1, f"{len(chunks)} chunk(s)")
    check("no chunk is materially over the limit",
          all(len(t) <= 1000 + 320 for _, t in chunks), str([len(t) for _, t in chunks]))
    joined = "".join(t for _, t in chunks)
    check("nothing is lost while splitting", joined.count("paragraph") == 40,
          f"{joined.count('paragraph')} of 40")
    check("a page without headings is kept", mem_ops._page_chunks("just text")[0][1] == "just text")
    check("an empty page still yields one chunk", len(mem_ops._page_chunks("")) == 1)

    print("== page_publish SQL ==")

    class Cursor:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            self.row = (41,) if "INSERT INTO documents" in sql else None

        def fetchone(self):
            return self.row

    saved = mem_ops.embed, mem_ops.canon_project
    mem_ops.embed = lambda text: "[0]"
    mem_ops.canon_project = lambda cur, name: name
    try:
        cur = Cursor()
        out = mem_ops.do_page_publish(cur, {"project": "my-project",
                                            "content": "## A\none\n\n## B\ntwo"})
        inserts = [(s, p) for s, p in cur.calls if "INSERT INTO chunks" in s]
        check("publishes under the sibling tag", out["repo"] == "my-project~mem", str(out))
        check("one chunk row per section", len(inserts) == 2 == out["chunks"], str(out))
        check("no '{lang}' placeholder reaches SQL",
              all("{" not in s for s, _ in inserts), inserts[0][0])
        check("the configuration is a bound regconfig parameter",
              all("to_tsvector(%s::regconfig,%s)" in s and p[5] == mem_ops.FTS_LANG
                  for s, p in inserts), str(inserts[0]))
        check("placeholders and parameters agree",
              all(s.count("%s") == len(p) for s, p in inserts), str(inserts[0]))

        refused = None
        try:
            mem_ops.do_page_publish(Cursor(), {"project": "my-project", "content": "x",
                                               "lang": "english');DROP TABLE chunks;--"})
        except SystemExit as exc:
            refused = str(exc)
        check("an unknown text-search configuration is refused",
              refused is not None and "text-search configuration" in refused, str(refused))
        try:
            ok_lang = mem_ops.do_page_publish(Cursor(), {"project": "my-project",
                                                         "content": "x", "lang": "simple"})
            check("a stock configuration is accepted", ok_lang["chunks"] == 1)
        except SystemExit as exc:
            check("a stock configuration is accepted", False, str(exc))
    finally:
        mem_ops.embed, mem_ops.canon_project = saved

    print("== reflect refuses to run without an author, and reads its write as JSON ==")
    import contextlib, io
    rsrc = (ROOT / "hooks/mem_reflect.py").read_text(encoding="utf-8")
    rns = {"__name__": "mem_reflect_undertest", "__file__": str(ROOT / "hooks/x.py")}
    exec(compile(rsrc, "mem_reflect", "exec"), rns)
    called = []
    rns.update(identity=lambda: None, mem_ops=lambda *a, **k: called.append(a) or None,
               acquire_lock=lambda path: True, release_lock=lambda path: None)
    argv, sys.argv = sys.argv, ["mem_reflect.py"]
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            code = rns["main"]()
    finally:
        sys.argv = argv
    check("no MEM_AUTHOR: exit 2 before any store call", code == 2 and not called,
          str((code, called)))
    writes = []

    def fake_ops(cmd, payload, timeout=10):
        if cmd == "reflect_group":
            return json.dumps([{"type": "semantic", "content": f"m{i}"} for i in range(6)])
        writes.append(payload)
        return json.dumps({"id": 12})
    rns.update(mem_ops=fake_ops, complete=lambda prompt, text: "## Page\nbody")
    with contextlib.redirect_stdout(io.StringIO()):
        ok = rns["reflect_one"]("demo", False)
    check("page_upsert is asked for JSON and its id is what counts",
          ok is True and writes and writes[0].get("format") == "json", str(writes))

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
