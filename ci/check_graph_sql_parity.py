#!/usr/bin/env python3
"""Guard: the Python copy of GRAPH_SQL (hooks/_arch.py) must not drift from the Rust source of
truth (mcp-server/src/main.rs). The hook carries a hand-written copy so it can load the same graph
the MCP server does; if someone extends the Rust query (adds a field, a table) and forgets the
Python side, the hook would silently resolve against a stale shape. This fails CI on any drift.

Compares modulo whitespace (SQL is whitespace-insensitive; the two are formatted differently on
purpose -- multi-line raw string vs. concatenated literal). Exit 1 on mismatch.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))


def _norm(s):
    return re.sub(r"\s+", "", s).rstrip(";")


def main():
    from _arch import GRAPH_SQL as py_sql
    if "--print" in sys.argv:      # emit the Python GRAPH_SQL so CI can run it against a live DB
        print(py_sql)
        return 0
    rs = open(os.path.join(ROOT, "mcp-server", "src", "main.rs"), encoding="utf-8").read()
    m = re.search(r'GRAPH_SQL[^=]*=\s*r#"(.*?)"#', rs, re.S)
    if not m:
        print("FAIL: could not find GRAPH_SQL raw string in mcp-server/src/main.rs")
        return 1
    p, r = _norm(py_sql), _norm(m.group(1))
    if p == r:
        print(f"OK: GRAPH_SQL parity (Python == Rust, {len(p)} chars modulo whitespace)")
        return 0
    print("FAIL: GRAPH_SQL drifted between hooks/_arch.py and mcp-server/src/main.rs")
    for i, (a, b) in enumerate(zip(p, r)):
        if a != b:
            print(f"  first diff at char {i}:")
            print(f"    python: ...{p[max(0, i - 30):i + 30]}...")
            print(f"    rust  : ...{r[max(0, i - 30):i + 30]}...")
            break
    if len(p) != len(r):
        print(f"  length differs: python {len(p)} vs rust {len(r)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
