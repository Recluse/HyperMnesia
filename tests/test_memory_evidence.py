#!/usr/bin/env python3
"""Evidence DAG contract: visible endpoints, acyclicity and MCP exposure."""
import _isolate  # noqa: F401  (first: a throwaway HOME and HYPERMNESIA_HOME)
import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("psycopg2", types.SimpleNamespace())
spec = importlib.util.spec_from_file_location("mem_ops", ROOT / "ingest/mem_ops.py")
mem_ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mem_ops)

migration = (ROOT / "sql/migrate_memory_evidence.sql").read_text()
mcp = (ROOT / "mcp-server/src/main.rs").read_text()
assert "WITH RECURSIVE reachable" in migration
assert "LOCK TABLE mem.derivations IN SHARE ROW EXCLUSIVE MODE" in migration
assert "LANGUAGE plpgsql SECURITY DEFINER" in migration
assert "SET search_path=pg_catalog,mem" in migration
assert "REVOKE ALL ON FUNCTION mem.guard_derivation() FROM PUBLIC" in migration
assert migration.count("mem.may_read(m.author,m.scope,m.project)") >= 2
insert_policy = migration[migration.index("CREATE POLICY derivations_insert"):]
assert "m.id=evidence_memory_id" in insert_policy
assert "m.id=derived_memory_id" in insert_policy
assert "FOREIGN KEY (author, derived_memory_id)" in migration
assert '"name":"memory_evidence"' in mcp
assert 'tool_memory("evidence", args)' in mcp
assert '"evidence":{"type":"array"' in mcp


class Cursor:
    def execute(self, sql, params=None):
        assert "FROM mem.derivations" in sql
        assert params == (17, 17, 9)
        self.row = ([{"evidence_memory_id": 4, "derived_memory_id": 17,
                      "relation": "supports"}],)

    def fetchone(self):
        return self.row


result = mem_ops.do_evidence(Cursor(), {"memory_id": 17, "direction": "both", "limit": 9})
assert result["version"] == 1 and result["edges"][0]["derived_memory_id"] == 17
assert mem_ops._DERIVATION_RELATIONS == {
    "supports", "inferred_from", "summarizes", "supersedes", "contradicts"}
print(json.dumps({"ok": True, "edge": result["edges"][0]}))
