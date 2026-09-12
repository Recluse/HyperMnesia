#!/usr/bin/env python3
"""The shared settings file: what it may do, and what it may not.

The pipeline's tunables are read from the environment by scripts launched in different ways --
from a scheduler with its own environment block, from the MCP client, by hand from a terminal.
A shared file makes "change a setting" a meaningful action instead of "changed it for some of
the runs".

But it is read by processes that start with no person present, so it has two limits, and both
are checked here rather than assumed:

  * only an exact list of names is accepted. A prefix list was not a boundary -- HM_PYTHON is
    argv[0] of every mem_ops call and HM_LLM_CMD is executed outright, and both carry the
    allowed HM_ prefix;
  * a file that is not the owner's own private file, in the owner's own private directory, is
    ignored ENTIRELY and loudly. A partly-applied file carrying someone else's values is worse
    than none.

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


def run(env_text, mode=0o600, extra_env=None, dir_mode=0o700):
    """Load the settings file in a fresh process; returns (environment as the hook sees it, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "hypermnesia.env")
        with open(path, "w", encoding="utf-8") as f:
            f.write(env_text)
        os.chmod(path, mode)
        os.chmod(tmp, dir_mode)
        env = dict(os.environ, HYPERMNESIA_ENV_FILE=path)
        # PATH is popped too, and that is the point of popping it: the loader skips any name
        # already in the environment, so a PATH check run with PATH set passes whatever the
        # allowlist does -- it was measuring the wrong rule.
        for k in ("MEM_REFLECT_MIN", "MEM_STALE_DAYS", "PYTHONPATH", "PATH", "HM_PYTHON"):
            env.pop(k, None)
        env.update(extra_env or {})
        code = ("import sys, os, json; sys.path.insert(0, %r); import _mem_common; "
                "print(json.dumps({k: os.environ.get(k) for k in "
                "['MEM_REFLECT_MIN','MEM_STALE_DAYS','PATH','PYTHONPATH','HM_PYTHON']}))"
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

    print("\n== an empty variable counts as unset, here and in the console ==")
    # The console's settings screen skips an empty variable and shows the file's value. The
    # loader used to see the name as present and hand the hook "" -- the two disagreeing about
    # which value is in force, which is the one thing this console exists to prevent.
    seen, _ = run("MEM_REFLECT_MIN=9\n", extra_env={"MEM_REFLECT_MIN": ""})
    check("the file value applies", seen.get("MEM_REFLECT_MIN") == "9", str(seen))

    print("\n== foreign names are refused ==")
    # A settings file able to choose the interpreter is not a setting file, it is a way to swap
    # out the executables a scheduled job runs.
    before = os.environ.get("PATH")
    seen, err = run("PATH=/evil\nPYTHONPATH=/evil\nHM_PYTHON=/tmp/evil-python\n"
                    "MEM_REFLECT_MIN=9\n")
    check("PATH is not replaced", seen.get("PATH") != "/evil", str(seen.get("PATH")))
    check("PYTHONPATH is not replaced", seen.get("PYTHONPATH") != "/evil")
    # The one that mattered: HM_PYTHON passed the old prefix allowlist and became argv[0] of
    # every mem_ops call the scheduled hooks make.
    check("HM_PYTHON is not replaced", seen.get("HM_PYTHON") != "/tmp/evil-python",
          str(seen.get("HM_PYTHON")))
    check("and it said which names it ignored", "ignored" in err, err[:200])
    check("and an allowed key from the same file did apply", seen.get("MEM_REFLECT_MIN") == "9")
    check("the parent process PATH is untouched", os.environ.get("PATH") == before)

    print("\n== a world-writable file is ignored entirely, and loudly ==")
    seen, err = run("MEM_REFLECT_MIN=9\n", mode=0o666)
    check("not one value was applied", seen.get("MEM_REFLECT_MIN") is None, str(seen))
    check("and it said so on stderr", "ignored entirely" in err, err[:160])

    print("\n== a file in a directory others can write is ignored too ==")
    # The file's own mode is not the whole story: anyone who can write the directory can replace
    # the file with their own mode-600 one.
    seen, err = run("MEM_REFLECT_MIN=9\n", dir_mode=0o777)
    check("not one value was applied", seen.get("MEM_REFLECT_MIN") is None, str(seen))
    check("and the reason names the directory", "writable by others" in err, err[:200])

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
