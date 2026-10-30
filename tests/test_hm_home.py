#!/usr/bin/env python3
"""HYPERMNESIA_HOME in the hooks: where the settings file and the state are read from.

The rule (hooks/_mem_common.py): state lives in <home>/state once that directory exists, and in
~/.claude until then -- never a mix, and importing a hook never moves or creates anything. The
settings file is HYPERMNESIA_ENV_FILE, else <home>/hypermnesia.env, else the ~/.claude copy while
that is the only one. The MCP server (cargo test) and the console (cargo test) pin the same rule.

    python3 tests/test_hm_home.py
"""
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = str(ROOT / "hooks")
PROBE = """
import json, sys
sys.path.insert(0, %r)
import _mem_common as m
print(json.dumps({"home": m.HOME_DIR, "env": m.ENV_FILE, "queue": m.state_path("mem-queue.jsonl"),
                  "down": m.STORE_DOWN, "cache": m.profile_cache_path()}))
""" % HOOKS


def env_for(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HYPERMNESIA_HOME", "HYPERMNESIA_ENV_FILE", "MEM_AUTHOR")}
    env.update(HOME=str(home), USERPROFILE=str(home), **extra)
    return env


def py(code, home, **extra):
    r = subprocess.run([sys.executable, "-c", code], env=env_for(home, **extra),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


def probe(home, **extra):
    return json.loads(py(PROBE, home, **extra))


tmp = Path(tempfile.mkdtemp(prefix="hm-home-"))
home = tmp / "h"
legacy = home / ".claude"
legacy.mkdir(parents=True)
hm = home / ".HyperMnesia"

# Nothing installed: everything stays where an older install had it.
(legacy / "mem-queue.jsonl").write_text("q\n")
(legacy / "hypermnesia.env").write_text("MEM_AUTHOR=alice\n")
(legacy / "hypermnesia.env").chmod(0o600)
p = probe(home)
assert p["home"] == str(hm), p
assert p["env"] == str(legacy / "hypermnesia.env"), "only the legacy settings file exists"
for k in ("queue", "down", "cache"):
    assert p[k].startswith(str(legacy) + os.sep), (k, p)
assert (legacy / "mem-queue.jsonl").exists() and not hm.exists(), "importing a hook moved or created something"

# The home exists but state/ does not yet (an install halfway): still the legacy state -- and a
# fault marker raised now must not create state/, which would switch every hook to an empty one.
hm.mkdir(mode=0o700)
assert probe(home)["queue"] == str(legacy / "mem-queue.jsonl")
out = py("import sys; sys.path.insert(0, %r); import _mem_common as m; print(m.flag_flip('store-down', True))"
         % HOOKS, home)
assert out == "True", out
assert (legacy / "hypermnesia-store-down").exists() and not (hm / "state").exists()

# Installed: state/ exists, so every state path is there; the home's settings file wins.
(hm / "state").mkdir(mode=0o700)
(hm / "hypermnesia.env").write_text("MEM_AUTHOR=alice\n")
(hm / "hypermnesia.env").chmod(0o600)
p = probe(home)
assert p["env"] == str(hm / "hypermnesia.env"), p
for k in ("queue", "down", "cache"):
    assert p[k].startswith(str(hm / "state") + os.sep), (k, p)

# HYPERMNESIA_HOME moves it all; HYPERMNESIA_ENV_FILE still names the file outright; empty
# values of either count as unset.
other = tmp / "elsewhere"
(other / "state").mkdir(parents=True)
p = probe(home, HYPERMNESIA_HOME=str(other))
assert p["home"] == str(other) and p["queue"] == str(other / "state" / "mem-queue.jsonl"), p
assert p["env"] == str(legacy / "hypermnesia.env"), "no file in that home yet: the legacy copy"
p = probe(home, HYPERMNESIA_HOME=str(other), HYPERMNESIA_ENV_FILE="~/x.env")
assert p["env"] == str(home / "x.env"), p
p = probe(home, HYPERMNESIA_HOME="", HYPERMNESIA_ENV_FILE="")
assert p["home"] == str(hm) and p["env"] == str(hm / "hypermnesia.env"), p
p = probe(home, HYPERMNESIA_HOME="~/rel/../hm2")
assert p["home"] == str(home / "hm2"), "expanded and folded, as abspath(expanduser()) does"

# The loader reads the moved file, through the link the installer leaves at the old name too.
who = "import sys; sys.path.insert(0, %r); import _mem_common as m; print(m.identity())" % HOOKS
(hm / "hypermnesia.env").write_text("MEM_AUTHOR=bob\n")
assert py(who, home) == "bob"
if os.name != "nt":
    (legacy / "hypermnesia.env").unlink()
    legacy.chmod(0o700)              # the loader checks the directory the name is in, link or not
    os.symlink("../.HyperMnesia/hypermnesia.env", legacy / "hypermnesia.env")
    assert py(who, home, HYPERMNESIA_HOME=str(other)) == "bob", "the old name, followed"

# The extractor and an old-layout writer: the ~/.claude queue is read as a second source and never
# copied; it is retired only when quiet for a day and fully done, its done list folded in first.
sw = tmp / "sw"
(sw / ".claude").mkdir(parents=True)
st = sw / ".HyperMnesia" / "state"
st.mkdir(parents=True)
(st / "mem-queue.done").write_text("k1\n")
old_q, old_d = sw / ".claude" / "mem-queue.jsonl", sw / ".claude" / "mem-queue.done"
old_q.write_text('{"transcript_path": "/t/a.jsonl"}\n')
old_d.write_text("k0\n")
code = """
import json, os, sys, time
sys.path.insert(0, %r)
import mem_extract as e
q, d = e.legacy_twin("mem-queue.jsonl"), e.legacy_twin("mem-queue.done")
key = e.done_key(json.loads(open(q).read())["transcript_path"])
e.retire_legacy_queue(q, d, settle=0)            # young: kept
young = os.path.exists(q)
old = time.time() - 2 * 86400
os.utime(q, (old, old)); os.utime(d, (old, old))
e.retire_legacy_queue(q, d, settle=0)            # quiet but not done: kept
pending = os.path.exists(q)
with open(e.DONE, "a") as f: f.write(key + "\\n")
e.retire_legacy_queue(q, d, settle=0)            # quiet and done: retired
print(json.dumps([young, pending, os.path.exists(q), os.path.exists(d),
                  sorted(open(e.DONE).read().split())]))
""" % HOOKS
young, pending, q_left, d_left, done = json.loads(py(code, sw, MEM_AUTHOR="t"))
assert young and pending and not q_left and not d_left, (young, pending, q_left, d_left)
assert "k0" in done and "k1" in done, "the old done list was folded in before it was removed"
# A writer that had the old queue open when it was retired: its line lands in the renamed file
# during the settle, and is carried into the new queue instead of being deleted with it.
late = """
import json, os, sys, threading, time
sys.path.insert(0, %r)
import mem_extract as e
q = os.path.join(e.LEGACY_DIR, "mem-queue.jsonl")
open(q, "w").write(json.dumps({"transcript_path": "/t/a.jsonl"}) + "\\n")
with open(e.DONE, "a") as f: f.write(e.done_key("/t/a.jsonl") + "\\n")
old = time.time() - 2 * 86400
os.utime(q, (old, old))
fd = open(q, "a")                                 # opened before the retirement renames it
def write_late():
    time.sleep(0.3)
    fd.write(json.dumps({"transcript_path": "/t/late.jsonl"}) + "\\n"); fd.close()
threading.Thread(target=write_late).start()
e.retire_legacy_queue(e.legacy_twin("mem-queue.jsonl"), None, settle=1.0)
print(json.dumps([os.path.exists(q), os.path.exists(q + ".retiring"),
                  open(e.QUEUE).read() if os.path.exists(e.QUEUE) else ""]))
""" % HOOKS
lw = tmp / "lw"
(lw / ".claude").mkdir(parents=True)
(lw / ".HyperMnesia" / "state").mkdir(parents=True)
gone, retiring, newq = json.loads(py(late, lw, MEM_AUTHOR="t"))
assert not gone and not retiring, (gone, retiring)
assert "/t/late.jsonl" in newq and "/t/a.jsonl" not in newq, newq
# A .retiring file a dead run left is finished by the next one.
(lw / ".claude" / "mem-queue.jsonl.retiring").write_text('{"transcript_path": "/t/b.jsonl"}\n')
finish = ("import sys; sys.path.insert(0, %r); import mem_extract as e; "
          "e.retire_legacy_queue(None, None, settle=0); print(open(e.QUEUE).read().count('/t/b.jsonl'))" % HOOKS)
assert py(finish, lw, MEM_AUTHOR="t") == "1"
assert not (lw / ".claude" / "mem-queue.jsonl.retiring").exists()

# Before any install there is no twin: the ~/.claude file IS the queue. A link is never one.
bare = tmp / "bare"
(bare / ".claude").mkdir(parents=True)
(bare / ".claude" / "mem-queue.jsonl").write_text("x\n")
twin = "import sys; sys.path.insert(0, %r); import _mem_common as m; print(m.legacy_twin('mem-queue.jsonl'))" % HOOKS
assert py(twin, bare) == "None"
(bare / ".HyperMnesia" / "state").mkdir(parents=True)
assert py(twin, bare) == str(bare / ".claude" / "mem-queue.jsonl")
if os.name != "nt":
    (bare / ".claude" / "mem-queue.jsonl").unlink()
    os.symlink(bare / "elsewhere", bare / ".claude" / "mem-queue.jsonl")
    (bare / "elsewhere").write_text("x\n")
    assert py(twin, bare) == "None"

# No state file is named outside the rule: every ~/.claude path in the hooks and the doctor goes
# through state_path() (or is the rule's own fallback).
for f in sorted((ROOT / "hooks").glob("*.py")) + [ROOT / "ci" / "doctor.py"]:
    for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0]
        if re.search(r"""expanduser\(\s*f?["']~/\.claude/""", code):
            raise AssertionError(f"{f.name}:{n}: a ~/.claude path outside state_path(): {line.strip()}")

shutil.rmtree(tmp, ignore_errors=True)
print("test_hm_home: ok")
