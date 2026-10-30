#!/usr/bin/env python3
"""arch_invariants resolves edited paths against the REPO ROOT, not the session's cwd.

It used to strip the cwd. After `cd src/` in a session, `/repo/src/api/x.py` became `api/x.py`,
no glob matched, and every edit under src/ went out without its invariants -- silently, because
"no component" is not an error. The root is CLAUDE_PROJECT_DIR when the host sets it, else the
git top level of the cwd, else the cwd; the repo scope defaults to the root's folder name.

    python3 tests/test_arch_hook_root.py
"""
import _isolate  # noqa: F401  (first: a throwaway HOME and HYPERMNESIA_HOME)
import json
import os
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hooks", "arch_invariants.py")

GRAPH = {
    "components": [{"slug": "api", "name": "API", "repo": "myapp", "parent": None,
                    "responsibility": "http", "key_paths": ["src/api/**"], "priority": 0},
                   {"slug": "py", "name": "Python", "repo": "myapp", "parent": None,
                    "responsibility": "all python", "key_paths": ["**/*.py"], "priority": 0}],
    "relationships": [],
    "constraints": [{"kind": "invariant", "scope": "component", "repo": "myapp",
                     "component": "api", "component_repo": "myapp", "title": "API rule",
                     "statement": "Handlers stay thin.", "severity": "must", "source": None},
                    {"kind": "invariant", "scope": "component", "repo": "myapp",
                     "component": "py", "component_repo": "myapp", "title": "Python rule",
                     "statement": "Type hints everywhere.", "severity": "must", "source": None},
                    {"kind": "invariant", "scope": "global", "repo": "myapp",
                     "component": None, "component_repo": None, "title": "Global rule",
                     "statement": "Applies to myapp files.", "severity": "must", "source": None}],
}

failures, ran = [], 0


def check(name, cond, detail=""):
    global ran
    ran += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(event, env_extra, tmp):
    shim_dir = os.path.join(tmp, "bin")
    os.makedirs(shim_dir, exist_ok=True)
    shim = os.path.join(shim_dir, "psql")
    with open(shim, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\ncat <<'EOF_STUB'\n" + json.dumps(GRAPH) + "\nEOF_STUB\n")
    os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
    env = dict(os.environ, DATABASE_URL="postgresql://stub/stub", HOME=os.path.join(tmp, "home"),
               PATH=shim_dir + os.pathsep + os.environ.get("PATH", ""))
    for k in ("HM_REPO", "CLAUDE_PROJECT_DIR"):
        env.pop(k, None)
    env.update(env_extra)
    os.makedirs(env["HOME"], exist_ok=True)
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(event).encode(),
                       capture_output=True, timeout=60, env=env)
    return p.returncode, p.stdout.decode()


with tempfile.TemporaryDirectory() as tmp:
    repo = os.path.join(tmp, "myapp")
    os.makedirs(os.path.join(repo, "src", "api"))
    subprocess.run(["git", "init", "-q", repo], check=True)
    target = os.path.join(repo, "src", "api", "routes.py")

    print("== cwd is a subdirectory of a git repo ==")
    ev = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": os.path.join(repo, "src"),
          "tool_input": {"file_path": target}}
    rc, out = run(ev, {}, tmp)
    check("exits 0", rc == 0)
    check("the rule is injected from a subdirectory (git top level is the root)", "API rule" in out, out[:200])
    check("the scope defaults to the root's folder, not the cwd's", "nothing is mapped" not in out, out[:200])

    print("== a relative tool path is anchored at the cwd ==")
    ev_rel = dict(ev, tool_input={"file_path": "api/routes.py"})
    rc, out = run(ev_rel, {}, tmp)
    check("cwd-relative path still resolves", "API rule" in out, out[:200])

    print("== CLAUDE_PROJECT_DIR wins over the cwd ==")
    plain = os.path.join(tmp, "plain")             # not a git repo
    os.makedirs(os.path.join(plain, "sub"))
    ev_p = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": os.path.join(plain, "sub"),
            "tool_input": {"file_path": os.path.join(plain, "src", "api", "x.py")}}
    rc, out = run(ev_p, {"CLAUDE_PROJECT_DIR": plain, "HM_REPO": "myapp"}, tmp)
    check("rule injected relative to CLAUDE_PROJECT_DIR", "API rule" in out, out[:200])
    rc, out = run(ev_p, {"HM_REPO": "myapp"}, tmp)
    check("without it, a non-git cwd is the root (and src/ is outside it)", "API rule" not in out, out[:200])

    print("== containment: a file outside the root gets none of this repo's rules ==")
    other = os.path.join(tmp, "otherrepo")
    os.makedirs(os.path.join(other, "lib"))
    ev_o = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": other,
            "tool_input": {"file_path": os.path.join(other, "lib", "x.py")}}
    rc, out = run(ev_o, {"CLAUDE_PROJECT_DIR": repo, "HM_REPO": "myapp"}, tmp)
    check("`**/*.py` of the project repo does not claim another repo's file",
          "Python rule" not in out and "Global rule" not in out, out[:300])
    check("says the file is outside the root it resolved against, and which root",
          "is outside the repo root" in out and "CLAUDE_PROJECT_DIR" in out and repo in out, out[:300])
    ev_up = dict(ev, tool_input={"file_path": "../../otherrepo/lib/x.py"})
    rc, out = run(ev_up, {}, tmp)
    check("a relative path escaping upward is outside too", "is outside the repo root" in out
          and "Python rule" not in out, out[:300])
    ev_drive = dict(ev, tool_input={"file_path": "C:/work/myapp/src/api/x.py"})
    rc, out = run(ev_drive, {}, tmp)
    check("a drive-qualified path is absolute, not joined onto the cwd",
          "C:/work/myapp/src/api/x.py is outside" in out and "API rule" not in out, out[:300])

    print("== the unmapped-scope note is the shared text ==")
    rc, out = run(ev, {"HM_REPO": "ghost"}, tmp)
    check("names the scope", "(!) SCOPE: nothing is mapped under the scope 'ghost'" in out, out[:240])

print(f"\n{ran - len(failures)}/{ran} checks passed")
sys.exit(1 if failures else 0)
