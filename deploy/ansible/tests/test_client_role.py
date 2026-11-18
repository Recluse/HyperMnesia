#!/usr/bin/env python3
"""Run the actual client role twice in a disposable HOME, without services or a model.

The MCP fixture records requests so this also verifies that the copied target-side
smoke executes, including when every automatic hook is disabled (the default).
Requires ansible-playbook on PATH. It never uses the user's agent configuration.
"""
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ansible = shutil.which("ansible-playbook")
assert ansible, "ansible-playbook is required"

with tempfile.TemporaryDirectory(prefix="hm-role-") as raw:
    tmp = Path(raw)
    home = tmp / "target-home"
    home.mkdir()
    hm = home / ".HyperMnesia"
    binary = hm / "current/bin/hypermnesia-mcp"
    binary.parent.mkdir(parents=True)
    requests = tmp / "requests.jsonl"
    binary.write_text("#!" + sys.executable + "\n" + '''import json, sys
from pathlib import Path
for line in sys.stdin:
    req = json.loads(line)
    with Path(REQUEST_LOG).open("a") as out:
        out.write(json.dumps(req) + "\\n")
    if req["method"] == "initialize":
        result = {"serverInfo": {"name": "hypermnesia"}}
    elif req["method"] == "tools/list":
        result = {"tools": [{"name": x} for x in ("project_status", "get_project_map", "memory_search", "memory_get")]}
    else:
        assert req["params"] == {"name": "project_status", "arguments": {}}
        result = {"isError": False}
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
'''.replace("REQUEST_LOG", repr(str(requests))))
    binary.chmod(0o700)
    claude = home / ".claude.json"
    claude.write_text(json.dumps({"mcpServers": {"other": {"command": "/unchanged"}}, "theme": "dark"}))
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}, "theme": "light"}))
    variables = {
        "ansible_env": {"HOME": str(home)},
        "ansible_system": "Darwin",  # registration only; never touch host systemd
        "ansible_user_id": getpass.getuser(), "ansible_user_uid": os.getuid(),
        "ansible_python_interpreter": sys.executable,
        "hypermnesia_home": str(hm), "hypermnesia_clients": ["claude"],
        "hypermnesia_memory_author": "test-user", "hypermnesia_python": sys.executable,
        "hypermnesia_smoke_enabled": True, "hypermnesia_smoke_repo": "test-repo",
        "hypermnesia_smoke_root": str(tmp),
    }
    play = tmp / "play.json"
    play.write_text(json.dumps([{"hosts": "localhost", "gather_facts": False,
                                "vars": variables, "roles": ["hypermnesia_client"]}]))
    env = dict(os.environ, HOME=str(tmp / "controller-home"),
               ANSIBLE_ROLES_PATH=str(ROOT / "roles"), ANSIBLE_CONFIG=str(tmp / "ansible.cfg"),
               ANSIBLE_LOCAL_TEMP=str(tmp / "ansible-local"),
               ANSIBLE_REMOTE_TEMP=str(tmp / "ansible-remote"), ANSIBLE_NOCOLOR="1")
    (tmp / "ansible.cfg").write_text("[defaults]\n")
    Path(env["HOME"]).mkdir()
    for run in range(2):
        proc = subprocess.run([ansible, "-i", "localhost,", "-c", "local", str(play)],
                              env=env, text=True, capture_output=True, timeout=90)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        if run == 1:
            assert re.search(r"localhost\s+:.*changed=0\s+.*failed=0", proc.stdout), proc.stdout
        data = json.loads(claude.read_text())
        assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "/unchanged"}
        data = json.loads(settings.read_text())
        assert data["theme"] == "light" and not data["hooks"]
        assert set(data["permissions"]["deny"]) == {
            "Bash(rm:*)", "mcp__hypermnesia__memory_write", "mcp__hypermnesia__memory_supersede"}
    copied = hm / "state/ansible-mcp-smoke.py"
    assert copied.read_bytes() == (ROOT / "roles/hypermnesia_client/files/smoke.py").read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o700
    calls = [json.loads(line) for line in requests.read_text().splitlines()]
    assert [c["method"] for c in calls] == ["initialize", "tools/list", "tools/call"] * 2
print("client role: default write denies, target smoke, unrelated config preservation, second apply changed=0: PASS")
