#!/usr/bin/env python3
"""ci/install.py: release layout, the move out of ~/.claude, the switch, pruning and wiring.

Runs the installer against a throwaway HOME, with a stand-in MCP binary so no Rust toolchain is
needed. Every path the installer touches is under that HOME. The installer archives commits of
the repository it sits in, so it runs from a sandbox repository built from this working tree --
two commits, whatever the history (or the shallow clone) of the checkout under test.

    python3 tests/test_install.py
"""
import fnmatch, importlib.util, json, os, re, shutil, stat, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
BIN = "hypermnesia-mcp.exe" if WINDOWS else "hypermnesia-mcp"

tmp = Path(tempfile.mkdtemp(prefix="hm-install-"))


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c",
                           "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


# -- the sandbox repository -------------------------------------------------------------------
repo = tmp / "src" / "checkout"
files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
                       check=True, capture_output=True).stdout.decode().split("\0")
for f in filter(None, files):
    src = ROOT / f
    if src.is_file():
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, repo / f)
git(repo, "init", "-q")
git(repo, "add", "-A")
git(repo, "commit", "-qm", "one")
with open(repo / "README.md", "a", encoding="utf-8") as fh:
    fh.write("\nsecond commit\n")
git(repo, "commit", "-qam", "two")
with open(repo / "README.md", "a", encoding="utf-8") as fh:
    fh.write("\nthird commit\n")
git(repo, "commit", "-qam", "three")
INSTALL = repo / "ci" / "install.py"
head = git(repo, "rev-parse", "--short=12", "HEAD")
prev = git(repo, "rev-parse", "--short=12", "HEAD~1")
third = git(repo, "rev-parse", "--short=12", "HEAD~2")


def run(home, *args, ok=True, script=INSTALL, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("HYPERMNESIA_HOME", "HYPERMNESIA_ENV_FILE")}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, str(script), *args], env=env, capture_output=True,
                       text=True)
    if ok:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


spec = importlib.util.spec_from_file_location("hm_install", INSTALL)
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)

# -- every state file the hooks name is moved, and no lock is --------------------------------
names = set()
for py in (ROOT / "hooks").glob("*.py"):
    src = py.read_text(encoding="utf-8")
    names |= set(re.findall(r'state_path\(f?"([^"]+)"\)', src))
    names |= {"hypermnesia-" + n for n in re.findall(r'(?:flag_flip|_warn_once)\(\s*"([^"]+)"', src)}
    names |= {"hypermnesia-" + n + "*" for n in re.findall(r'(?:flag_flip|_warn_once)\(\s*"([^"]+)"\s*\+', src)}
names.discard("hypermnesia-{name}")
assert len(names) >= 8, names
for n in sorted(names):
    pattern = re.sub(r"\{[^}]*\}", "x", n)
    is_lock = any(fnmatch.fnmatch(pattern, g) for g in inst.LOCK_GLOBS)
    is_state = any(fnmatch.fnmatch(pattern, g) for g in inst.STATE_GLOBS)
    assert is_lock != is_state, f"{n}: lock={is_lock} state={is_state} -- add it to one list"
    assert is_lock == n.endswith(".lock"), n

# -- a machine from before HOME_DIR: settings and state in ~/.claude, configs into a checkout --
home = tmp / "home"
legacy = home / ".claude"
legacy.mkdir(parents=True)
hm = home / ".HyperMnesia"
fake = tmp / "fake-mcp"
fake.write_text("#!/bin/sh\necho fake\n")
fake.chmod(0o755)
(legacy / "hypermnesia.env").write_text("MEM_AUTHOR=alice\n")
(legacy / "hypermnesia.env").chmod(0o600)
(legacy / "mem-queue.jsonl").write_text('{"transcript_path": "/x"}\n')
(legacy / "mem-queue.done").write_text("/y\n")
(legacy / "hypermnesia-profile-cache.0123456789abcdef.txt").write_text("cached")
(legacy / "hypermnesia-no-repo-myapp").write_text("")
(legacy / "mem-extract.lock").write_text(f"{os.getpid()}\n")
os.utime(legacy / "mem-extract.lock", (0, time.time() - 3 * 3600))   # three hours old: a leftover
(legacy / "unrelated.json").write_text("{}")

ck = str(repo)                       # a real checkout on disk, under any name
far = "/w/Work/HyperMnesia"          # a path from another machine, named after the project
claude_json = home / ".claude.json"
claude_json.write_text(json.dumps({
    "mcpServers": {"hypermnesia": {"command": f"{ck}/mcp-server/target/release/hypermnesia-mcp",
                                   "env": {"HM_SEARCH": f"{ck}/ingest/search.py",
                                           "HM_MEM_OPS": f"{far}/ingest/mem_ops.py",
                                           "HM_REPO": "myrepo"}},
                   "other": {"command": "/usr/bin/other-mcp"}},
    "projects": {far: {"allowedTools": []}}}, indent=2))
settings = legacy / "settings.json"
settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
    {"type": "command", "command": f"python3 {ck}/hooks/mem_recall.py"},
    {"type": "command", "command": f"/usr/bin/python3 {far}/hooks/mem_profile.py"},
    {"type": "command", "command": "python3 /w/Work/app/.claude/hooks/arch_invariants.py"},
    {"type": "command", "command": f"python3 {ck}/hooks/not_shipped.py"}]}]}}, indent=1))
codex = home / ".codex" / "config.toml"
codex.parent.mkdir()
codex.write_text('[mcp_servers.hypermnesia]\ncommand = "C:\\\\Work\\\\HyperMnesia\\\\target-x'
                 '\\\\release\\\\hypermnesia-mcp.exe"\n# keep "me"\n\n[other]\ncommand = "/bin/true"\n')
original_codex = codex.read_text()

# Dry run: nothing changes.
r = run(home, "--mcp-binary", str(fake), "--dry-run")
assert not hm.exists(), "a dry run created the home"
assert (legacy / "mem-queue.jsonl").exists()
assert "would move" in r.stdout and "still points into a checkout" in r.stdout, r.stdout

# First install, wired.
r = run(home, "--mcp-binary", str(fake), "--wire")
rel = hm / "releases" / head
assert (rel / "RELEASE").exists() and (rel / "hooks" / "_mem_common.py").exists(), r.stdout
assert (rel / "bin" / BIN).read_text() == fake.read_text()
assert os.access(rel / "bin" / BIN, os.X_OK)
assert os.path.realpath(hm / "current") == os.path.realpath(rel)
if not WINDOWS:
    assert os.readlink(hm / "current") == os.path.join("releases", head), "the link is relative"
    assert stat.S_IMODE(os.stat(hm).st_mode) & 0o077 == 0, "the home is private"
# The move: settings and state out, locks and unrelated files left, settings linked back.
assert (hm / "hypermnesia.env").read_text() == "MEM_AUTHOR=alice\n"
if not WINDOWS:
    assert os.path.islink(legacy / "hypermnesia.env")
    assert (legacy / "hypermnesia.env").read_text() == "MEM_AUTHOR=alice\n"
    assert not os.path.lexists(legacy / "hypermnesia.env.hm-link")
for n in ("mem-queue.jsonl", "mem-queue.done", "hypermnesia-profile-cache.0123456789abcdef.txt",
          "hypermnesia-no-repo-myapp"):
    assert (hm / "state" / n).exists() and not (legacy / n).exists(), n
assert (legacy / "mem-extract.lock").exists() and not (hm / "state" / "mem-extract.lock").exists()
assert (legacy / "unrelated.json").exists()
assert not (hm / "state.moving").exists()
# Wiring: checkout paths rewritten, everything else untouched, backups written.
cur = str(hm / "current").replace("\\", "/")
cj = json.loads(claude_json.read_text())
srv = cj["mcpServers"]["hypermnesia"]
assert srv["command"] == f"{cur}/bin/{BIN}", cj
assert srv["env"] == {"HM_SEARCH": f"{cur}/ingest/search.py", "HM_MEM_OPS": f"{cur}/ingest/mem_ops.py",
                      "HM_REPO": "myrepo"}, srv
assert cj["mcpServers"]["other"]["command"] == "/usr/bin/other-mcp"
assert cj["projects"] == {far: {"allowedTools": []}}, "a key naming the checkout is not a script"
assert claude_json.read_text().startswith('{\n  "mcpServers"'), "the layout is kept"
hooks = [h["command"] for h in json.loads(settings.read_text())["hooks"]["UserPromptSubmit"][0]["hooks"]]
assert hooks == [f"python3 {cur}/hooks/mem_recall.py",
                 f"/usr/bin/python3 {cur}/hooks/mem_profile.py",
                 "python3 /w/Work/app/.claude/hooks/arch_invariants.py",
                 f"python3 {ck}/hooks/not_shipped.py"], hooks
assert codex.read_text() == (f'[mcp_servers.hypermnesia]\ncommand = "{cur}/bin/{BIN}"\n# keep "me"\n\n'
                             '[other]\ncommand = "/bin/true"\n'), codex.read_text()
assert list(codex.parent.glob("config.toml.bak-hm-*"))[0].read_text() == original_codex
assert list(home.glob(".claude.json.bak-hm-*")), "the JSON config was backed up"

# A second run with the same ref is a fixed point.
before = {p: p.read_bytes() for p in (claude_json, settings, codex)}
r = run(home, "--mcp-binary", str(fake), "--wire")
assert "already installed" in r.stdout and "current is already" in r.stdout, r.stdout
assert {p: p.read_bytes() for p in before} == before, "a re-run rewrote a config"
assert "both" not in r.stdout, "the link left behind is not mistaken for a second settings file"

# An old-layout process wrote files after the switch. The queue is never merged under a possible
# writer: it stays for the extractor, which reads it as a second source. A cache is newer-wins.
(legacy / "mem-queue.jsonl").write_text('{"transcript_path": "/late"}\n')
(legacy / "hypermnesia-profile-cache.0123456789abcdef.txt").write_text("old cache")
os.utime(legacy / "hypermnesia-profile-cache.0123456789abcdef.txt", (1000, 1000))
r = run(home, "--mcp-binary", str(fake))
assert (hm / "state" / "mem-queue.jsonl").read_text() == '{"transcript_path": "/x"}\n', r.stdout
assert (legacy / "mem-queue.jsonl").read_text() == '{"transcript_path": "/late"}\n'
assert "extractor" in r.stdout, r.stdout
assert (hm / "state" / "hypermnesia-profile-cache.0123456789abcdef.txt").read_text() == "cached"
assert not (legacy / "hypermnesia-profile-cache.0123456789abcdef.txt").exists()
(legacy / "mem-queue.jsonl").unlink()
(legacy / "hypermnesia-store-down").write_text("")
os.utime(hm / "state" / "hypermnesia-no-repo-myapp", (1000, 1000))
(legacy / "hypermnesia-no-repo-myapp").write_text("newer")
run(home, "--mcp-binary", str(fake))
assert (hm / "state" / "hypermnesia-no-repo-myapp").read_text() == "newer", "the newer copy wins"
assert (hm / "state" / "hypermnesia-store-down").exists()

# Switching to an older commit, then pruning to one kept release, keeps both live ones -- and
# re-running the current commit (no switch at all) still keeps the rollback.
run(home, "--mcp-binary", str(fake), "--ref", "HEAD~1", "--keep", "1")
assert os.path.realpath(hm / "current") == os.path.realpath(hm / "releases" / prev)
assert (hm / "releases" / head).exists(), "the release current replaced is never pruned"
run(home, "--mcp-binary", str(fake), "--ref", "HEAD", "--keep", "1")
assert os.path.realpath(hm / "current") == os.path.realpath(rel)
assert (hm / "previous").read_text().strip() == prev
run(home, "--mcp-binary", str(fake), "--ref", "HEAD", "--keep", "1")
assert (hm / "releases" / prev).exists(), "re-running the current commit keeps the rollback"

# Installing from an installed release is refused: it is not a checkout.
r = run(home, "--dry-run", ok=False, script=hm / "current" / "ci" / "install.py")
assert r.returncode != 0 and "not a checkout" in r.stderr, r.stdout + r.stderr

# --wire only for the default home, and never for one a hook command would split.
r = run(home, "--mcp-binary", str(fake), "--home", str(tmp / "custom"), "--wire", ok=False)
assert r.returncode != 0 and "default home" in r.stderr, r.stderr
assert not (tmp / "custom").exists()
spaced = tmp / "with space"
(spaced / ".claude").mkdir(parents=True)
r = run(spaced, "--mcp-binary", str(fake), "--wire", ok=False)
assert r.returncode != 0 and "whitespace" in r.stderr, r.stderr
# Without --wire another home is fine.
run(home, "--mcp-binary", str(fake), "--home", str(tmp / "custom"))
assert (tmp / "custom" / "current" / "bin" / BIN).exists()
if not WINDOWS:
    assert os.path.realpath(legacy / "hypermnesia.env") == os.path.realpath(hm / "hypermnesia.env"), \
        "a second home must not take over the first one's settings link"

# A first install that died after moving some state into state.moving: the re-run finishes it and
# never replaces the staged queue with what an old process wrote since (that one is left).
home4 = tmp / "home4"
(home4 / ".claude").mkdir(parents=True)
staged = home4 / ".HyperMnesia" / "state.moving"
staged.mkdir(parents=True)
(home4 / ".HyperMnesia").chmod(0o700)
(staged / "mem-queue.jsonl").write_text("A\n")
(home4 / ".claude" / "mem-queue.jsonl").write_text("B\n")
(home4 / ".claude" / "hypermnesia-store-down").write_text("")
run(home4, "--mcp-binary", str(fake))
assert (home4 / ".HyperMnesia" / "state" / "mem-queue.jsonl").read_text() == "A\n"
assert (home4 / ".claude" / "mem-queue.jsonl").read_text() == "B\n"
assert (home4 / ".HyperMnesia" / "state" / "hypermnesia-store-down").exists()
assert not staged.exists()

# Both settings files present: warned about only when they differ (an identical pair is the copy
# a Windows install leaves on purpose).
home11 = tmp / "home11"
(home11 / ".claude").mkdir(parents=True)
(home11 / ".HyperMnesia").mkdir(mode=0o700)
for d in (".claude", ".HyperMnesia"):
    (home11 / d / "hypermnesia.env").write_text("MEM_AUTHOR=eve\n")
r = run(home11, "--mcp-binary", str(fake))
assert "differ" not in r.stdout, r.stdout
(home11 / ".claude" / "hypermnesia.env").write_text("MEM_AUTHOR=mallory\n")
r = run(home11, "--mcp-binary", str(fake))
assert "exist and differ" in r.stdout, r.stdout

# A settings link that someone made to a file kept elsewhere is left as it is.
if not WINDOWS:
    home6 = tmp / "home6"
    (home6 / ".claude").mkdir(parents=True)
    (tmp / "elsewhere.env").write_text("MEM_AUTHOR=carol\n")
    os.symlink(tmp / "elsewhere.env", home6 / ".claude" / "hypermnesia.env")
    run(home6, "--mcp-binary", str(fake))
    assert os.path.realpath(home6 / ".claude" / "hypermnesia.env") == os.path.realpath(tmp / "elsewhere.env")
    assert not (home6 / ".HyperMnesia" / "hypermnesia.env").exists()

# An interrupted switch: the run dies right after `current` is replaced. `previous` must already
# be on disk then, so the re-run (which sees no switch to make) still protects the rollback.
home7 = tmp / "home7"
(home7 / ".claude").mkdir(parents=True)
run(home7, "--mcp-binary", str(fake), "--ref", "HEAD~1")
assert not (home7 / ".HyperMnesia" / "previous").exists(), "a first install replaces nothing"
crash = tmp / "crash.py"
crash.write_text(f"""
import os, sys
sys.path.insert(0, {str(repo / 'ci')!r})
sys.argv = ["install.py", "--mcp-binary", {str(fake)!r}, "--ref", "HEAD", "--keep", "1",
            "--prune-age", "0"]
import install
real = os.replace
def replace(src, dst):
    real(src, dst)
    if str(dst).endswith(os.sep + "current"):
        print("previous-before-switch", os.path.exists(os.path.join(os.path.dirname(dst), "previous")), flush=True)
        os._exit(9)
install.os.replace = replace
install.main()
""")
env7 = {**{k: v for k, v in os.environ.items() if k not in ("HYPERMNESIA_HOME", "HYPERMNESIA_ENV_FILE")},
        "HOME": str(home7), "USERPROFILE": str(home7)}
r = subprocess.run([sys.executable, str(crash)], env=env7, capture_output=True, text=True)
assert r.returncode == 9 and "previous-before-switch True" in r.stdout, r.stdout + r.stderr
run(home7, "--mcp-binary", str(fake), "--ref", "HEAD", "--keep", "1", "--prune-age", "0")
assert (home7 / ".HyperMnesia" / "releases" / prev).exists(), "the rollback was pruned"

# Pruning: with no age floor, a release that is neither newest, current, previous nor running is
# removed; one a running process was started from is kept (Linux: /proc/*/exe). The copy is named
# `sleep` because coreutils may be one multi-call binary that dispatches on its own name.
home8 = tmp / "home8"
(home8 / ".claude").mkdir(parents=True)
for ref in ("HEAD~2", "HEAD~1", "HEAD"):
    run(home8, "--mcp-binary", str(fake), "--ref", ref, "--keep", "1")
rels = home8 / ".HyperMnesia" / "releases"
assert (rels / third).exists(), "the 30-day floor keeps a fresh release"
if sys.platform.startswith("linux") and shutil.which("sleep"):
    sleeper = rels / third / "bin" / "sleep"
    shutil.copy2(shutil.which("sleep"), sleeper)
    proc = subprocess.Popen([str(sleeper), "30"])
    try:
        run(home8, "--mcp-binary", str(fake), "--ref", "HEAD", "--keep", "1", "--prune-age", "0")
        assert (rels / third).exists(), "a release a running process was started from was pruned"
    finally:
        proc.kill()
        proc.wait()
# The age floor counts from when a release stopped being current, not from when it was built.
assert (rels / third / "RETIRED").exists() and (rels / prev / "RETIRED").exists()
assert not (rels / head / "RETIRED").exists(), "the current release carries no RETIRED stamp"
os.remove(rels / third / "RETIRED")
run(home8, "--mcp-binary", str(fake), "--ref", "HEAD", "--keep", "1", "--prune-age", "0")
assert (rels / third).exists(), "a release never switched away from (no stamp) is kept"
(rels / third / "RETIRED").write_text("x\n")
run(home8, "--mcp-binary", str(fake), "--ref", "HEAD", "--keep", "1", "--prune-age", "0")
assert not (rels / third).exists(), "an unused old release was kept"
assert (rels / prev).exists() and (rels / head).exists()
run(home8, "--mcp-binary", str(fake), "--ref", "HEAD~1", "--keep", "1")
assert not (rels / prev / "RETIRED").exists(), "a release switched back to loses its stamp"
assert (rels / head / "RETIRED").exists()

# An explicit HYPERMNESIA_ENV_FILE is the owner's arrangement: the file it names is not moved.
home9 = tmp / "home9"
(home9 / ".claude").mkdir(parents=True)
(home9 / ".claude" / "hypermnesia.env").write_text("MEM_AUTHOR=dan\n")
r = subprocess.run([sys.executable, str(INSTALL), "--mcp-binary", str(fake)],
                   env={**env7, "HOME": str(home9), "USERPROFILE": str(home9),
                        "HYPERMNESIA_ENV_FILE": str(home9 / ".claude" / "hypermnesia.env")},
                   capture_output=True, text=True)
assert r.returncode == 0, r.stderr
assert (home9 / ".claude" / "hypermnesia.env").is_file()
assert not os.path.islink(home9 / ".claude" / "hypermnesia.env")
assert not (home9 / ".HyperMnesia" / "hypermnesia.env").exists()

# A first install that died between moving the settings file and linking it: finished next time.
if not WINDOWS:
    home5 = tmp / "home5"
    (home5 / ".claude").mkdir(parents=True)
    (home5 / ".HyperMnesia").mkdir(mode=0o700)
    (home5 / ".HyperMnesia" / "hypermnesia.env").write_text("MEM_AUTHOR=bob\n")
    os.symlink("../.HyperMnesia/hypermnesia.env", home5 / ".claude" / "hypermnesia.env.hm-link")
    run(home5, "--mcp-binary", str(fake))
    assert os.path.islink(home5 / ".claude" / "hypermnesia.env")
    assert (home5 / ".claude" / "hypermnesia.env").read_text() == "MEM_AUTHOR=bob\n"
    assert not os.path.lexists(home5 / ".claude" / "hypermnesia.env.hm-link")

# A job of the old layout still running blocks the first install on a fresh home -- and a lock
# with no PID written yet counts as held, as it does for acquire_lock().
home2 = tmp / "home2"
(home2 / ".claude").mkdir(parents=True)
(home2 / ".claude" / "mem-queue.jsonl").write_text("x\n")
for content in (f"{os.getpid()}\n", ""):
    (home2 / ".claude" / "hypermnesia-dream.lock").write_text(content)
    r = run(home2, "--mcp-binary", str(fake), ok=False)
    assert r.returncode != 0 and "old layout is running" in r.stderr, r.stdout + r.stderr
    assert (home2 / ".claude" / "mem-queue.jsonl").exists() and not (home2 / ".HyperMnesia").exists()

# A real directory where `current` belongs is refused, not replaced.
home3 = tmp / "home3"
(home3 / ".HyperMnesia" / "current").mkdir(parents=True)
(home3 / ".HyperMnesia").chmod(0o700)
r = run(home3, "--mcp-binary", str(fake), ok=False)
assert r.returncode != 0 and "real directory" in r.stderr, r.stdout + r.stderr

# -- the console across upgrades -------------------------------------------------------------
# A stand-in cargo (CARGO): it "builds" each binary as a text naming the crate and its features,
# so which build landed where can be read back without a Rust toolchain.
fake_cargo = tmp / "fake-cargo"
fake_cargo.write_text("""#!%s
import os, sys
a = sys.argv[1:]
crate = os.path.basename(os.path.dirname(a[a.index("--manifest-path") + 1]))
feat = a[a.index("--features") + 1] if "--features" in a else ""
out = os.path.join(os.environ["CARGO_TARGET_DIR"], "release")
os.makedirs(out, exist_ok=True)
names = ["hypermnesia-mcp"] if crate == "mcp-server" else \\
    ["hypermnesia", "hypermnesia-stats", "hypermnesia-jobs", "hypermnesia-settings", "hypermnesia-setup"]
for n in names:
    with open(os.path.join(out, n), "w") as fh:
        fh.write(crate + " features=" + feat + "\\n")
""" % sys.executable)
fake_cargo.chmod(0o755)
cg = {"CARGO": str(fake_cargo)}
home12 = tmp / "home12"
(home12 / ".claude").mkdir(parents=True)
rels12 = home12 / ".HyperMnesia" / "releases"
tray_bin = lambda sha: (rels12 / sha / "bin" / "hypermnesia").read_text()
run(home12, "--mcp-binary", str(fake), "--ref", "HEAD~2", "--console=tray", env_extra=cg)
assert tray_bin(third) == "console features=tray\n"
assert "console_features=tray" in (rels12 / third / "RELEASE").read_text()
# An ordinary upgrade, no --console: the console comes along, with the same features.
r = run(home12, "--mcp-binary", str(fake), "--ref", "HEAD~1", env_extra=cg)
assert tray_bin(prev) == "console features=tray\n", r.stdout
assert "replaced release has the console" in r.stdout, r.stdout
# The same commit with other features asked for: built into the release, not reused as it is.
r = run(home12, "--mcp-binary", str(fake), "--ref", "HEAD~1", "--console", env_extra=cg)
assert tray_bin(prev) == "console features=\n", r.stdout
rec = (rels12 / prev / "RELEASE").read_text()
assert rec.count("console_features=") == 1 and "console_features=\n" in rec, rec
r = run(home12, "--mcp-binary", str(fake), "--ref", "HEAD~1", "--console", env_extra=cg)
assert "console built" not in r.stdout, "the same request rebuilt the console"
# --no-console leaves it out, even though the replaced release has one.
run(home12, "--mcp-binary", str(fake), "--ref", "HEAD", "--no-console", env_extra=cg)
assert not (rels12 / head / "bin" / "hypermnesia").exists()
r = run(home12, "--mcp-binary", str(fake), "--console", "--no-console", ok=False, env_extra=cg)
assert r.returncode != 0 and "contradict" in r.stderr, r.stderr
# Without --mcp-binary the MCP server is built by cargo too, into the home's build directory.
home13 = tmp / "home13"
(home13 / ".claude").mkdir(parents=True)
run(home13, env_extra=cg)
assert (home13 / ".HyperMnesia" / "releases" / head / "bin" / BIN).read_text() == "mcp-server features=\n"
assert (home13 / ".HyperMnesia" / "build" / "mcp-server-target" / "release" / BIN).exists()

# -- path rewriting, on the raw text ---------------------------------------------------------
# Spaces and apostrophes inside a path, Windows separators as escaped in JSON/TOML, quoted and bare
# segments of one command line, an interpreter before the path, TOML literal and multi-line
# strings, escapes left alone -- and nothing that is not a checkout path or a shipped script.
h = "/home/u/.HyperMnesia"
c = h + "/current"
shipped = {"hooks/mem_recall.py", "hooks/a.py", "hooks/b.py", "rerank/search_reranked.py",
           "ingest/search.py", "hooks/workspace/x.py"}
for text, want in [
    ('"command": "/u/Work/HyperMnesia/mcp-server/target/release/hypermnesia-mcp"', f'"command": "{c}/bin/{BIN}"'),
    ('"C:/Users/Jane Doe/Work/hm/mcp-server/target/release/hypermnesia-mcp.exe"', f'"{c}/bin/{BIN}"'),
    ('command = "C:\\\\Work\\\\hm\\\\target-infra\\\\release\\\\hypermnesia-mcp.exe"', f'command = "{c}/bin/{BIN}"'),
    ("command = 'C:\\Work\\hm\\target-infra\\release\\hypermnesia-mcp.exe'", f"command = '{c}/bin/{BIN}'"),
    ('cmd = """\n/w/hm/mcp-server/target/release/hypermnesia-mcp"""', f'cmd = """\n{c}/bin/{BIN}"""'),
    ('"python3 /u/Work/HyperMnesia/hooks/mem_recall.py"', f'"python3 {c}/hooks/mem_recall.py"'),
    ('"/usr/bin/python3 /w/HyperMnesia/hooks/mem_recall.py"', f'"/usr/bin/python3 {c}/hooks/mem_recall.py"'),
    ('"python3 \\"/Users/O\'Brien/Work/HyperMnesia/hooks/mem_recall.py\\""', f'"python3 \\"{c}/hooks/mem_recall.py\\""'),
    ('"python3 \\"/w/hypermnesia/hooks/a.py\\" && python3 /w/hypermnesia/hooks/b.py"',
     f'"python3 \\"{c}/hooks/a.py\\" && python3 {c}/hooks/b.py"'),
    ('"HM_SEARCH": "/Users/Jane Doe/Work/HyperMnesia/ingest/search.py"', f'"HM_SEARCH": "{c}/ingest/search.py"'),
    ('"HM_RERANK": "C:\\\\Work\\\\hypermnesia\\\\rerank\\\\search_reranked.py"', f'"HM_RERANK": "{c}/rerank/search_reranked.py"'),
    (f'"HM_RERANK={ck}/rerank/search_reranked.py x"', f'"HM_RERANK={c}/rerank/search_reranked.py x"'),
    ('"python3 /w/Work/app/.claude/hooks/mem_recall.py"', '"python3 /w/Work/app/.claude/hooks/mem_recall.py"'),
    ('"python3 /u/Work/HyperMnesia/hooks/gone.py"', '"python3 /u/Work/HyperMnesia/hooks/gone.py"'),
    ('"python3 hooks/mem_recall.py"', '"python3 hooks/mem_recall.py"'),
    (f'"{c}/bin/{BIN}"', f'"{c}/bin/{BIN}"'),
    (f'"python3 {c}/hooks/mem_recall.py"', f'"python3 {c}/hooks/mem_recall.py"'),
    ('"/u/Work/HyperMnesia"', '"/u/Work/HyperMnesia"'),
    ('note = "\\U0001F600"', 'note = "\\U0001F600"'),
    ('"/x/HyperMnesia/hooks/mem_recall.pyc"', '"/x/HyperMnesia/hooks/mem_recall.pyc"'),
    ('"/usr/bin/python3 $HOME/Work/HyperMnesia/hooks/mem_recall.py"', f'"/usr/bin/python3 {c}/hooks/mem_recall.py"'),
    ('"python3 ${HOME}/Work/HyperMnesia/hooks/mem_recall.py"', f'"python3 {c}/hooks/mem_recall.py"'),
    ('"/usr/bin/env python3 ${HOME}/Work/HyperMnesia/hooks/mem_recall.py"', f'"/usr/bin/env python3 {c}/hooks/mem_recall.py"'),
    ('"python3 %USERPROFILE%\\\\Work\\\\HyperMnesia\\\\hooks\\\\mem_recall.py"', f'"python3 {c}/hooks/mem_recall.py"'),
    ('"python3 /u/Work/HyperMnesia/hooks/workspace/x.py"', f'"python3 {c}/hooks/workspace/x.py"'),
    ('"python3 /u/Work/HyperMnesia/hooks/other/x.py"', '"python3 /u/Work/HyperMnesia/hooks/other/x.py"'),
]:
    got, _ = inst.rewrite(text, h, shipped)
    assert got == want, (text, got, want)

shutil.rmtree(tmp, ignore_errors=True)
print("test_install: ok")
