#!/usr/bin/env python3
"""`.hmignore` — the per-repo "this is on disk and is NOT corpus" list.

Why it needs a test at all: the exclusion has to apply to BOTH enumerations. list_md takes the
git branch in a repository and the walk branch outside one, and a rule that only holds on the
branch someone happened to try is worse than none -- the file it was written for stays in the
index while the ignore file sits there looking like it works.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingest.ingest_repo import list_md, read_ignore  # noqa: E402

ok = fail = 0


def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}")


def tree(root, ignore=None):
    os.makedirs(os.path.join(root, "legacy", "sub"))
    for p in ("a.md", "keep.md", "secret.md",
              os.path.join("legacy", "b.md"), os.path.join("legacy", "sub", "c.md")):
        with open(os.path.join(root, p), "w", encoding="utf-8") as f:
            f.write("# x\n")
    if ignore is not None:
        with open(os.path.join(root, ".hmignore"), "w", encoding="utf-8") as f:
            f.write(ignore)


def git_init(root):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(["git", "-C", root] + cmd, check=True, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


IGNORE = "# a comment\n\nlegacy/\nsecret.md\n"

with tempfile.TemporaryDirectory() as d:
    tree(d, IGNORE)
    check("comments and blank lines are dropped", read_ignore(d) == ["legacy/", "secret.md"])
    check("no ignore file -> no patterns", read_ignore(tempfile.gettempdir() + "/nope-xyz") == [])

    _, walked = list_md(d, walk=True)
    check("walk branch: a trailing-slash pattern takes the whole subtree",
          not any(f.startswith("legacy/") for f in walked))
    check("walk branch: a plain pattern takes the one file", "secret.md" not in walked)
    check("walk branch: everything else survives", sorted(walked) == ["a.md", "keep.md"])

with tempfile.TemporaryDirectory() as d:
    # The same tree under git, so the OTHER enumeration branch runs. This is the case that
    # would silently keep excluded files in the corpus if the filter sat in only one branch.
    tree(d, IGNORE)
    git_init(d)
    commit, tracked = list_md(d)
    check("git branch really was taken", commit != "nogit")
    check("git branch: subtree excluded", not any(f.startswith("legacy/") for f in tracked))
    check("git branch: file excluded", "secret.md" not in tracked)
    check("git branch: everything else survives", sorted(tracked) == ["a.md", "keep.md"])

with tempfile.TemporaryDirectory() as d:
    # Mutation: remove the ignore file and the same call must return everything again --
    # otherwise the test above would pass for a filter that drops those paths unconditionally.
    tree(d, None)
    _, all_files = list_md(d, walk=True)
    check("without the file nothing is excluded", len(all_files) == 5)

print(f"\n{ok}/{ok + fail} checks passed")
sys.exit(1 if fail else 0)
