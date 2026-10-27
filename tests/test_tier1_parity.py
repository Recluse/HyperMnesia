#!/usr/bin/env python3
"""Tier 1 selection: the hook (hooks/_arch.py) and the MCP server (mcp-server/src/main.rs) must
select the same constraints for the same paths -- not just agree on globs.

tests/test_glob_parity.py pins glob -> regex. Everything after that used to be free to drift,
and had: absolute paths, the unmapped-scope note, undeliverable rules, tie order and cross-repo
edges all behaved differently in the two copies. One JSON fixture now describes a graph, a set
of paths and the exact selection expected; this file asserts it against the Python resolver, and
the `tier1_fixture_parity` unit test in main.rs asserts the same file against Rust
(`cargo test`). A case added here is a case added there.

    python3 tests/test_tier1_parity.py
"""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
from _arch import get_constraints, rel_to_root  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "tier1_cases.json")
failures, ran = [], 0


def check(name, cond, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def subst(v, tmp):
    if isinstance(v, str):
        return v.replace("{tmp}", tmp)
    if isinstance(v, list):
        return [subst(x, tmp) for x in v]
    if isinstance(v, dict):
        return {k: subst(x, tmp) for k, x in v.items()}
    return v


def build_fs(fs, base):
    for d in fs.get("dirs", []):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    for f in fs.get("files", []):
        open(os.path.join(base, f), "w").close()
    for link, target in fs.get("symlinks", []):
        os.symlink(target, os.path.join(base, link))


def case_sensitive_fs(base):
    probe = os.path.join(base, "CaseProbe")
    open(probe, "w").close()
    try:
        return not os.path.exists(os.path.join(base, "caseprobe"))
    finally:
        os.remove(probe)


def selection(case, graph):
    r = get_constraints(case["paths"], graph, case["repo"], case["root"] or None)
    return {
        "touched": [[p, ms] for p, ms in r["touched"]],
        "unmatched": r["unmatched"],
        "outside": r["outside"],
        "direct": sorted(r["direct"]),
        "graph_only": sorted(r["graph_only"]),
        "constraints": [[c["title"], c["_via"]] for c in r["constraints"]],
        "undeliverable": [c["title"] for c in r["undeliverable"]],
        "scope_note": r["scope_note"],
    }


doc = json.load(open(FIXTURE, encoding="utf-8"))
print("python resolver (hooks/_arch.py) against tests/fixtures/tier1_cases.json:")
for case in doc["cases"]:
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.realpath(td)
        if "fs" in case:
            build_fs(case["fs"], tmp)
            if case.get("case_sensitive_fs") and not case_sensitive_fs(tmp):
                print(f"  skip {case['name']} (this filesystem folds case)")
                continue
        c = subst(case, tmp)
        got = selection(c, doc["graph"])
    for key, want in c["expect"].items():
        check(f"{case['name']}: {key}", got[key] == want, f"got {got[key]!r}, want {want!r}")

print("\nrel_to_root edge cases:")
check("trailing slash on the root", rel_to_root("/r/x/a.py", "/r/x/") == ("a.py", True))
check("a sibling with a common prefix is NOT under the root",
      rel_to_root("/r/xy/a.py", "/r/x") == ("/r/xy/a.py", False))
check("root '/' contains every absolute path", rel_to_root("/a.py", "/") == ("a.py", True))
check("no root: an upward escape is outside", rel_to_root("../x.py", None) == ("../x.py", False))

print("\nthe Rust side must consume the same fixture:")
rs = open(os.path.join(ROOT, "mcp-server", "src", "main.rs"), encoding="utf-8").read()
check("main.rs includes tests/fixtures/tier1_cases.json",
      'include_str!("../../tests/fixtures/tier1_cases.json")' in rs)
check("main.rs has the tier1_fixture_parity test", re.search(r"fn tier1_fixture_parity\(", rs) is not None)

print(f"\n{ran - len(failures)}/{ran} checks passed"
      + (f"; FAILED: {', '.join(failures[:4])}" if failures else ""))
sys.exit(1 if failures else 0)
