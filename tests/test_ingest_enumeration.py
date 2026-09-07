#!/usr/bin/env python3
"""Enumeration contract for ingest_repo.py -- the failure this pins is SILENT.

A directory that sits inside a git repository but is itself fully .gitignore'd makes both git
calls succeed while `git ls-files` returns nothing, so the old auto-detect never reached its
os.walk fallback and ingested zero documents with exit 0. That is worse than it sounds: the
emitted SQL opens with DELETE FROM documents WHERE repo = <tag>, so applying an "empty" ingest
deletes every document the repo already had. Nothing in the output says so, and the symptom
arrives much later as search answering "no results".

So: an empty enumeration must exit non-zero and write no file, and --walk must be able to
index a git-ignored tree. No database and no embedder -- the ingester writes SQL, and its
exit code and output file are what we assert on.

    python3 tests/test_ingest_enumeration.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INGEST = os.path.join(ROOT, "ingest", "ingest_repo.py")

failures = []
ran = 0


def check(name, cond):
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ingest(repo_dir, out, *extra):
    res = subprocess.run([sys.executable, INGEST, repo_dir, "testrepo", out, *extra],
                         capture_output=True, text=True)
    return res.returncode, res.stderr


def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        notes = os.path.join(repo, "notes")
        os.makedirs(notes)
        git(repo, "init")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        open(os.path.join(repo, ".gitignore"), "w").write("notes/\n")
        open(os.path.join(repo, "tracked.md"), "w").write("# Tracked\n\nin git\n")
        open(os.path.join(notes, "runbook.md"), "w").write("# Runbook\n\nignored by git\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "one")

        print("a git-ignored directory inside a repo (issue #1):")
        out = os.path.join(tmp, "ignored.sql")
        rc, err = ingest(notes, out)
        check("exits non-zero instead of emitting an empty corpus", rc != 0)
        check("says how to fix it", "--walk" in err)
        check("writes no file, so a stale one cannot be applied by mistake",
              not os.path.exists(out))

        print("--walk indexes it:")
        rc, err = ingest(notes, out, "--walk")
        body = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        check("exits 0", rc == 0)
        check("finds the git-ignored document", "'runbook.md'" in body)
        check("records no commit, since untracked content has no relation to HEAD",
              "'nogit'" in body)

        print("unchanged behaviour elsewhere:")
        out2 = os.path.join(tmp, "tracked.sql")
        rc, _ = ingest(repo, out2)
        tracked = open(out2, encoding="utf-8").read()
        check("a normal tracked repo still ingests", rc == 0 and "'tracked.md'" in tracked)
        check("and does not pick up the git-ignored file", "'notes/runbook.md'" not in tracked)

        plain = os.path.join(tmp, "plain")          # no git at all -> auto-detect still walks
        os.makedirs(plain)
        open(os.path.join(plain, "loose.md"), "w").write("# Loose\n\nno repo here\n")
        out3 = os.path.join(tmp, "plain.sql")
        rc, _ = ingest(plain, out3)
        check("a directory outside any repo still auto-walks",
              rc == 0 and "'loose.md'" in open(out3, encoding="utf-8").read())

        empty = os.path.join(tmp, "empty")          # tracked but no markdown -> refuse, no wipe
        os.makedirs(empty)
        git(empty, "init")
        git(empty, "config", "user.email", "t@t")
        git(empty, "config", "user.name", "t")
        open(os.path.join(empty, "code.py"), "w").write("x = 1\n")
        git(empty, "add", "-A")
        git(empty, "commit", "-m", "one")
        rc, err = ingest(empty, os.path.join(tmp, "empty.sql"))
        check("a repo with no markdown refuses too, rather than deleting the stored corpus",
              rc != 0 and "refusing to write an empty corpus" in err)

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
