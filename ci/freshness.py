#!/usr/bin/env python3
"""Map-freshness checks -- make Tier-0/1 decay LOUD instead of silent.

The deterministic map is only trustworthy while it tracks the tree. When a file moves and its
component's glob stops matching, the constraints silently stop resolving -- the map now lies more
confidently than search would. This surfaces that mechanically:

  1. MAP ORPHANS  -- component key_paths globs matching NO real file (a moved/renamed file quietly
     unhooked its constraints). The most important check; exit 1 if any (so CI fails).
  2. STALE DOCS   -- documents whose git_commit != repo HEAD (ingestion lagged behind the tree).
  3. CONSTRAINT RE-REVIEW -- constraints whose source_doc link is NULL: the document they were
     authored from was deleted (a full re-ingest does exactly this, since the FK is
     ON DELETE SET NULL). Re-apply the repo's seed to restore the links.

Generic: connects via DATABASE_URL (ingest/_common). Scope is one repo (the map is multi-repo).

Usage: ci/freshness.py <repo_dir> <repo> [--mark]   (--mark sets documents.status='stale')
Exit 1 if any map orphans.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingest.ingest_repo import list_md, _SKIP_DIRS  # noqa: E402
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
    commit, _ = list_md(repo_dir)          # HEAD commit for the stale-docs check
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

    # 2. stale docs (ingested at a commit other than HEAD; 'nogit' repos are exempt)
    cur.execute("SELECT count(*) FROM documents WHERE repo=%s AND git_commit NOT IN (%s,'nogit')",
                (repo, commit))
    stale = cur.fetchone()[0]

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
    print(f"STALE DOCS (git_commit != HEAD): {stale}")
    print(f"CONSTRAINTS w/o source_doc (informational; many are NULL by design): {dangling}")

    if "--mark" in sys.argv:
        cur.execute("UPDATE documents SET status='stale' WHERE repo=%s AND git_commit NOT IN (%s,'nogit')",
                    (repo, commit))
        conn.commit()
        print(f"marked {stale} docs status='stale'")

    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
