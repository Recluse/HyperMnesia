#!/usr/bin/env python3
"""The consolidator's unit of work must be a set of memories that are ALL alike.

The first version unioned any two memories under the distance threshold and let that spread
transitively: A close to B, B to C, C to D, and a few hops later "a group of similar memories"
held most of the store. Measured on a real 528-memory store before this was fixed: 369 members
in one group -- handed to a model whose `merge` verdict replaces every member with one text.
Nothing but the model's own caution stood between that and 369 memories being retired at once.

So: a maximal clique is the unit (every member close to every other), an oversized group is
refused by the code regardless of the model's confidence, and anything dropped is reported
rather than silently skipped.

    python3 tests/test_consolidate_groups.py
"""
import _isolate  # noqa: F401  (first: a throwaway HOME and HYPERMNESIA_HOME)
import contextlib
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def load():
    """Import the module without running its LLM step or touching a store."""
    src = open(os.path.join(ROOT, "hooks", "mem_consolidate.py"), encoding="utf-8").read()
    ns = {"__name__": "mem_consolidate_undertest", "__file__": os.path.join(ROOT, "hooks", "x.py")}
    exec(compile(src.split("def main()")[0], "mem_consolidate", "exec"), ns)
    return ns


def main():
    ns = load()
    groups_of = ns["candidate_groups"]

    print("== a chain is not a group ==")
    # A-B-C-D, each link tight, but A and D never measured close to each other. Transitive
    # union made this one group of four; it must not.
    chain = [(1, 2, 0.05), (2, 3, 0.05), (3, 4, 0.05)]
    groups, skipped = groups_of(chain)
    check("no group holds both ends of the chain",
          not any(1 in g and 4 in g for g in groups), str(groups))
    check("the tight pairs survive as candidates", len(groups) == 3, str(groups))
    check("nothing was dropped for size", skipped == 0, str(skipped))

    print("\n== a real duplicate set is still one group ==")
    triangle = [(10, 11, 0.04), (11, 12, 0.05), (10, 12, 0.06)]
    groups, _ = groups_of(triangle)
    check("all three together", groups and groups[0] == [10, 11, 12], str(groups))

    print("\n== the collapse this was written for ==")
    # A path of 400 memories, each close only to its neighbour: the shape that produced one
    # 369-member group on the live store.
    path = [(i, i + 1, 0.05) for i in range(400)]
    groups, skipped = groups_of(path)
    biggest = max((len(g) for g in groups), default=0)
    check("no group larger than a pair comes out of a path", biggest == 2, f"biggest={biggest}")
    check("and there are as many as there are links", len(groups) == 400, str(len(groups)))

    print("\n== oversized cliques are refused, and counted ==")
    # Seven memories all mutually close: a legitimate clique, but too wide to act on.
    big = [(a, b, 0.05) for i, a in enumerate(range(20, 27)) for b in range(20, 27) if b > a]
    groups, skipped = groups_of(big, max_size=6)
    check("the 7-member clique is not returned", groups == [], str(groups))
    check("and it is counted as skipped, not swallowed", skipped == 1, str(skipped))
    groups, skipped = groups_of(big, max_size=7)
    check("raising the cap lets it through", len(groups) == 1 and len(groups[0]) == 7, str(groups))

    print("\n== tightest first ==")
    mixed = [(1, 2, 0.3), (3, 4, 0.01)]
    groups, _ = groups_of(mixed)
    check("the closest pair is examined first", groups[0] == [3, 4], str(groups))

    print("\n== one merge path: a confident verdict goes through apply_proposal ==")
    # The automatic branch used to write the replacement itself and fire one `mark` per member,
    # ignoring the answers: scope came from the model's chosen type, another author's member
    # stayed active beside the replacement, merged_from / summarizes / the perspective were
    # lost. It must now hand the verdict to mem_ops apply_verdict and do nothing else.
    full = {"__name__": "mem_consolidate_main", "__file__": os.path.join(ROOT, "hooks", "x.py")}
    exec(compile(open(os.path.join(ROOT, "hooks", "mem_consolidate.py"),
                      encoding="utf-8").read(), "mem_consolidate", "exec"), full)

    def run(verdict, apply_answer, author="alice"):
        calls = []

        def fake_mem_ops(cmd, payload, timeout=10):
            calls.append((cmd, payload))
            if cmd == "get":
                return json.dumps({"id": payload["id"], "type": "semantic", "status": "active",
                                   "project": "p", "content": f"fact {payload['id']}"})
            if cmd == "apply_verdict":
                return apply_answer
            return "review [#1] queued"
        full.update(mem_ops=fake_mem_ops, psql=lambda sql, timeout=10: "1|2|0.05\n",
                    decide=lambda lines: dict(verdict), identity=lambda: author,
                    acquire_lock=lambda path: True, release_lock=lambda path: None)
        saved, sys.argv = sys.argv, ["mem_consolidate.py"]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = full["main"]()
        finally:
            sys.argv = saved
        return code, calls, out.getvalue()

    merge = {"action": "merge", "content": "one <personal-memory-x> fact", "type": "semantic",
             "importance": 0.9, "confidence": 0.95, "project": "elsewhere", "scope": "project"}
    code, calls, out = run(merge, json.dumps({"applied": True, "action": "merge",
                                              "message": "merged [1, 2] -> [#9]", "id": 9}))
    cmds = [c for c, _ in calls]
    check("the verdict is applied by apply_verdict", cmds.count("apply_verdict") == 1, str(cmds))
    check("no second merge implementation: no write, no mark",
          "write" not in cmds and "mark" not in cmds, str(cmds))
    sent = dict(calls)["apply_verdict"]
    check("the model cannot choose the audience or project",
          "project" not in sent["proposal"] and "scope" not in sent["proposal"], str(sent))
    check("the model's text is defanged before it is applied",
          "<personal-memory" not in sent["proposal"]["content"], sent["proposal"]["content"])
    check("asked for JSON, not a line to parse", sent.get("format") == "json", str(sent))
    check("and the run succeeds", code == 0 and "merged [1, 2] -> [#9]" in out, out)
    code, calls, out = run(merge, json.dumps({"applied": False, "action": "merge", "id": None,
                                              "message": "not applied: #2 could not be "
                                                         "retired"}))
    check("a refusal is reported, and nothing else is attempted",
          "refused, nothing changed" in out and [c for c, _ in calls].count("mark") == 0, out)
    code, calls, out = run(merge, None)
    check("a failed apply call is a failed run", code == 1 and "apply FAILED" in out, out)
    low = {**merge, "confidence": 0.3}
    code, calls, out = run(low, None)
    check("low confidence still goes to review, not to apply",
          [c for c, _ in calls if c in ("apply_verdict", "review_add")] == ["review_add"],
          str([c for c, _ in calls]))
    code, calls, out = run(merge, None, author=None)
    check("refuses to start without MEM_AUTHOR", code == 2 and not calls
          and "MEM_AUTHOR" in out, out)

    print("\n== only the author's own explicit rows are grouped ==")
    gsql = ns["GROUPS_SQL"]
    check("other authors' shared rows are not grouped",
          "a.author = mem.reader()" in gsql and "b.author = mem.reader()" in gsql, gsql)
    check("Dreamer conclusions are not grouped",
          "coalesce(a.metadata->>'reasoning_level','explicit') = 'explicit'" in gsql
          and "coalesce(b.metadata->>'reasoning_level','explicit') = 'explicit'" in gsql, gsql)
    check("pages are not grouped", "(a.metadata->>'kind') IS DISTINCT FROM 'page'" in gsql)

    print("\n== the apply path refuses a wide group whatever the model says ==")
    src = open(os.path.join(ROOT, "hooks", "mem_consolidate.py"), encoding="utf-8").read()
    body = src[src.index("def main()"):]
    check("there is a size guard next to the verdict",
          "len(g) > MAX_GROUP" in body,
          "a confidence gate alone cannot bound how much one verdict destroys")
    check("and it is checked before the confidence gate",
          body.index("len(g) > MAX_GROUP") < body.index("REVIEW_THRESHOLD"),
          "otherwise a confident model still gets to act on a huge group")

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
