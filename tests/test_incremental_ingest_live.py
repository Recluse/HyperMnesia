#!/usr/bin/env python3
"""Execute ingest SQL in a throwaway PostgreSQL database; never use a live store.

DATABASE_URL must name a migrated test database. Each run uses a separate corpus
and removes it afterwards. No model or embedder is called.
"""
import _isolate  # noqa: F401 -- isolate user settings; DATABASE_URL stays explicit
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ["DATABASE_URL"]
TAG = "ingest-test-" + uuid.uuid4().hex
conn = psycopg2.connect(DSN)
conn.autocommit = True
cur = conn.cursor()


def snapshot():
    cur.execute("SELECT path, content_hash FROM documents WHERE repo=%s ORDER BY path", (TAG,))
    return cur.fetchall()


try:
    with tempfile.TemporaryDirectory(prefix="hm-ingest-live-") as tmp:
        root = Path(tmp)
        repo = root / "source"
        repo.mkdir()
        # Quoting and the dollar delimiter are part of the snapshot's data.
        names = ["keep.md", "change'quote$do$.md", "gone.md"]
        for name in names:
            (repo / name).write_text("# Test\n\n" + name + " original content\n")
        known = root / "known.tsv"
        output = root / "ingest.sql"

        def generate(incremental=False):
            args = [sys.executable, str(ROOT / "ingest/ingest_repo.py"),
                    str(repo), TAG, str(output), "--walk"]
            if incremental:
                args += ["--known-hashes", str(known)]
            subprocess.run(args, check=True, capture_output=True, text=True)
            return output.read_text()

        cur.execute(generate())
        original = snapshot()
        known.write_text("".join(f"{p}\t{h}\n" for p, h in original))
        cur.execute("SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id "
                    "WHERE d.repo=%s AND d.path='keep.md'", (TAG,))
        kept_id = cur.fetchone()[0]
        vector = "[" + ",".join(["1"] + ["0"] * 1023) + "]"
        cur.execute("UPDATE chunks SET embedding=%s::vector, embedding_model='test-only' WHERE id=%s",
                    (vector, kept_id))
        (repo / names[1]).write_text("# Updated\n\nA changed document.\n")
        (repo / "gone.md").unlink()
        (repo / "new.md").write_text("# New\n\nA new document.\n")
        incremental = generate(True)

        # A row count cannot distinguish either of these concurrent replacements.
        for mutation in ("hash", "path"):
            if mutation == "hash":
                cur.execute("UPDATE documents SET content_hash='concurrent' WHERE repo=%s AND path='keep.md'", (TAG,))
            else:
                cur.execute("UPDATE documents SET path='renamed.md' WHERE repo=%s AND path='keep.md'", (TAG,))
            before = snapshot()
            try:
                cur.execute(incremental)
            except psycopg2.errors.RaiseException as exc:
                assert "known-hashes no longer matches" in str(exc), exc
                cur.execute("ROLLBACK")
            else:
                raise AssertionError("stale snapshot was accepted: " + mutation)
            assert snapshot() == before, "stale SQL changed the concurrent writer's rows"
            if mutation == "hash":
                cur.execute("UPDATE documents SET content_hash=%s WHERE repo=%s AND path='keep.md'",
                            (dict(original)["keep.md"], TAG))
            else:
                cur.execute("UPDATE documents SET path='keep.md' WHERE repo=%s AND path='renamed.md'", (TAG,))
        print("same-count hash/path drift: rejected without partial changes")

        # Once the guard has passed, a normal uncooperative writer cannot invalidate it.
        guard_end = incremental.index("DELETE FROM documents")
        cur.execute(incremental[:guard_end])
        other = psycopg2.connect(DSN)
        try:
            with other.cursor() as contender:
                contender.execute("SET LOCAL lock_timeout='200ms'")
                try:
                    contender.execute("UPDATE documents SET content_hash='racing' WHERE repo=%s", (TAG,))
                except psycopg2.errors.LockNotAvailable:
                    other.rollback()
                else:
                    raise AssertionError("a writer bypassed the snapshot lock")
            cur.execute(incremental[guard_end:])
        finally:
            other.close()
        expected = [(p.name, hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in sorted(repo.glob("*.md"))]
        assert snapshot() == expected
        cur.execute("SELECT embedding_model, embedding::text FROM chunks WHERE id=%s", (kept_id,))
        assert cur.fetchone() == ("test-only", vector), "unchanged embeddings were lost"
        print("locked apply: exact final corpus, unchanged chunk and embedding preserved")
finally:
    cur.execute("ROLLBACK")
    cur.execute("DELETE FROM documents WHERE repo=%s", (TAG,))
    conn.close()
print("incremental ingest live: PASS")
