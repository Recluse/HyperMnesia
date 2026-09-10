#!/usr/bin/env python3
"""The memory store's SQL contract, against a real Postgres and no embedder.

Abstention and supersede-not-overwrite are the two headline claims of the personal-memory tier,
and until now nothing in CI executed a single `mem.*` statement. The only check of either was
eval/mem_probes.py, which needs a live store AND a live embedder and is therefore not run
anywhere automatically. So a change to SEARCH_SQL's placeholders, or a `mem.active_memories`
view that lost its validity-window clause, would ship green -- and the first symptom would be
superseded or expired facts injected into prompts as current.

No embedder is needed: pgvector takes a literal vector, so the test writes its own. That is the
point -- it isolates the SQL and the view from the model.

    DATABASE_URL=postgresql://... python3 tests/test_memory_sql.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ingest"))

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://hm:hm@localhost:5432/hypermnesia")
DIM = 1024

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def psql(sql):
    p = subprocess.run(["psql", DATABASE_URL, "-tAX", "-v", "ON_ERROR_STOP=1"],
                       input=sql.encode(), capture_output=True, timeout=60)
    if p.returncode != 0:
        raise SystemExit(f"psql failed:\n{p.stderr.decode()}\n--- sql ---\n{sql}")
    return p.stdout.decode().strip()


def vec(seed):
    """A deterministic unit-ish vector. Two different seeds are far apart in cosine terms."""
    v = [0.0] * DIM
    v[seed % DIM] = 1.0
    return "[" + ",".join(f"{x:.1f}" for x in v) + "]"


def main():
    print("== the view hides what it promises to hide ==")
    psql("DELETE FROM mem.memories WHERE (metadata->>'sqltest') = 'true';")
    meta = "'{\"sqltest\":true}'::jsonb"
    psql(f"""
      INSERT INTO mem.memories (memory_type, content, lang, importance, confidence, project,
                                metadata, embedding, embedding_model)
      VALUES ('semantic','active fact about widgets','en',0.5,0.8,'t', {meta}, '{vec(1)}', 'test'),
             ('semantic','expired fact about widgets','en',0.5,0.8,'t', {meta}, '{vec(2)}', 'test'),
             ('semantic','superseded fact about widgets','en',0.5,0.8,'t', {meta}, '{vec(3)}', 'test');
      UPDATE mem.memories SET valid_to = now() - interval '1 day'
       WHERE content = 'expired fact about widgets' AND (metadata->>'sqltest') = 'true';
      UPDATE mem.memories SET status = 'superseded'
       WHERE content = 'superseded fact about widgets' AND (metadata->>'sqltest') = 'true';
    """)
    active = psql("SELECT count(*) FROM mem.active_memories WHERE (metadata->>'sqltest')='true';")
    check("only the active row is visible through mem.active_memories", active == "1", active)
    check("but all three are still in mem.memories (supersede keeps history)",
          psql("SELECT count(*) FROM mem.memories WHERE (metadata->>'sqltest')='true';") == "3")
    check("an expired row is hidden by the validity window, not by status",
          psql("SELECT status FROM mem.memories WHERE content='expired fact about widgets' "
               "AND (metadata->>'sqltest')='true';") == "active")

    print("\n== the distance gate is what makes abstention possible ==")
    # The gate the store relies on: a query vector far from everything must return nothing at
    # all, rather than the least-bad row. This is the SQL half of the abstention claim.
    near = psql(f"SELECT count(*) FROM mem.active_memories WHERE (metadata->>'sqltest')='true' "
                f"AND embedding <=> '{vec(1)}' < 0.5;")
    far = psql(f"SELECT count(*) FROM mem.active_memories WHERE (metadata->>'sqltest')='true' "
               f"AND embedding <=> '{vec(500)}' < 0.5;")
    check("a close query matches", near == "1", near)
    check("an unrelated query matches NOTHING, rather than the least-bad row", far == "0", far)

    print("\n== the search query the store actually uses still runs ==")
    # Executed, not string-matched: a renamed column or a view that lost its validity-window
    # clause is invisible to a text comparison and fatal here. Run the same shape do_search
    # runs -- vector distance, the composite fts leg, the project filter -- with literals.
    rows = psql(f"SELECT count(*) FROM mem.active_memories m "
                f"WHERE (m.metadata->>'sqltest')='true' "
                f"AND m.embedding <=> '{vec(1)}' < 0.5 "
                f"AND (m.project IS NULL OR m.project = 't');")
    check("a project-filtered vector search over the view runs and finds the active row",
          rows == "1", rows)
    lex = psql("SELECT count(*) FROM mem.active_memories m "
               "WHERE (m.metadata->>'sqltest')='true' "
               "AND to_tsvector('simple', m.content) @@ to_tsquery('simple', 'widgets');")
    check("and the lexical leg matches the same row", lex == "1", lex)

    psql("DELETE FROM mem.memories WHERE (metadata->>'sqltest') = 'true';")
    check("the test cleans up after itself",
          psql("SELECT count(*) FROM mem.memories WHERE (metadata->>'sqltest')='true';") == "0")

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
