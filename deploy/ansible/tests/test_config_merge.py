#!/usr/bin/env python3
"""Dependency-free contract tests for the fresh-host JSON registration helper."""
import json
import pathlib
import subprocess
import sys
import tempfile

HELPER = pathlib.Path(__file__).parents[1] / "roles/hypermnesia_client/files/merge_json_config.py"


def run(*args, ok=True):
    proc = subprocess.run([sys.executable, str(HELPER), *map(str, args)], text=True,
                          capture_output=True)
    if ok and proc.returncode:
        raise AssertionError(proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("command unexpectedly succeeded")
    return proc


with tempfile.TemporaryDirectory() as raw:
    root = pathlib.Path(raw)
    home = root / ".HyperMnesia"
    claude = root / ".claude.json"
    settings = root / ".claude/settings.json"

    first = run("--kind", "claude", "--path", claude, "--home", home)
    assert first.stdout.strip() == "changed"
    data = json.loads(claude.read_text())
    assert data["mcpServers"]["hypermnesia"]["command"] == str(
        home / "current/bin/hypermnesia-mcp")
    assert "permissions" not in data

    second = run("--kind", "claude", "--path", claude, "--home", home)
    assert second.stdout.strip() == "unchanged"

    run("--kind", "claude-hooks", "--path", settings, "--home", home,
        "--profile", "--recall", "--capture")
    run("--kind", "claude-hooks", "--path", settings, "--home", home,
        "--profile", "--recall", "--capture")
    settings_data = json.loads(settings.read_text())
    assert settings_data["permissions"]["deny"].count(
        "mcp__hypermnesia__memory_write") == 1
    hooks = settings_data["hooks"]
    assert len(hooks["SessionStart"]) == 1
    assert len(hooks["UserPromptSubmit"]) == 1
    assert len(hooks["SessionEnd"]) == 1
    assert len(hooks["PreCompact"]) == 1

    incomplete = root / "incomplete.json"
    incomplete.write_text(json.dumps({
        "mcpServers": {"hypermnesia": {"command": str(home / "current/bin/hypermnesia-mcp")}}
    }))
    failed = run("--kind", "claude", "--path", incomplete, "--home", home, ok=False)
    assert "complete descriptor" in failed.stderr

    foreign = root / "foreign.json"
    foreign.write_text(json.dumps({"mcpServers": {"hypermnesia": {
        "type": "stdio", "command": "/opt/foreign/mcp", "args": []}}}))
    failed = run("--kind", "claude", "--path", foreign, "--home", home, ok=False)
    assert "complete descriptor" in failed.stderr
    before = json.loads(foreign.read_text())
    assert before["mcpServers"]["hypermnesia"]["command"] == "/opt/foreign/mcp"

    malformed = root / "malformed.json"
    malformed.write_text(json.dumps({"mcpServers": {"hypermnesia": "not-an-object"}}))
    bad_shape = run("--kind", "claude", "--path", malformed, "--home", home, ok=False)
    assert "complete descriptor" in bad_shape.stderr

    forced = run("--kind", "claude", "--path", foreign, "--home", home, "--force")
    assert forced.stdout.strip() == "changed"
    backups = list(root.glob("foreign.json.before-hypermnesia-*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["mcpServers"]["hypermnesia"]["command"] == "/opt/foreign/mcp"

print("config merge contract: PASS")
