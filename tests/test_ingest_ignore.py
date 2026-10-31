#!/usr/bin/env python3
"""`.hmignore` — the per-repo "this is on disk and is NOT corpus" list.

Why it needs a test at all: the exclusion has to apply to BOTH enumerations. list_md takes the
git branch in a repository and the walk branch outside one, and a rule that only holds on the
branch someone happened to try is worse than none -- the file it was written for stays in the
index while the ignore file sits there looking like it works.
"""
import _isolate  # noqa: F401  (first: a throwaway HOME and HYPERMNESIA_HOME)
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingest.ingest_repo import ignored_dir, list_md, read_ignore  # noqa: E402

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
    # A directory pattern may be a glob, and the walk must not even enter what it excludes: a
    # workspace with tens of thousands of scratch files under one such folder was read in full
    # (16 s) only for the result to be thrown away. An unreadable directory inside the excluded
    # tree proves it -- os.walk would have to list it to descend, and onerror would hear of it.
    tree(d, "preview-*/\n")
    for sub in ("preview-01", "preview-02/locked"):
        os.makedirs(os.path.join(d, sub))
        with open(os.path.join(d, sub, "x.md"), "w", encoding="utf-8") as f:
            f.write("# x\n")
    os.makedirs(os.path.join(d, "previews"))
    with open(os.path.join(d, "previews", "kept.md"), "w", encoding="utf-8") as f:
        f.write("# x\n")
    locked = os.path.join(d, "preview-02", "locked")
    os.chmod(locked, 0)
    errors = []
    try:
        _, walked = list_md(d, walk=True, onerror=errors.append)
    finally:
        os.chmod(locked, 0o700)
    check("a glob directory pattern takes every folder it matches",
          not any(f.startswith("preview-") for f in walked))
    check("and only those: previews/ is not preview-*/", "previews/kept.md" in walked)
    check("the walk never enters an excluded folder", errors == [])
    check("ignored_dir: prefix and glob forms, and only directory patterns",
          ignored_dir("legacy/sub/", ["legacy/"]) and ignored_dir("preview-9/", ["preview-*/"])
          and not ignored_dir("legacy/", ["legacy"]) and not ignored_dir("previews/", ["preview-*/"]))

with tempfile.TemporaryDirectory() as d:
    # Mutation: remove the ignore file and the same call must return everything again --
    # otherwise the test above would pass for a filter that drops those paths unconditionally.
    tree(d, None)
    _, all_files = list_md(d, walk=True)
    check("without the file nothing is excluded", len(all_files) == 5)

print(f"\n{ok}/{ok + fail} checks passed")
sys.exit(1 if fail else 0)
