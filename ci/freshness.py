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
  3. CONSTRAINT SOURCES -- a constraint names the document it was written from by
     (source_repo, source_path, source_hash) (sql/migrate_constraint_sources.sql). Flagged: the
     source changed since the rule was written (its hash now differs), and the source is gone.
     `--accept-sources` records the current hash after you have re-read the rule.
  4. MAP SHAPE -- edges whose two endpoints are in different repos (an ERROR: Tier 1 never
     follows them, so whatever they were meant to propagate does not arrive); and, for
     information, components with no constraint, constraints that can never be delivered (no
     component, a component in another repo, or one with no key_paths and no edge to a mapped
     component), and tracked files inside a mapped directory that no component matches -- the
     file that moved out from under its directory's glob, which check 1 cannot see.

Generic: connects via DATABASE_URL (ingest/_common). Scope is one repo (the map is multi-repo).

Usage: ci/freshness.py <repo_dir> <repo> [--mark] [--accept-sources]
       (--mark sets documents.status='stale'; --accept-sources stamps changed constraint sources
        with their current hash)
Exit 1 if any map orphans or cross-repo edges.
"""
import fnmatch
import hashlib
import os
import posixpath
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingest.ingest_repo import list_md, read_ignore, _SKIP_DIRS, MAX_FILE_BYTES  # noqa: E402
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


def git_tracked(repo_dir):
    """The git index for this tree, or None when it is not a git checkout."""
    try:
        out = subprocess.check_output(["git", "-C", repo_dir, "ls-files", "-z"],
                                      stderr=subprocess.DEVNULL)
    except Exception:
        return None
    return sorted(f for f in out.decode("utf-8", "surrogateescape").split("\0") if f)


def mapped_dirs(globs):
    """Directories a set of key_paths claims: the literal directory part of each glob (before
    its first wildcard), or an exact path's parent. The repo root is never one -- a root-level
    exact path (`.gitlab-ci.yml`) does not claim the whole tree."""
    out = set()
    for g in globs:
        if not g:
            continue
        cut = min([i for i in (g.find("*"), g.find("?")) if i >= 0] or [len(g)])
        head = g[:cut] if cut < len(g) else posixpath.dirname(g)
        d = head.rsplit("/", 1)[0] if cut < len(g) else head
        d = d.strip("/")
        if d:
            out.add(d)
    return out


def file_hash(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def has_column(cur, table, column):
    cur.execute("SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) AND attname = %s "
                "AND NOT attisdropped", (table, column))
    return cur.fetchone() is not None


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
    orphans, blank_globs = [], []
    for slug, kps in cur.fetchall():
        for g in kps or []:
            # An empty or whitespace-only glob matches nothing and claims nothing; it is a map
            # error of its own (flagged below), never an orphan and never "has key_paths".
            if not (g or "").strip():
                blank_globs.append(slug)
                continue
            if not any(_glob_matches(g, f) for f in files):
                orphans.append((slug, g))

    # 1b. edges across repos -- an ERROR. GRAPH_SQL names each endpoint's repo and Tier 1
    #     follows only edges wholly inside the current repo, so such an edge propagates nothing,
    #     in either repo, while the map reads as if it did.
    cur.execute("""SELECT s.repo, s.slug, r.kind, d.repo, d.slug
                     FROM relationships r
                     JOIN components s ON s.id = r.src_component_id
                     JOIN components d ON d.id = r.dst_component_id
                    WHERE (s.repo = %s OR d.repo = %s) AND s.repo <> d.repo
                    ORDER BY 1, 2, 4, 5""", (repo, repo))
    cross_edges = cur.fetchall()

    # 1c. components with no active constraint -- informational: a grouping parent normally has
    #     none, but a leaf with none is a map entry that changes nothing.
    cur.execute("""SELECT c.slug, NOT EXISTS (SELECT 1 FROM unnest(c.key_paths) g
                                             WHERE btrim(g) <> '') FROM components c
                    WHERE c.repo = %s AND NOT EXISTS (
                          SELECT 1 FROM constraints x
                           WHERE x.component_id = c.id AND x.status = 'active')
                    ORDER BY c.slug""", (repo,))
    ruleless = cur.fetchall()

    # 1d. constraints no path can ever receive -- the static form of what get_constraints
    #     reports per call as UNDELIVERABLE, plus the case it cannot see from one call: a
    #     component with no key_paths and no edge to a component that has some.
    cur.execute("""SELECT x.title,
                          CASE WHEN x.scope <> 'component' THEN 'scope is ' || quote_literal(x.scope)
                               WHEN c.id IS NULL THEN 'no component'
                               WHEN c.repo <> x.repo THEN 'component is in repo ' || c.repo
                               ELSE 'component ' || c.slug || ' has no key_paths and no edge to a mapped component'
                          END
                     FROM constraints x LEFT JOIN components c ON c.id = x.component_id
                    WHERE x.repo = %s AND x.status = 'active' AND x.scope <> 'global'
                      AND (x.scope <> 'component' OR c.id IS NULL OR c.repo <> x.repo
                           OR (NOT EXISTS (SELECT 1 FROM unnest(c.key_paths) g
                                           WHERE btrim(g) <> '') AND NOT EXISTS (
                               SELECT 1 FROM relationships r
                                 JOIN components o ON o.id = CASE WHEN r.src_component_id = c.id
                                                                  THEN r.dst_component_id
                                                                  ELSE r.src_component_id END
                                WHERE (r.src_component_id = c.id OR r.dst_component_id = c.id)
                                  AND o.repo = c.repo
                                  AND EXISTS (SELECT 1 FROM unnest(o.key_paths) g
                                              WHERE btrim(g) <> ''))))
                    ORDER BY x.id""", (repo,))
    never = cur.fetchall()

    # 1e. tracked files inside a mapped directory that no component matches. Check 1 asks
    #     whether each glob still matches SOMETHING; a file that moved out from under its glob
    #     (src/api/x.py -> src/api/v2/x.py under an exact-path glob) leaves every glob matching
    #     and the file mapped to nothing. Informational: new files are legitimately unmapped
    #     until someone decides where they belong.
    cur.execute("SELECT key_paths FROM components WHERE repo=%s", (repo,))
    all_globs = [g for (kps,) in cur.fetchall() for g in (kps or []) if (g or "").strip()]
    dirs = mapped_dirs(all_globs)
    tracked = git_tracked(repo_dir)
    universe = tracked if tracked is not None else files
    unmapped = [f for f in universe
                if any(f.startswith(d + "/") for d in dirs)
                and not any(_glob_matches(g, f) for g in all_globs)]

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
    # ...and the same for a path the tree's own .hmignore excludes. That file is a
    # DECISION that something stays out of the corpus -- a credentials file, a superseded set of
    # documents. Listing it here under "re-run with --walk to include them" advertises the one
    # action that would undo the decision, on every single run.
    ignored = read_ignore(repo_dir)

    def is_ignored(rel):
        return any(rel.startswith(p) if p.endswith("/") else fnmatch.fnmatch(rel, p)
                   for p in ignored)

    seen_md = set(enumerated)
    beyond = [f for f in files
              if f.lower().endswith(".md") and f not in seen_md and f not in indexed
              and not (set(f.split("/")) & _SKIP_DIRS)
              and not is_ignored(f)
              and why_skipped(f) is None]

    # 3. constraint sources. The rule names its document by (repo, path, hash), which survives
    #    the delete-and-reinsert of a re-ingest; source_doc_id alone did not (ON DELETE SET
    #    NULL), so the edit this check exists to notice used to erase the evidence of itself.
    #    Same-repo sources are compared with the file ON DISK (the truth the rule must match);
    #    a source in another repo can only be compared with that repo's indexed copy.
    src_changed, src_gone, src_unhashed, src_unreadable, unnamed = [], [], [], [], 0
    sources_migrated = has_column(cur, "constraints", "source_hash")
    if sources_migrated:
        cur.execute("""SELECT id, title, source_repo, source_path, source_hash FROM constraints
                        WHERE repo=%s AND status='active' AND source_path IS NOT NULL
                        ORDER BY id""", (repo,))
        for cid, title, srepo, spath, shash in cur.fetchall():
            if srepo == repo:
                ap = os.path.join(repo_dir, spath)
                try:
                    now = file_hash(ap) if os.path.isfile(ap) else None
                except OSError as exc:
                    # Present but unreadable (EACCES, EIO, vanished mid-run): a finding, not a
                    # crash that takes every other finding down with it.
                    src_unreadable.append((cid, title, f"{srepo}:{spath}", exc.strerror or str(exc)))
                    continue
            else:
                cur.execute("SELECT content_hash FROM documents WHERE repo=%s AND path=%s",
                            (srepo, spath))
                row = cur.fetchone()
                now = row[0] if row else None
            where = f"{srepo}:{spath}"
            if now is None:
                src_gone.append((cid, title, where))
            elif shash is None:
                src_unhashed.append((cid, title, where))
            elif now != shash:
                src_changed.append((cid, title, where, now))
        cur.execute("SELECT count(*) FROM constraints WHERE repo=%s AND status='active' "
                    "AND source_path IS NULL", (repo,))
        unnamed = cur.fetchone()[0]
    else:
        cur.execute("SELECT count(*) FROM constraints WHERE repo=%s AND source_doc_id IS NULL",
                    (repo,))
        unnamed = cur.fetchone()[0]

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
    print(f"CROSS-REPO EDGES ({len(cross_edges)}) -- never followed by Tier 1 (error):")
    for sr, ss, k, dr, ds in cross_edges:
        print(f"   ! {sr}:{ss} -{k}-> {dr}:{ds}")
    if sources_migrated:
        print(f"CONSTRAINT SOURCE CHANGED since the rule was written: {len(src_changed)}")
        for cid, title, where, _ in src_changed:
            print(f"   ~ #{cid} {title!r} <- {where}  (re-read the rule; --accept-sources to stamp)")
        print(f"CONSTRAINT SOURCE GONE (the named document no longer exists): {len(src_gone)}")
        for cid, title, where in src_gone:
            print(f"   ! #{cid} {title!r} <- {where}")
        if src_unhashed:
            print(f"CONSTRAINT SOURCE NOT YET INGESTED (no hash recorded): {len(src_unhashed)}")
        if src_unreadable:
            print(f"CONSTRAINT SOURCE COULD NOT BE READ: {len(src_unreadable)}")
            for cid, title, where, why in src_unreadable:
                print(f"   ? #{cid} {title!r} <- {where}: {why}")
        print(f"CONSTRAINTS naming no source (informational; many have none by design): {unnamed}")
    else:
        print("CONSTRAINT SOURCES: not checked -- sql/migrate_constraint_sources.sql is not applied "
              "(run `hm migrate`)")
        print(f"CONSTRAINTS w/o source_doc (informational; many are NULL by design): {unnamed}")
    print(f"COMPONENTS WITH NO CONSTRAINT (informational): {len(ruleless)}"
          + (f"  e.g. {', '.join(sl + (' (grouping)' if grp else '') for sl, grp in ruleless[:10])}"
             if ruleless else ""))
    if blank_globs:
        print(f"BLANK GLOBS (empty key_paths entries, matching nothing): "
              f"{', '.join(sorted(set(blank_globs)))}")
    print(f"NEVER-DELIVERABLE CONSTRAINTS (informational): {len(never)}")
    for title, why in never[:10]:
        print(f"   ? {title!r}: {why}")
    print(f"UNMAPPED FILES inside mapped directories (informational): {len(unmapped)}")
    for f in unmapped[:10]:
        print(f"   - {f}")
    if len(unmapped) > 10:
        print(f"   ... and {len(unmapped) - 10} more")

    if "--mark" in sys.argv:
        # Marks exactly the documents the check above found, by path. The old predicate was the
        # old test -- `git_commit NOT IN (HEAD,'nogit')` -- so `--mark` flagged every document
        # ingested before the last commit, most of them identical to the file on disk, and
        # flagged nothing at all in a repo without git.
        cur.execute("UPDATE documents SET status='stale' WHERE repo=%s AND path = ANY(%s)",
                    (repo, stale_paths))
        conn.commit()
        print(f"marked {cur.rowcount} docs status='stale'")

    if "--accept-sources" in sys.argv and src_changed:
        # Only the ones reported as changed above, each to the hash it was just compared with:
        # stamping is a statement that a person re-read those rules, not a blanket reset.
        for cid, _, _, now in src_changed:
            cur.execute("UPDATE constraints SET source_hash=%s WHERE id=%s", (now, cid))
        conn.commit()
        print(f"stamped {len(src_changed)} constraint source(s) with their current hash")

    return 1 if orphans or cross_edges else 0


if __name__ == "__main__":
    sys.exit(main())
