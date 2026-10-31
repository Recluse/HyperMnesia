#!/usr/bin/env python3
"""Compare one MCP-owned project root with a read-only database snapshot on stdin.

Uses the ingester's enumeration and raw-byte hashes; no SQL, embeddings or writes here.
"""
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest.ingest_repo import list_md, IGNORE_FILE, MAX_FILE_BYTES


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def collect(root, repo, snapshot):
    checked = now()
    root = Path(root).resolve()
    result = {"version": 1, "repo": repo, "root": str(root), "checked_at": checked,
              "documents": {"indexed": None}, "chunks": {"indexed": None},
              "freshness": {"state": "unknown", "checked_at": None, "stale": None,
                            "unindexed": None, "missing": None, "reason": None},
              "map": {"state": "unknown", "components": None, "checked_at": None}}
    fresh = result["freshness"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        fresh["reason"] = "Invalid or unavailable server repo scope"
        return result
    if "error" in snapshot:
        fresh.update(state="error", reason="Database snapshot unavailable")
        return result
    stored = snapshot["documents"]
    result["documents"]["indexed"] = len(stored)
    result["chunks"]["indexed"] = snapshot["chunks"]
    components = snapshot["components"]
    result["map"].update(state="available" if components else "missing",
                         components=components, checked_at=checked)
    if not root.is_dir():
        fresh["reason"] = "Project root unavailable"
        return result

    def path_for(rel):
        path = Path(rel)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Indexed path escapes project root")
        path = root / path
        if not path.resolve().is_relative_to(root):
            raise ValueError("Path escapes project root")
        return path

    try:
        # Same candidate list as normal ingest: tracked markdown in Git, walk elsewhere, one pass,
        # never entering what the ignore file excludes. os.walk would skip a directory it cannot
        # read without a word, and the ingester does exactly that, so such a directory is no gap
        # in the index -- but it must not pass for checked either: the reason counts them and names
        # the first. Any other failure to list a directory -- the root itself included: then
        # nothing about the inventory is known -- leaves the state unknown.
        unreadable = []

        def walk_error(exc):
            where = Path(exc.filename) if isinstance(exc.filename, str) else None
            if (not isinstance(exc, PermissionError) or where is None or where == root
                    or not where.is_relative_to(root)):
                raise exc
            unreadable.append(where.relative_to(root).as_posix())
        # Git discovery keeps its own short timeout: a hung git must not outlast the caller.
        in_git = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                                capture_output=True, timeout=5).returncode == 0
        _, enumerated = list_md(str(root), walk=not in_git, onerror=walk_error)
        indexed = {row["path"] for row in stored}
        stale = missing = unindexed = 0
        for row in stored:
            path = path_for(row["path"])
            try:
                before = path.stat()
            except FileNotFoundError:
                missing += 1
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("Indexed source is not a regular project file")
            if not row["hash"]:
                raise ValueError("Indexed content hash unavailable")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError("Source changed during check; retry")
            stale += digest.hexdigest() != row["hash"]
        for rel in enumerated:
            if rel in indexed:
                continue
            if (root / rel).is_symlink() or not (root / rel).resolve().is_relative_to(root):
                continue
            path = path_for(rel)
            # Deliberate ingester exclusions are not gaps.
            if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            with path.open("rb"):
                pass
            unindexed += 1
        fresh.update(state="stale" if stale or missing or unindexed else "fresh",
                     checked_at=now(), stale=stale, missing=missing, unindexed=unindexed)
        reasons = []
        if not stored and not unindexed:
            fresh["state"] = "unknown"
            reasons.append("No indexed or eligible source documents")
        if unreadable:
            reasons.append(f"{len(unreadable)} director{'y' if len(unreadable) == 1 else 'ies'} "
                           f"could not be read (first: {unreadable[0]}) and were not checked, as "
                           f"the ingester skips them too; {IGNORE_FILE} can exclude them")
        fresh["reason"] = "; ".join(reasons) or None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        fresh["reason"] = "Source files could not be checked safely or changed during check"
    return result


if __name__ == "__main__":
    print(json.dumps(collect(sys.argv[1], sys.argv[2], json.load(sys.stdin))))
