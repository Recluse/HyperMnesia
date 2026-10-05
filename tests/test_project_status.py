#!/usr/bin/env python3
"""Runnable source-freshness and actual MCP transport checks; no live DB needed."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci.project_status import collect


def main():
    with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
        root = Path(tmp)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        doc = root / "read me|док.md"
        doc.write_text("# Current\n")
        snapshot = {"documents": [{"path": doc.name,
                                   "hash": hashlib.sha256(doc.read_bytes()).hexdigest()}],
                    "chunks": 7, "components": 2}
        result = collect(root, "myrepo", snapshot)
        assert result["root"] == str(root.resolve())
        assert result["documents"]["indexed"] == 1 and result["chunks"]["indexed"] == 7
        assert result["freshness"]["state"] == "fresh", result
        doc.write_text("# Changed\n")
        (root / "new.md").write_text("# New\n")
        result = collect(root, "myrepo", snapshot)
        assert result["freshness"]["stale"] == 1
        assert result["freshness"]["unindexed"] == 1
        doc.unlink()
        assert collect(root, "myrepo", snapshot)["freshness"]["missing"] == 1
        assert collect(root / "absent", "myrepo", snapshot)["freshness"]["state"] == "unknown"
        error = collect(root, "myrepo", {"error": "down"})
        assert error["freshness"]["state"] == "error" and error["chunks"]["indexed"] is None
        assert collect(root, "bad scope", snapshot)["documents"]["indexed"] is None
        bad = {**snapshot, "documents": [{"path": "../outside.md", "hash": "x"}]}
        assert collect(root, "myrepo", bad)["freshness"]["state"] == "unknown"
        doc.write_text("# Current\n")
        doc.unlink()
        doc.symlink_to(ROOT / "README.md")
        assert collect(root, "myrepo", snapshot)["freshness"]["state"] == "unknown"
        doc.unlink()
        (root / "new.md").unlink()
        empty = {"documents": [], "chunks": 0, "components": 0}
        assert collect(root, "myrepo", empty)["freshness"]["state"] == "unknown"
        doc.write_text("# Current\n")
        # Ignore rules are the ingester's own, including the different private/public filename.
        from ingest.ingest_repo import IGNORE_FILE
        (root / IGNORE_FILE).write_text("ignored.md\n")
        (root / "ignored.md").write_text("# Excluded\n")
        assert collect(root, "myrepo", snapshot)["freshness"]["state"] == "fresh"

        public = IGNORE_FILE == ".hmignore"
        prefix = "HM" if public else "AGENTMEM"
        fake = root / ("psql" if public else "kubectl")
        fake.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                        "sql=sys.stdin.read()\n"
                        "assert \"WHERE repo='myrepo'\" in sql\n"
                        "assert \"WHERE d.repo='myrepo'\" in sql\n"
                        "assert 'BEGIN READ ONLY' in sql\n"
                        "print('BEGIN')\nprint(" + repr(json.dumps(snapshot)) + ")\nprint('COMMIT')\n")
        fake.chmod(0o755)
        binary = ROOT / "mcp-server/target/release" / (
            "hypermnesia-mcp" if public else "agentmem-mcp")
        env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
               prefix + "_REPO": "myrepo", prefix + "_ROOT": str(root),
               prefix + "_PROJECT_STATUS": str(ROOT / "ci/project_status.py"),
               prefix + "_PYTHON": sys.executable, "AGENTMEM_SSH": "local"}
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "project_status", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "project_status", "arguments": {"repo": "foreign"}}}]
        run = subprocess.run([str(binary)], input="".join(json.dumps(r) + "\n" for r in requests),
                             text=True, capture_output=True, env=env, cwd=root, timeout=20)
        assert run.returncode == 0, run.stderr
        responses = [json.loads(line)["result"] for line in run.stdout.splitlines()]
        tool = next(t for t in responses[0]["tools"] if t["name"] == "project_status")
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["outputSchema"]["properties"]["version"]["const"] == 1
        status = responses[1]
        assert status["isError"] is False, status
        assert status["structuredContent"] == json.loads(status["content"][0]["text"])
        assert status["structuredContent"]["freshness"]["state"] == "fresh", status
        assert responses[2]["isError"] is True
        # DB failure remains a structured error state, never fabricated zero counts.
        fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nsys.exit(1)\n")
        run = subprocess.run([str(binary)], input=json.dumps(requests[1]) + "\n",
                             text=True, capture_output=True, env=env, cwd=root, timeout=20)
        status = json.loads(run.stdout)["result"]
        assert status["isError"] is False
        assert status["structuredContent"]["freshness"]["state"] == "error"
        assert status["structuredContent"]["documents"]["indexed"] is None
    print("project_status source and MCP transport checks passed")


if __name__ == "__main__":
    main()
