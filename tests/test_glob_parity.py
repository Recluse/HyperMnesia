#!/usr/bin/env python3
"""The Python hook and the Rust MCP server must agree on glob semantics.

hooks/_arch.py exists so the PreToolUse hook injects exactly what the get_constraints tool would
return. Two implementations of one rule drift, and this one drifted silently: `**/` meant
"one-or-more path segments" in Python and "zero-or-more" in Rust, so a component glob like
`**/CHANGELOG.md` matched a root-level file through the tool and NOT through the hook. The hook
then withheld invariants the tool was happy to show — the exact silent Tier-1 failure the whole
freshness story is about, and ci/check_graph_sql_parity.py could never catch it, because that
compares the text of a SQL query, not behaviour.

So: one table of cases, asserted against the Python matcher here, and against the Rust matcher by
the `glob_parity` unit test in mcp-server (same table, kept in sync by this file's own check).

    python3 tests/test_glob_parity.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
from _arch import _glob_matches  # noqa: E402

# (glob, path, should_match) — the shared contract. Keep in sync with GLOB_CASES in main.rs.
CASES = [
    # `**` crosses separators, `*` does not
    ("src/**", "src/main.py", True),
    ("src/**", "src/a/b/main.py", True),
    ("src/*.py", "src/main.py", True),
    ("src/*.py", "src/a/main.py", False),
    # `**/` matches ZERO segments as well as many — the divergence this test exists for
    ("src/**/*.py", "src/main.py", True),
    ("src/**/*.py", "src/a/main.py", True),
    ("src/**/*.py", "src/a/b/main.py", True),
    ("**/CHANGELOG.md", "CHANGELOG.md", True),
    ("**/CHANGELOG.md", "docs/CHANGELOG.md", True),
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/b", True),
    ("a/**/b", "a/x/y/b", True),
    # anchoring: a glob matches the whole path, not a prefix of it
    ("src/main.py", "src/main.py", True),
    ("src/main.py", "src/main.pyc", False),
    ("src", "src/main.py", False),
    # dot-paths are ordinary paths here
    (".github/**", ".github/workflows/ci.yml", True),
    (".gitlab-ci.yml", ".gitlab-ci.yml", True),
    ("*.yml", ".gitlab-ci.yml", True),           # `*` stays inside a segment; there is no '/' here
    ("*.yml", "ci/deploy.yml", False),           # ... so it does not reach into a directory
    # single-char wildcard stays inside a segment
    ("src/?.py", "src/a.py", True),
    ("src/?.py", "src/ab.py", False),
    ("src/?.py", "src/a/b.py", False),
    # regex metacharacters in a glob are literal
    ("docs/a+b.md", "docs/a+b.md", True),
    ("docs/a+b.md", "docs/aab.md", False),
]

failures, ran = [], 0


def check(name, cond):
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


print("python matcher (hooks/_arch.py):")
for glob, path, want in CASES:
    got = _glob_matches(glob, path)
    check(f"{glob!r} vs {path!r} -> {want}", got == want)

print("\nthe Rust side must carry the same table:")
rs = open(os.path.join(ROOT, "mcp-server", "src", "main.rs"), encoding="utf-8").read()
rust_cases = re.findall(r'\("([^"]+)",\s*"([^"]+)",\s*(true|false)\)', rs)
rust_set = {(g, p, v == "true") for g, p, v in rust_cases}
check("mcp-server/src/main.rs defines GLOB_CASES", bool(rust_set))
missing = [c for c in CASES if c not in rust_set]
check(f"every case here is also asserted in Rust ({len(CASES) - len(missing)}/{len(CASES)})",
      not missing)
if missing:
    print("     missing on the Rust side:", missing[:5])

print(f"\n{ran - len(failures)}/{ran} checks passed"
      + (f"; FAILED: {', '.join(failures[:4])}" if failures else ""))
sys.exit(1 if failures else 0)
