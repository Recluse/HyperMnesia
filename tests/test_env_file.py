#!/usr/bin/env python3
"""The shared settings file: what it may do, and what it may not.

The pipeline's tunables are read from the environment by scripts launched in different ways --
from a scheduler with its own environment block, from the MCP client, by hand from a terminal.
A shared file makes "change a setting" a meaningful action instead of "changed it for some of
the runs".

But it is read by processes that start with no person present, so it has two limits, and both
are checked here rather than assumed:

  * only names from known prefixes are accepted. Otherwise the settings file is a way to set
    PATH or PYTHONPATH for a scheduled job;
  * a file writable by anyone but its owner is ignored ENTIRELY and loudly. A partly-applied
    file carrying someone else's values is worse than none.

And a third, not about safety: an explicit environment variable beats the file. Without that you
could not run one pass with a different threshold without editing the shared settings.

    python3 tests/test_env_file.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run(env_text, mode=0o600, extra_env=None):
    """Load the settings file in a fresh process; returns (environment as the hook sees it, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "hypermnesia.env")
        with open(path, "w", encoding="utf-8") as f:
            f.write(env_text)
        os.chmod(path, mode)
        env = dict(os.environ, HYPERMNESIA_ENV_FILE=path)
        for k in ("MEM_REFLECT_MIN", "MEM_STALE_DAYS", "PYTHONPATH"):
            env.pop(k, None)
        env.update(extra_env or {})
        code = ("import sys, os, json; sys.path.insert(0, %r); import _mem_common; "
                "print(json.dumps({k: os.environ.get(k) for k in "
                "['MEM_REFLECT_MIN','MEM_STALE_DAYS','PATH','PYTHONPATH']}))"
                % os.path.join(ROOT, "hooks"))
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env=env, timeout=60)
        import json
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
        return out, p.stderr


def main():
    print("== the file is applied ==")
    seen, _ = run("# комментарий\nMEM_REFLECT_MIN=9\n\nMEM_STALE_DAYS = 42\n")
    check("a value is read", seen.get("MEM_REFLECT_MIN") == "9", str(seen))
    check("spaces around the equals sign do not matter", seen.get("MEM_STALE_DAYS") == "42", str(seen))

    print("\n== an explicit variable beats the file ==")
    seen, _ = run("MEM_REFLECT_MIN=9\n", extra_env={"MEM_REFLECT_MIN": "3"})
    check("the environment wins", seen.get("MEM_REFLECT_MIN") == "3", str(seen))

    print("\n== foreign names are refused ==")
    # A settings file able to set PATH for a scheduled job is not a setting, it is a way to
    # swap out the executables it runs.
    before = os.environ.get("PATH")
    seen, _ = run("PATH=/evil\nPYTHONPATH=/evil\nMEM_REFLECT_MIN=9\n")
    check("PATH is not replaced", seen.get("PATH") != "/evil", str(seen.get("PATH")))
    check("PYTHONPATH is not replaced", seen.get("PYTHONPATH") != "/evil")
    check("and an allowed key from the same file did apply", seen.get("MEM_REFLECT_MIN") == "9")
    check("the parent process PATH is untouched", os.environ.get("PATH") == before)

    print("\n== a world-writable file is ignored entirely, and loudly ==")
    seen, err = run("MEM_REFLECT_MIN=9\n", mode=0o666)
    check("not one value was applied", seen.get("MEM_REFLECT_MIN") is None, str(seen))
    check("and it said so on stderr", "ignored entirely" in err, err[:160])

    print("\n== a missing file is not an error ==")
    env = dict(os.environ, HYPERMNESIA_ENV_FILE="/nope/hypermnesia.env")
    env.pop("MEM_REFLECT_MIN", None)
    p = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, %r); import _mem_common; print('ok')"
                        % os.path.join(ROOT, "hooks")],
                       capture_output=True, text=True, env=env, timeout=60)
    check("importing the hooks works with no settings file", p.returncode == 0 and "ok" in p.stdout,
          p.stderr[-200:])

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
