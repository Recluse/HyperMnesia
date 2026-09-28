#!/usr/bin/env python3
"""Map-freshness checks -- make Tier-0/1 decay LOUD instead of silent.

The deterministic map is only trustworthy while it tracks the tree. When a file moves and its
component's glob stops matching, the constraints silently stop resolving -- the map now lies more
confidently than search would. This surfaces that mechanically:

  1. MAP ORPHANS  -- component key_paths globs matching NO real file (a moved/renamed file quietly
     unhooked its constraints). The most important check; exit 1 if any (so CI fails).
  2. STALE DOCS   -- documents whose INDEXED content differs from the file on disk, plus any
     indexed document that is gone from disk, plus markdown on disk that was NEVER indexed
     (that last one walks the DISK: the other two cannot see a file the corpus has no row for).
  3. CONSTRAINT RE-REVIEW -- constraints whose source_doc link is NULL: the document they were
     authored from was deleted (a full re-ingest does exactly this, since the FK is
     ON DELETE SET NULL). Re-apply the repo's seed to restore the links.

Generic: connects via DATABASE_URL (ingest/_common). Scope is one repo (the map is multi-repo).

Usage: ci/freshness.py <repo_dir> <repo> [--mark]   (--mark sets documents.status='stale')
Exit 1 if any map orphans.
"""
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingest.ingest_repo import list_md, _SKIP_DIRS, MAX_FILE_BYTES  # noqa: E402
from hooks._arch import _glob_matches         # noqa: E402


def list_tracked(repo_dir):
    """Every file that actually exists in the tree: the UNION of the git index and a filtered
    walk of the working directory.

    Neither source alone is enough, and using only one produces false orphans:
      * `git ls-files` misses anything present but untracked -- a repo that tracks a handful of
        files under a vendored tree while tens of thousands sit on disk makes every glob over
        that tree look orphaned;
      * a walk alone misses nothing here, but the index is still worth unioning in for trees
        where files are tracked yet not materialised (sparse checkouts).
    Component key_paths point at CODE, so this must see the whole tree, not just markdown.
    """
    tracked = set()
    try:
        tracked.update(subprocess.check_output(["git", "-C", repo_dir, "ls-files"],
                                               stderr=subprocess.DEVNULL).decode().splitlines())
    except Exception:
        pass
    walked = set()
    for root, dirs, fs in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in fs:
            walked.add(os.path.relpath(os.path.join(root, fn), repo_dir).replace("\\", "/"))
    # The skip list belongs to the INGESTER, where it means "don't index vendored/generated
    # markdown". Here the question is "does this file exist", and a tracked file exists no matter
    # what its directory is called -- `build/`, `bin/`, `dist/` and `out/` are perfectly ordinary
    # source directories in some projects. Applying the list to tracked paths invented three
    # orphan globs for a real project (build/macos/**, src/bin/**) whose files are right
    # there in git. So: filter only what the WALK found, never what git reports.
    walked = {f for f in walked if not (set(f.split("/")) & _SKIP_DIRS)}
    return sorted(tracked | walked)


def main():
    if len(sys.argv) < 3:
        print(__doc__); return 2
    repo_dir, repo = sys.argv[1], sys.argv[2]
    commit, enumerated = list_md(repo_dir)  # exactly what the ingester would take for this repo
    files = list_tracked(repo_dir)         # whole tree for the orphan check
    from ingest._common import connect     # lazy: keep this module importable without psycopg2
    conn = connect()
    cur = conn.cursor()

    # 0. does this scope exist at all? An empty component set makes every check below report
    #    zero, so a repo whose tag is one character off -- a directory named `Infra` ingested as
    #    `infra`, a renamed folder, a typo in the CI invocation -- passes forever with "MAP
    #    ORPHANS (0)". That is the exact state arch_invariants.py treats as the commonest and
    #    best-hidden misconfiguration, and the checker whose stated job is making decay LOUD had
    #    no equivalent. Name the scopes that do exist, because the fix is nearly always one.
    cur.execute("SELECT count(*) FROM components WHERE repo=%s", (repo,))
    if cur.fetchone()[0] == 0:
        cur.execute("SELECT repo, count(*) FROM components GROUP BY repo ORDER BY repo")
        known = ", ".join(f"{r} ({n})" for r, n in cur.fetchall()) or "(none)"
        print(f"-- freshness [{repo} @ {commit[:8]}] --")
        print(f"NO MAP: nothing is mapped under the scope '{repo}', so every check below would "
              f"report zero regardless of the map's real state.")
        print(f"        ingested scopes: {known}")
        conn.close()
        return 1

    # 1. map orphans -- scope to THIS repo (files is only this repo's tree, so other repos'
    #    components would always "not match" and get falsely flagged under multi-repo).
    cur.execute("SELECT slug, key_paths FROM components WHERE repo=%s AND key_paths <> '{}'", (repo,))
    orphans = []
    for slug, kps in cur.fetchall():
        for g in kps or []:
            if g and not any(_glob_matches(g, f) for f in files):
                orphans.append((slug, g))

    # 2. stale docs -- the INDEXED content against the file as it is now.
    #
    #    NOT `git_commit != HEAD`, which is what this asked before. The ingester deliberately
    #    SKIPS a document whose content_hash is unchanged, leaving its git_commit at the commit
    #    it was first stored at, so that test flagged every document in a repo that had moved on
    #    -- hundreds of them byte-identical to the file on disk. It measured repository activity,
    #    not index lag. And it exempted 'nogit', so a walked tree -- a notes folder outside git,
    #    the case `hm ingest --walk` exists for -- got no staleness check at all and reported a
    #    confident 0 forever.
    cur.execute("SELECT path, content_hash FROM documents WHERE repo=%s", (repo,))
    stored = cur.fetchall()
    stale_paths, gone = [], 0
    for path, h in stored:
        try:
            with open(os.path.join(repo_dir, path), "rb") as fh:
                if h and hashlib.sha256(fh.read()).hexdigest() != h:
                    stale_paths.append(path)
        except OSError:
            gone += 1                          # indexed but no longer on disk
    stale = len(stale_paths)

    # 2b. NEVER INDEXED -- markdown on disk with no document row at all.
    #
    #     The check above walks the INDEX and asks whether each stored document still matches its
    #     file, so a file that was never ingested is invisible to it: it reports a number that
    #     reads as "this much is behind" while whole directories are missing from the corpus and
    #     search answers about them with silence rather than with an error.
    #
    #     Measured against list_md -- the ingester's OWN enumeration -- and not the whole tree.
    #     In a git repo that means tracked files, so markdown that is git-ignored or sits in a
    #     nested checkout is not a gap the ingest left, and listing it would nag forever about
    #     files that ingest is never going to take. It is still worth knowing, so it gets its own
    #     line: those need `--walk`, which is a decision about what belongs in the corpus.
    #
    #     Over-MAX_FILE_BYTES files are EXCLUDED from both: the ingester refuses them by design
    #     and says so, and counting them here would be a permanent false alarm.
    #
    #     Deliberately does NOT affect the exit code. A commit that adds a document is supposed
    #     to have it unindexed until the ingest runs, so gating on this would fail every such
    #     build. It is loud in the output, which is what it needs to be.
    indexed = {p for p, _ in stored}

    def why_skipped(rel):
        """What would stop the ingester taking this file -- or None if nothing would.

        Mirrors the ingester's own refusals. Kept in this shape rather than as a boolean so the
        third case, "could not look at it", stays distinguishable from the two deliberate ones:
        folding a permission error into "excluded on purpose" erases a real gap from the report.
        """
        ap = os.path.join(repo_dir, rel)
        if os.path.islink(ap):
            return "symlink"                   # not followed out of the checkout, by design
        try:
            if os.path.getsize(ap) > MAX_FILE_BYTES:
                return "oversize"
        except OSError as exc:
            return f"unreadable ({exc.strerror})"
        return None

    unindexed, unreadable = [], []
    for f in enumerated:
        if f in indexed:
            continue
        why = why_skipped(f)
        if why is None:
            unindexed.append(f)
        elif why.startswith("unreadable"):
            unreadable.append(f"{f}: {why}")

    # `files` is the union of the git index and a filtered walk, and only the WALK half has the
    # skip list applied -- so a TRACKED `vendor/README.md` survives into it while list_md drops it
    # at both ends, `--walk` included. Without the same filter here it would sit under OUTSIDE THE
    # ENUMERATION forever, under advice ("re-run with --walk") that cannot move it.
    seen_md = set(enumerated)
    beyond = [f for f in files
              if f.lower().endswith(".md") and f not in seen_md and f not in indexed
              and not (set(f.split("/")) & _SKIP_DIRS)
              and why_skipped(f) is None]

    # 3. constraints with no source document. NOT "dangling pointer": the FK is
    #    ON DELETE SET NULL (sql/schema.sql), so a deleted document can never leave a pointer to
    #    a missing row -- that test asks for a state the schema makes unreachable and therefore
    #    reports 0 forever, which reads as health.
    #    Read this number as INFORMATIONAL. From the database alone, a link a re-ingest severed
    #    is indistinguishable from a norm whose seed row deliberately carries no source, and in
    #    practice most are the latter. The checkable question is whether every document path a
    #    seed references actually exists in the index: a seed pointing at a file the corpus
    #    cannot hold (a .yml, say, in a markdown-only corpus) silently resolves to NULL.
    cur.execute("SELECT count(*) FROM constraints WHERE repo=%s AND source_doc_id IS NULL", (repo,))
    dangling = cur.fetchone()[0]

    print(f"-- freshness [{repo} @ {commit[:8]}] --")
    print(f"MAP ORPHANS ({len(orphans)}) -- key_paths matching no file:")
    for slug, g in orphans:
        print(f"   ! {slug}: '{g}'")
    print(f"STALE DOCS (indexed content differs from disk): {stale}"
          + (f"  [+{gone} indexed but gone from disk]" if gone else ""))
    print(f"NEVER INDEXED (*.md the ingester enumerates, with no document row): {len(unindexed)}")
    for f in unindexed[:15]:
        print(f"   + {f}")
    if len(unindexed) > 15:
        print(f"   ... and {len(unindexed) - 15} more")
    if beyond:
        print(f"OUTSIDE THE ENUMERATION: {len(beyond)} *.md on disk that this scope's ingest "
              f"never sees (git-ignored / nested checkout). Re-run with --walk to include "
              f"them, e.g. {beyond[0]}")
    if unreadable:
        print(f"COULD NOT CHECK: {len(unreadable)} enumerated file(s) -- neither indexed nor "
              f"confirmed excluded")
        for u in unreadable[:5]:
            print(f"   ? {u}")
    print(f"CONSTRAINTS w/o source_doc (informational; many are NULL by design): {dangling}")

    if "--mark" in sys.argv:
        # Marks exactly the documents the check above found, by path. The old predicate was the
        # old test -- `git_commit NOT IN (HEAD,'nogit')` -- so `--mark` flagged every document
        # ingested before the last commit, most of them identical to the file on disk, and
        # flagged nothing at all in a repo without git.
        cur.execute("UPDATE documents SET status='stale' WHERE repo=%s AND path = ANY(%s)",
                    (repo, stale_paths))
        conn.commit()
        print(f"marked {cur.rowcount} docs status='stale'")

    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
