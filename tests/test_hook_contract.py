#!/usr/bin/env python3
"""Hook I/O contract tests.

Every hook is fail-open by design: on any error it prints nothing and exits 0. That is right
for reliability and terrible for detection -- if the host ever changes the hook contract
(field names, output shape, permission semantics), the hooks would not error, they would
simply stop injecting and the whole feature would go dark with no signal. Nothing else in CI
would notice, because syntax still compiles and every other test avoids the hooks.

So these tests pin the contract from the outside: feed a canonical event on stdin and assert
the exact shape of what comes back. They stub the store with a fake `psql` on PATH, so no
database or embedder is needed.

    python3 tests/test_hook_contract.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, "hooks")

GRAPH = {
    "components": [
        {"slug": "api", "name": "API", "repo": "myapp", "parent": None,
         "responsibility": "http layer", "key_paths": ["src/api/**", ".gitlab-ci.yml"], "priority": 0},
        {"slug": "db", "name": "DB", "repo": "myapp", "parent": None,
         "responsibility": "data layer", "key_paths": ["src/db/**"], "priority": 0},
    ],
    "relationships": [{"src": "api", "dst": "db", "kind": "depends_on"}],
    "constraints": [
        {"kind": "rule", "scope": "global", "repo": "myapp", "component": None,
         "title": "No raw SQL outside the data layer",
         "statement": "Only db may import the driver.", "severity": "must", "source": None},
        {"kind": "rule", "scope": "component", "repo": "myapp", "component": "api",
         "title": "Envelope", "statement": "Handlers return the envelope.",
         "severity": "should", "source": None},
    ],
}

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run_hook(script, event, extra_env=None, fake_psql_out=None):
    """Run a hook with a canonical event on stdin; optionally stub `psql`."""
    env = dict(os.environ, HM_REPO="myapp", DATABASE_URL="postgresql://stub/stub")
    env.pop("MEM_STALE_DAYS", None)
    env.update(extra_env or {})
    with tempfile.TemporaryDirectory() as tmp:
        if fake_psql_out is not None:
            shim = os.path.join(tmp, "psql")
            with open(shim, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\ncat <<'EOF_STUB'\n" + fake_psql_out + "\nEOF_STUB\n")
            os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            env["PATH"] = tmp + os.pathsep + env.get("PATH", "")
        p = subprocess.run([sys.executable, os.path.join(HOOKS, script)],
                           input=json.dumps(event).encode(), capture_output=True, timeout=60, env=env)
    return p


def main():
    print("== PreToolUse: arch_invariants ==")
    ev = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": "/w/myapp",
          "tool_input": {"file_path": "/w/myapp/src/api/routes.py"}}
    p = run_hook("arch_invariants.py", ev, fake_psql_out=json.dumps(GRAPH))
    check("exits 0", p.returncode == 0, p.stderr.decode()[-200:])
    out = p.stdout.decode().strip()
    check("emits output for a mapped file", bool(out), "hook stayed silent")
    obj = {}
    if out:
        try:
            obj = json.loads(out)
        except ValueError as e:
            check("stdout is JSON", False, str(e))
    hso = obj.get("hookSpecificOutput", {})
    check("hookSpecificOutput.hookEventName == PreToolUse", hso.get("hookEventName") == "PreToolUse")
    check("carries a non-empty additionalContext", bool(hso.get("additionalContext")))
    # The hook must express NO opinion on permission. Emitting "allow" here would auto-approve
    # every edit that happens to have a `must`, silently bypassing the normal approval flow.
    check("does NOT decide permission", "permissionDecision" not in hso,
          f"found permissionDecision={hso.get('permissionDecision')!r}")
    check("injects the applicable must", "No raw SQL outside the data layer" in hso.get("additionalContext", ""))
    check("omits should-severity rules", "Envelope" not in hso.get("additionalContext", ""))

    print("== PreToolUse: MultiEdit carries the path at the top level ==")
    ev_multi = {"hook_event_name": "PreToolUse", "tool_name": "MultiEdit", "cwd": "/w/myapp",
                "tool_input": {"file_path": "/w/myapp/src/api/routes.py",
                               "edits": [{"old_string": "a", "new_string": "b"}]}}
    p = run_hook("arch_invariants.py", ev_multi, fake_psql_out=json.dumps(GRAPH))
    check("MultiEdit still injects", bool(p.stdout.decode().strip()),
          "edits[] carries no file_path; reading it there silently skips MultiEdit")

    print("== PreToolUse: dot-prefixed paths still resolve ==")
    ev_dot = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": "/w/myapp",
              "tool_input": {"file_path": "/w/myapp/.gitlab-ci.yml"}}
    p = run_hook("arch_invariants.py", ev_dot, fake_psql_out=json.dumps(GRAPH))
    check("dot-file resolves to its component", "No raw SQL" in p.stdout.decode(),
          "lstrip('./') would eat the leading dot and match nothing")

    print("== PreToolUse: unrelated tools are ignored ==")
    p = run_hook("arch_invariants.py",
                 {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "/w/myapp",
                  "tool_input": {"command": "ls"}}, fake_psql_out=json.dumps(GRAPH))
    check("silent for a non-edit tool", p.stdout.decode().strip() == "" and p.returncode == 0)

    print("== fail-open: a broken store must never block a tool call ==")
    p = run_hook("arch_invariants.py", ev)  # no psql stub at all
    check("exits 0 with no store", p.returncode == 0)
    check("stays silent with no store", p.stdout.decode().strip() == "")

    print("== SessionStart: mem_profile ==")
    rows = "#PREF# 1|owner prefers one command at a time\n#REVQ# 2|2026-08-29\n#STALE# 0"
    with tempfile.TemporaryDirectory() as tmp:
        p = run_hook("mem_profile.py", {"hook_event_name": "SessionStart", "source": "startup"},
                     extra_env={"HOME": tmp}, fake_psql_out=rows)
    out = p.stdout.decode()
    check("exits 0", p.returncode == 0, p.stderr.decode()[-200:])
    check("emits a nonce-fenced block", "<personal-memory-profile nonce=" in out
          and "</personal-memory-profile nonce=" in out)
    check("marks the content as data, not instructions", "not instructions" in out or "не инструкции" in out)
    check("includes the memory", "one command at a time" in out)
    check("surfaces the pending review queue", "2" in out and "mem_review.py" in out)

    print(f"\n{6 + 11 - len(failures)}/{6 + 11} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
