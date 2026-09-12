#!/usr/bin/env python3
"""Every knob the console offers must be one the code actually reads.

The console lists the pipeline's tunables with, for each, the file that reads it. That list is
written by hand in Rust, and a hand-written list describing code drifts from it -- silently, in
the direction that matters: the console keeps offering a setting, the settings file grows a line,
and nothing ever consults it. The person who set it believes they changed the system.

This is the check that keeps the two together. For each knob:

  * the file named in `read_by` exists;
  * that file, or something it imports from, actually reads the variable.

It found three dead knobs when it was written: MEM_EXTRACT_MODEL and MEM_CONSOLIDATE_MODEL (the
hooks take their model from HM_LLM_MODEL instead) and MEM_FRESHNESS_MAX_AGE (the freshness stamp
it belongs to does not exist here).

Two more things are checked here, because a grep for the key string is not enough on its own:

  * the mention must be a real read, not a line of prose. A key that appears only in a comment
    used to satisfy this test -- which is how a knob can be documented, displayed, and never
    consulted;
  * the reader must be reachable BY THE FILE. The loader is called from two modules; a script
    that goes through neither is a knob the settings file cannot affect, however honestly the
    screen reports it.

    python3 tests/test_settings_knobs.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOBS_RS = os.path.join(ROOT, "console", "src", "settings.rs")

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def reads(path, key):
    """Does this file actually READ the variable, rather than mention it in prose?"""
    for line in open(path, encoding="utf-8"):
        code = line.split("#", 1)[0]
        if key in code and ("environ" in code or "getenv" in code):
            return True
    return False


def knobs():
    """(key, default, read_by) for every Knob literal in settings.rs."""
    src = open(KNOBS_RS, encoding="utf-8").read()
    body = src[src.index("pub const KNOBS"):]
    body = body[:body.index("\n];")]
    out = []
    for m in re.finditer(r'key:\s*"([^"]+)".*?default:\s*"([^"]*)".*?read_by:\s*"([^"]+)"',
                         body, re.S):
        out.append(m.groups())
    return out


def main():
    ks = knobs()
    print(f"== {len(ks)} knobs declared in console/src/settings.rs ==")
    check("the list was parsed at all", len(ks) >= 5, f"found {len(ks)}")

    print("\n== each names a file that exists ==")
    for key, _, read_by in ks:
        path = os.path.join(ROOT, read_by)
        check(f"{key} -> {read_by}", os.path.exists(path))

    print("\n== each variable is actually read by that file (or what it imports) ==")
    for key, _, read_by in ks:
        path = os.path.join(ROOT, read_by)
        if not os.path.exists(path):
            continue
        found, where = reads(path, key), read_by
        if not found:
            # A file may take the value from a sibling module it imports -- _common.py and
            # _llm.py both hold variables on behalf of their callers. Accept that, but only
            # within this repository's own modules.
            for sib in ("ingest/_common.py", "hooks/_llm.py", "hooks/_mem_common.py"):
                sp = os.path.join(ROOT, sib)
                if os.path.exists(sp) and reads(sp, key):
                    found, where = True, sib
                    break
        check(f"{key} is read ({where})", found,
              f"nothing in {read_by} or its siblings reads {key} outside a comment -- "
              f"a knob with no reader")

    print("\n== each reader is a file the settings file can actually reach ==")
    # load_env_file() is called on import by hooks/_mem_common.py and ingest/_common.py. A
    # reader that imports neither never sees the file, so the console would be offering a knob
    # it cannot change -- which is what happened to the three EMBED_* ones.
    loaders = ("_mem_common", "_common")
    for key, _, read_by in ks:
        path = os.path.join(ROOT, read_by)
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        reached = (os.path.basename(path) in ("_mem_common.py", "_common.py")
                   or any(f"import {m}" in text or f"from {m} import" in text for m in loaders))
        check(f"{key}: {read_by} loads the settings file", reached,
              f"{read_by} imports neither loader, so the file never reaches {key}")

    print("\n== the console's list and the loader's allowlist are the same list ==")
    # The loader accepts an exact set of names, not a prefix: HM_PYTHON and HM_LLM_CMD carry the
    # HM_ prefix and decide what gets executed. Two hand-written lists drift, so they are
    # compared here.
    common = open(os.path.join(ROOT, "hooks", "_mem_common.py"), encoding="utf-8").read()
    body = common[common.index("_SETTABLE = frozenset(("):]
    body = body[:body.index("))")]
    settable = set(re.findall(r'"([A-Z][A-Z0-9_]*)"', body))
    for key, _, _ in ks:
        check(f"{key} is in _SETTABLE", key in settable,
              "the console offers it and the loader would drop it")
    # The loader may know a few names the console does not offer (endpoints), but never a name
    # that decides what is executed.
    forbidden = {"HM_PYTHON", "HM_MEM_OPS", "HM_SEARCH", "HM_RERANK", "HM_LLM_CMD", "PATH",
                 "PYTHONPATH"}
    check("no executable-choosing name is settable", not (settable & forbidden),
          str(sorted(settable & forbidden)))

    print("\n== the settings file has a reader at all ==")
    # The whole feature rests on this: the hooks loading the file on import. Without it the
    # console writes a file nobody opens, and every setting in it is decoration.
    common = open(os.path.join(ROOT, "hooks", "_mem_common.py"), encoding="utf-8").read()
    check("hooks/_mem_common.py defines load_env_file", "def load_env_file" in common)
    check("and calls it at import time",
          re.search(r"^load_env_file\(\)", common, re.M) is not None)

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures[:4])}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
