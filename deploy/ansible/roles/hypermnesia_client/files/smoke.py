#!/usr/bin/env python3
"""One read-only MCP call; print only the verdict, never memory or map content."""
import json, os, subprocess, sys

requests = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "project_status", "arguments": {}}},
]
run = subprocess.run([sys.argv[1]], input="".join(json.dumps(x) + "\n" for x in requests),
                     text=True, capture_output=True, timeout=150, env=os.environ)
if run.returncode:
    raise SystemExit(run.stderr or f"MCP exited {run.returncode}")
by_id = {x["id"]: x for x in map(json.loads, run.stdout.splitlines()) if "id" in x}
expected_name = sys.argv[2] if len(sys.argv) > 2 else "hypermnesia"
assert by_id[1]["result"]["serverInfo"]["name"] == expected_name
tools = {x["name"] for x in by_id[2]["result"]["tools"]}
assert {"project_status", "get_project_map", "memory_search", "memory_get"} <= tools
assert by_id[3]["result"].get("isError") is False
print(f"HyperMnesia MCP smoke: PASS ({expected_name}; initialize, catalog, project_status)")
