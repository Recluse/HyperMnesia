#!/usr/bin/env python3
"""Static contract for entity authorship and the live migration."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ops = (ROOT / "ingest" / "mem_ops.py").read_text()
schema = (ROOT / "sql" / "schema_mem.sql").read_text()
role = (ROOT / "sql" / "mem_app_role.sql").read_text()
migration = (ROOT / "sql" / "migrate_entity_ownership.sql").read_text()

assert "def ensure_entity(cur, subj, author)" in ops
assert "ON CONFLICT (author, namespace, entity_type, canonical_name)" in ops
assert ops.index("author = require_author") < ops.index("ensure_entity(cur, observer_spec")
assert "author          TEXT NOT NULL" in schema
assert "UNIQUE (author, namespace, entity_type, canonical_name)" in schema
for text in (role, migration):
    assert "ALTER TABLE mem.entities ENABLE ROW LEVEL SECURITY" in text
# mem_app_role.sql re-states the entity policy last, through mem.reader(): the raw setting is the
# caller's to choose, so on a per-author login it must never be what a policy compares to.
assert "author = current_setting('mem.reader', true)" in migration
assert "author = (SELECT mem.reader())" in role
assert "current_setting('mem.reader'" not in role.split("-- Any policy on a memory table")[0]
assert "entity_author_map" in migration
assert "ALTER COLUMN author SET NOT NULL" in migration
assert "FROM pg_index i" in migration
assert "CREATE UNIQUE INDEX IF NOT EXISTS mem_entities_author_identity_idx" not in migration
assert "mem.touch_memory_access" in role
assert "SECURITY DEFINER" in role
assert "mem.may_read_as(m.author,m.scope,m.project," in role
review_policy = role[role.index("CREATE POLICY review_own"):role.index("-- mem.sources")]
assert "author = (SELECT mem.reader())" in review_policy
assert "IS NOT DISTINCT FROM" not in review_policy
assert "SELECT mem.touch_memory_access(%s)" in ops

print("entity ownership checks passed")
