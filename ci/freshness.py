#!/usr/bin/env python3
"""Map-freshness checks -- make Tier-0/1 decay LOUD instead of silent.

The deterministic map is only trustworthy while it tracks the tree. When a file moves and its
component's glob stops matching, the constraints silently stop resolving -- the map now lies more
confidently than search would. This surfaces that mechanically:

  1. MAP ORPHANS  -- component key_paths globs matching NO real file (a moved/renamed file quietly
     unhooked its constraints). The most important check; exit 1 if any (so CI fails).
  2. STALE DOCS   -- documents whose git_commit != repo HEAD (ingestion lagged behind the tree).
  3. CONSTRAINT RE-REVIEW -- constraints whose source_doc is gone (dangling): the norm may no
     longer match a source that changed or vanished.

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
      * `git ls-files` misses anything present but untracked -- CUSTOMS tracks 16 files under
        sourcecode/ while 96k sit on disk, so every `sourcecode/**` glob looked orphaned;
      * a walk alone misses nothing here, but the index is still worth unioning in for trees
        where files are tracked yet not materialised (sparse checkouts).
    Component key_paths point at CODE, so this must see the whole tree, not just markdown.
    """
    files = set()
    try:
        files.update(subprocess.check_output(["git", "-C", repo_dir, "ls-files"],
                                             stderr=subprocess.DEVNULL).decode().splitlines())
    except Exception:
        pass
    for root, dirs, fs in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in fs:
            files.add(os.path.relpath(os.path.join(root, fn), repo_dir).replace("\\", "/"))
    return [f for f in files if not (set(f.replace("\\", "/").split("/")) & _SKIP_DIRS)]


def main():
    if len(sys.argv) < 3:
        print(__doc__); return 2
    repo_dir, repo = sys.argv[1], sys.argv[2]
    commit, _ = list_md(repo_dir)          # HEAD commit for the stale-docs check
    files = list_tracked(repo_dir)         # whole tree for the orphan check
    from ingest._common import connect     # lazy: keep this module importable without psycopg2
    conn = connect()
    cur = conn.cursor()

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

    # 3. constraints with a dangling source_doc (source removed since it was authored)
    cur.execute("SELECT count(*) FROM constraints WHERE repo=%s AND source_doc_id IS NOT NULL "
                "AND source_doc_id NOT IN (SELECT id FROM documents)", (repo,))
    dangling = cur.fetchone()[0]

    print(f"-- freshness [{repo} @ {commit[:8]}] --")
    print(f"MAP ORPHANS ({len(orphans)}) -- key_paths matching no file:")
    for slug, g in orphans:
        print(f"   ! {slug}: '{g}'")
    print(f"STALE DOCS (git_commit != HEAD): {stale}")
    print(f"CONSTRAINTS w/ dangling source_doc (re-review): {dangling}")

    if "--mark" in sys.argv:
        cur.execute("UPDATE documents SET status='stale' WHERE repo=%s AND git_commit NOT IN (%s,'nogit')",
                    (repo, commit))
        conn.commit()
        print(f"marked {stale} docs status='stale'")

    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
