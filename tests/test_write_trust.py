#!/usr/bin/env python3
"""Reserved write fields: only the store's own machinery may classify a memory.

A row's layer lives in metadata (`reasoning_level`, `kind`). If a caller can set it, an
unreviewed memory_write is cited by memory_ask as an approved Dreamer conclusion, or hidden as
a page. These checks pin the Python half of that boundary (the MCP half is a Rust unit test,
memory_write_and_supersede_drop_undeclared_keys):

  * a payload arriving on stdin loses every `_trusted*` key before dispatch;
  * do_write drops reserved metadata unless an in-process caller marked it trusted;
  * the Dreamer and merge paths are such callers, and a merge keeps a shared perspective;
  * the embedder is called before the transaction takes any row lock.

    python3 tests/test_write_trust.py
"""
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("psycopg2", types.SimpleNamespace())
spec = importlib.util.spec_from_file_location("mem_ops_trust", ROOT / "ingest/mem_ops.py")
mem_ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mem_ops)

events = []
mem_ops.embed = lambda text: events.append("embed") or "[0]"


class WriteCursor:
    def __init__(self):
        self.inserted = None
        self.entities = []

    def execute(self, sql, params=None):
        self.row, self.rows = None, []
        if "SELECT repo FROM components" in sql or "SELECT project FROM mem.memories" in sql:
            pass
        elif "INSERT INTO mem.entities" in sql:
            events.append("entity")
            self.entities.append(params)
            self.row = (10 + len(self.entities),)
        elif "mem.may_publish" in sql:
            self.row = (True,)
        elif "INSERT INTO mem.memories" in sql:
            self.inserted = params
            self.row = (99,)
        elif ("INSERT INTO mem.sources" in sql or "INSERT INTO mem.assertions" in sql
              or "INSERT INTO mem.derivations" in sql):
            pass
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def stored_metadata(cur):
    # metadata is the 15th value of the INSERT (see do_write's column list)
    return json.loads(cur.inserted[14])


forged = {"kind": "dream", "reasoning_level": "inductive", "dream_run_id": 7,
          "merged_from": [1], "topic": "kept"}

# (a) an external payload cannot classify its own row
cur = WriteCursor()
events.clear()
mem_ops.do_write(cur, {"type": "semantic", "content": "a plain fact", "_identity": "alice",
                       "metadata": dict(forged)})
assert stored_metadata(cur) == {"topic": "kept"}, stored_metadata(cur)
# D10: the HTTP embed happens before any row lock is taken
assert events[0] == "embed" and "entity" in events, events

# (b) an internal caller that says so keeps it
cur = WriteCursor()
mem_ops.do_write(cur, {"type": "semantic", "content": "a derived fact", "_identity": "alice",
                       "metadata": dict(forged), "_trusted_metadata": True})
assert stored_metadata(cur)["reasoning_level"] == "inductive"

# ...and a payload from stdin can never say so: main() strips `_trusted*` before dispatch
seen = {}


class Conn:
    def cursor(self):
        return WriteCursor()

    def commit(self):
        pass

    def close(self):
        pass


saved = (mem_ops.connect, mem_ops.do_write, sys.stdin, sys.argv)
try:
    mem_ops.connect = lambda: Conn()
    mem_ops.do_write = lambda c, p, supersedes_id=None: seen.update(p) or 1
    sys.stdin = io.StringIO(json.dumps({"type": "semantic", "content": "x",
                                        "metadata": forged, "_trusted_metadata": True,
                                        "_trusted": 1}))
    sys.argv = ["mem_ops.py", "write"]
    out = io.StringIO()
    real_stdout, sys.stdout = sys.stdout, out
    try:
        mem_ops.main()
    finally:
        sys.stdout = real_stdout
finally:
    mem_ops.connect, mem_ops.do_write, sys.stdin, sys.argv = saved
assert not any(k.startswith("_trusted") for k in seen), seen
assert seen["metadata"] == forged     # stripped later, in do_write, as checked above

# (c) the merge path keeps a perspective all members share, and records it when they differ
alice = ("identity", "peer", "alice", [])
infra = ("project", "repository", "infra", [])


class MergeCursor:
    def __init__(self, pairs):
        self.pairs = pairs

    def execute(self, sql, params=None):
        self.row, self.rows = None, []
        if "SELECT count(*) FROM mem.active_memories" in sql:
            self.row = (2,)
        elif "SELECT DISTINCT author" in sql:
            self.rows = [("alice",)]
        elif "SELECT DISTINCT m.observer_entity_id" in sql:
            self.rows = [(10 + i, 20 + i) + o + d for i, (o, d) in enumerate(self.pairs)]
        elif "SELECT bool_or(scope = 'private')" in sql:
            self.row = (True,)
        elif "SELECT DISTINCT project" in sql:
            self.rows = [("sample-project",)]
        elif ("UPDATE mem.memories SET status='superseded'" in sql
              or "lower(btrim(content))" in sql):
            pass
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


written = []
original_write = mem_ops.do_write
mem_ops.do_write = lambda c, p, supersedes_id=None: written.append(p) or 77
try:
    msg = mem_ops.apply_proposal(MergeCursor([(alice, infra)]), "merge", [1, 2],
                                 {"content": "the merged fact"}, who="alice")
    assert msg == "merged [1, 2] -> [#77]", msg
    merged = written[-1]
    assert merged["observer"]["name"] == "alice" and merged["observed"]["name"] == "infra"
    assert merged["_trusted_metadata"] is True and merged["metadata"]["merged_from"] == [1, 2]

    mem_ops.apply_proposal(MergeCursor([(alice, infra), (alice, alice)]), "merge", [1, 2],
                           {"content": "the merged fact"}, who="alice")
    merged = written[-1]
    assert "observer" not in merged and "observed" not in merged
    assert {"observer": "alice", "observed": "infra"} in merged["metadata"]["merged_perspectives"]

    # The Dreamer approval path is trusted too: its reasoning level must survive do_write.
    mem_ops.apply_proposal(MergeCursor([(alice, infra)]), "derive", [1, 2], {
        "reasoning_level": "inductive", "content": "A grounded recurring project pattern.",
        "confidence": 0.8, "importance": 0.5}, who="alice")
finally:
    mem_ops.do_write = original_write
assert written[-1]["_trusted_metadata"] is True
assert written[-1]["metadata"]["reasoning_level"] == "inductive"
print("write trust checks passed")
