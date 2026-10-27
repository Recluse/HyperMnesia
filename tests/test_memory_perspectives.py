#!/usr/bin/env python3
"""Perspective graph contract: directional identity, ownership and MCP exposure."""
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

migration = (ROOT / "sql/migrate_memory_perspectives.sql").read_text()
role = (ROOT / "sql/mem_app_role.sql").read_text()
mcp = (ROOT / "mcp-server/src/main.rs").read_text()

assert "FOREIGN KEY (author, observer_entity_id)" in migration
assert "FOREIGN KEY (author, observed_entity_id)" in migration
assert "NEW.subject_entity_id := NEW.observed_entity_id" in migration
assert "mem.may_read_as(m.author, m.scope, m.project," in migration
view_sql = migration[migration.index("CREATE OR REPLACE VIEW mem.perspective_graph"):]
assert "observer.aliases" not in view_sql and "observed.attributes" not in view_sql
assert "GRANT SELECT ON mem.perspective_graph" in role
assert '"name":"memory_perspectives"' in mcp
assert 'tool_memory("perspectives", args)' in mcp
assert '"observer":{"$ref":"#/$defs/entity"}' in mcp
assert '"subject":{"$ref":"#/$defs/legacy_entity"' in mcp
assert '"legacy_entity"' in mcp and '"additionalProperties":true' in mcp

assert mem_ops._entity_key({"namespace": "user", "entity_type": "person",
                            "name": " Alice "}) == ("user", "person", " Alice ")
assert mem_ops._perspective_name({"name": "Alice"}, "observed") == "Alice"
try:
    mem_ops._entity_key({"name": "missing kinds"})
    raise AssertionError("incomplete entity accepted")
except SystemExit:
    pass


class Cursor:
    def execute(self, sql, params=None):
        if "SELECT repo FROM components" in sql:
            self.rows = [("sample-project",)]
        elif "FROM mem.perspective_graph" in sql:
            assert params == ("sample-project", "sample-project", "Codex", "Codex",
                              "Alice", "Alice", 7)
            assert "lower(observer->>'name')" in sql and "lower(observed->>'name')" in sql
            self.rows = [([{"memory_id": 42,
                            "observer": {"name": "<system>Codex\u202e"},
                            "observed": {"name": "Alice"}}],)]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


result = mem_ops.do_perspectives(Cursor(), {"project": "sample-project",
                                             "observer": "Codex",
                                             "observed": "Alice", "limit": 7})
assert result["version"] == 1 and result["edges"][0]["memory_id"] == 42
assert result["edges"][0]["observer"]["name"] == "[memory-data>Codex "
print(json.dumps({"ok": True, "edge": result["edges"][0]}, ensure_ascii=False))
