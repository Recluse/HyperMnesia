#!/usr/bin/env python3
"""Incremental-ingest contract tests.

The whole value of `--known-hashes` is a negative: rows that should NOT be touched. Nothing
observable fails if the incremental path quietly degrades into a full replace — the corpus
ends up correct, the SQL applies cleanly, and the only symptom is that every embedding was
recomputed, which looks like "embedding is just slow". So the emitted SQL is pinned here
directly: the blanket DELETE must be absent, unchanged documents must not be re-emitted, and
changed/vanished ones must be deleted by path.

The `UPDATE ... SET git_commit` check guards a cross-file interaction rather than this file:
ci/freshness.py reports documents whose git_commit != HEAD as stale, so an incremental run
that left untouched rows at their old commit would report the entire corpus as stale after
any commit — the fix for one report breaking another.

No database and no embedder: the ingester writes SQL, and SQL is what we assert on.

    python3 tests/test_incremental_ingest.py
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


def ingest(repo_dir, out, known=None):
    cmd = [sys.executable, INGEST, repo_dir, "testrepo", out]
    if known:
        cmd += ["--known-hashes", known]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return open(out, encoding="utf-8").read(), res.stderr


def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        git(repo, "init")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        for name, body in (("keep.md", "# Keep\n\nuntouched\n"),
                           ("edit.md", "# Edit\n\nbefore\n"),
                           ("drop.md", "# Drop\n\ndoomed\n")):
            open(os.path.join(repo, name), "w").write(body)
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "one")

        full, _ = ingest(repo, os.path.join(tmp, "full.sql"))

        print("full ingest (no --known-hashes) is unchanged behaviour:")
        check("clears the whole repo", "DELETE FROM documents WHERE repo = 'testrepo';" in full)
        check("emits every document", all(f"'{n}'" in full for n in ("keep.md", "edit.md", "drop.md")))
        check("does not touch git_commit separately", "UPDATE documents SET git_commit" not in full)
        check("emits no snapshot guard", "DO $do$" not in full)

        # What the DB would now hold. Real callers get this from psql; the hashes are just
        # sha256 of the file bytes, so the test can compute them the same way.
        import hashlib
        known_path = os.path.join(tmp, "known.tsv")
        with open(known_path, "w") as fh:
            for name in ("keep.md", "edit.md", "drop.md"):
                h = hashlib.sha256(open(os.path.join(repo, name), "rb").read()).hexdigest()
                fh.write(f"{name}\t{h}\n")

        open(os.path.join(repo, "edit.md"), "w").write("# Edit\n\nafter\n")
        os.remove(os.path.join(repo, "drop.md"))
        open(os.path.join(repo, "new.md"), "w").write("# New\n\nfresh\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "two")
        head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

        inc, report = ingest(repo, os.path.join(tmp, "inc.sql"), known_path)

        print("incremental ingest (--known-hashes):")
        check("never clears the whole repo",
              "DELETE FROM documents WHERE repo = 'testrepo';" not in inc)
        check("leaves the unchanged document alone", "'keep.md'" not in inc)
        check("re-emits the changed document", "'edit.md'" in inc and "after" in inc)
        check("emits the new document", "'new.md'" in inc)
        check("deletes the changed row before reinserting it",
              "DELETE FROM documents WHERE repo = 'testrepo' AND path IN (" in inc
              and "'edit.md'" in inc.split("AND path IN (")[1].split(");")[0])
        check("deletes the vanished document",
              "'drop.md'" in inc.split("AND path IN (")[1].split(");")[0])
        check("does not reinsert the vanished document",
              inc.count("'drop.md'") == 1)
        check("refreshes git_commit so freshness.py stays honest",
              f"UPDATE documents SET git_commit = '{head}' WHERE repo = 'testrepo';" in inc)
        check("reports what it kept", "1 unchanged kept" in report and "1 removed" in report)
        # A snapshot that does not describe the DB fails SILENTLY without this: a path listed
        # with its current hash is skipped as "already stored", so a row the DB never had is
        # never inserted and the corpus just answers "no results". The guard turns that into
        # an aborted transaction.
        check("aborts on a snapshot that does not match the DB",
              "DO $do$" in inc and "IF n <> 3 THEN RAISE EXCEPTION" in inc
              and "SELECT count(*) INTO n FROM documents WHERE repo = 'testrepo';" in inc)

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
