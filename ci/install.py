#!/usr/bin/env python3
"""Install a commit of this repository as the machine's HyperMnesia release.

    python3 ci/install.py [--ref HEAD] [--home DIR] [--mcp-binary PATH] [--console[=FEATURES]]
                          [--keep 3] [--wire] [--wire-file PATH]... [--dry-run]
    ./hm install ...                   the same thing

Every machine gets the same shape, whatever its OS (HYPERMNESIA_HOME, default ~/.HyperMnesia):

    hypermnesia.env       settings: MEM_AUTHOR and the tunables (hooks/_mem_common.py)
    current               -> releases/<sha>   what every hook, job and MCP config runs
    releases/<sha>/       `git archive` of one commit, plus bin/hypermnesia-mcp[.exe]
    state/                queue, done list, caches, fault markers, locks
    logs/                 what the schedulers write
    build/                cargo's target directories, kept so a rebuild is incremental

Nothing has to run out of a working copy. A checkout is where code is written, and the moment a
scheduler runs one, whatever lands in that tree is what runs: a tree copied over the checkout
silently changes every job, and `cargo clean` there takes the MCP server of every open session
with it.

Steps, each idempotent -- a second run with the same ref changes nothing:
 1. The release: `git archive <ref>` into releases/<sha>, so what is installed is a commit, never
    uncommitted edits. A complete release (it has its RELEASE file) is reused.
 2. The MCP server: `cargo build --release --locked` of that tree, or --mcp-binary to use one built
    elsewhere (a machine without a Rust toolchain). It lands in releases/<sha>/bin/. --console
    builds the console crate too (optionally with cargo features, e.g. --console=tray on Linux)
    and puts its binaries beside it; a console started from there installs its autostart entry
    pointing into `current`, not into the release it happened to start from. So that an
    ordinary upgrade does not take that binary away, the console is built by default whenever
    the release being replaced has one, with the features RELEASE records for it
    (--no-console to leave it out); a reused release whose console was built with other
    features than asked for is rebuilt, not silently reused.
 3. The move out of ~/.claude: hypermnesia.env and the state files an older install kept there.
    state/ is created only after the move, and the hooks use it only once it exists (until then
    they keep using ~/.claude), so state is never split between the two. Locks are not moved, and
    the install refuses to start while one of them is held by a live process (checked again
    after the build): that job would go on writing to the old place. Everything moves by rename
    only, so a writer holding a file open keeps writing into the moved inode; the home must
    therefore be on the filesystem ~/.claude is on, and a rename that fails on the first run puts
    back what that run moved and stops, so state/ is never published half-moved. What collides with
    state/ on a later run is never merged: a queue (or its done list) stays in ~/.claude, where
    the extractor reads it as a second source and retires it once it has been quiet for a day and
    is fully done; any other file keeps the newer copy. The settings file leaves a link behind
    (on Windows, where a link needs a privilege, the file is copied instead and the old one is
    left for the owner to delete once every session has restarted): a process started before the
    switch still reads the old name. A link that
    already points somewhere else, or HYPERMNESIA_ENV_FILE being set, is someone's own
    arrangement, and the settings file is left alone.
 4. The switch: `current` is replaced in one rename (a symlink; a junction on Windows, which needs
    no privilege but is not atomic).
 5. Old releases are pruned to the newest --keep, never touching `current`, the one it replaced
    (recorded in `previous`, so re-running the current commit keeps the rollback too), one
    current within the last --prune-age days (30; each release gets a RETIRED stamp when
    `current` moves away from it), or -- on Linux -- one a running process was started from: an
    MCP server resolves its scripts in its own release for the whole session.
 6. --wire: every MCP or script path in the known configs that points into a checkout is
    rewritten to point into `current` -- Claude Code (~/.claude.json, ~/.claude/settings.json),
    Codex (~/.codex/config.toml, ~/.codex/hooks.json), OpenCode (~/.config/opencode/opencode.json)
    and any --wire-file (a project's .mcp.json, for instance). The edit is on the raw text, so
    escapes, comments and layout survive byte for byte; a rewritten file must still parse, and
    each edited file is first copied to <file>.bak-hm-<UTC stamp>. Only the default home can be
    wired -- a hook started by its client has no HYPERMNESIA_HOME. On
    Linux the release's systemd user units, if it carries any (systemd/*.service, *.timer), are
    installed too, followed by `systemctl --user daemon-reload`. Without --wire it only lists
    what still points into a checkout.

Open sessions keep the MCP process they started; they pick the new one up on restart.
"""
import argparse, datetime, glob, json, os, re, shutil, subprocess, sys, tarfile, tempfile, time

WINDOWS = os.name == "nt"
EXE = ".exe" if WINDOWS else ""
BIN = f"hypermnesia-mcp{EXE}"
# The console crate's binaries (console/Cargo.toml [[bin]]), copied when they were built.
CONSOLE_BINS = tuple(f"{n}{EXE}" for n in ("hypermnesia", "hypermnesia-stats", "hypermnesia-jobs",
                                           "hypermnesia-settings", "hypermnesia-setup"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY = os.path.expanduser("~/.claude")
# What an older install kept in ~/.claude, by the names the hooks give them (state_path() in
# hooks/). Locks are absent on purpose (see step 3); tests/test_install.py checks that every name
# the hooks use is matched by one list or the other.
STATE_GLOBS = ("mem-queue.jsonl", "mem-queue.done", "hypermnesia-profile-cache.*.txt",
               "hypermnesia-store-down", "hypermnesia-graph-unparseable", "hypermnesia-no-repo-*")
LOCK_GLOBS = ("mem-*.lock", "hypermnesia-*.lock")
# Only ever appended to, by the capture hook and the extractor. When state/ already has one, the
# ~/.claude copy is left where it is for the extractor to read and retire; every other state file
# is a cache or a marker, and the newer copy wins.
APPEND_ONLY = ("mem-queue.jsonl", "mem-queue.done")


def say(msg):
    print(msg, flush=True)


def die(msg):
    sys.stderr.write(f"install: {msg}\n")
    sys.exit(1)


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


# Characters a home may not contain. Its paths are written unquoted into hook commands, systemd
# units and launchd plists, and read back by the wiring scan below (which stops at = , [ ] { });
# escaping each format separately is a promise kept in four places, so an unsafe home is refused
# instead. The console and the setup wizard refuse the same set (console/src/home.rs).
UNSAFE_IN_HOME = set('%&"\'<>;$`|*=,[]{}')


def home_fault(raw, home):
    """Why `home` (resolved from `raw`, the value as given) cannot be used, or None."""
    if raw and raw.startswith("~") and raw[1:2] not in ("", "/", "\\"):
        return "uses the ~user form, which not every reader expands the same way"
    try:
        home.encode("utf-8")
    except UnicodeEncodeError:
        return "is not valid UTF-8"
    for c in home:
        if c.isspace():
            return "contains whitespace"
        if ord(c) < 32 or 127 <= ord(c) < 160:
            return "contains a control character"
        if c in UNSAFE_IN_HOME:
            return f"contains {c!r}"
        if c == "\\" and not WINDOWS:
            return "contains a backslash"
    return None


def default_home():
    return os.path.abspath(os.path.expanduser(os.environ.get("HYPERMNESIA_HOME") or "~/.HyperMnesia"))


def stamp(fmt="%Y-%m-%dT%H:%M:%SZ"):
    return datetime.datetime.now(datetime.timezone.utc).strftime(fmt)


def make_private_dir(path):
    """A directory only this account can write: the settings loader refuses a file whose directory
    anyone else could replace it in, so the home is created 0700 and an existing one is checked."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    if not WINDOWS:
        st = os.stat(path)
        if st.st_uid != os.getuid():
            die(f"{path} belongs to uid {st.st_uid}, not to you")
        if st.st_mode & 0o022:
            os.chmod(path, st.st_mode & ~0o022)
            say(f"-- {path}: removed group/other write")


# -- 1-2. the release ---------------------------------------------------------

def cargo_build(home, tree, crate, features=None):
    """`cargo build --release --locked` of one crate of a release tree, into a target directory
    under the home that outlives the release, so the next install's build is incremental."""
    target = os.path.join(home, "build", f"{crate}-target")
    # CARGO names another cargo, as it does for cargo's own tools (and for the tests here).
    cmd = [os.environ.get("CARGO") or "cargo", "build", "--release", "--locked",
           "--manifest-path", os.path.join(tree, crate, "Cargo.toml")]
    if features:
        cmd += ["--features", features]
    say(f"-- building {crate} (cargo, target {target})")
    r = subprocess.run(cmd, env=dict(os.environ, CARGO_TARGET_DIR=target))
    if r.returncode != 0:
        die(f"cargo build of {crate} failed; nothing was switched")
    return os.path.join(target, "release")


def add_console(home, tree, bindir, features):
    """Build the console crate of `tree` and copy its binaries into `bindir`. Each one is copied
    under a temporary name and renamed into place, so a binary of a live release is never seen
    half-written."""
    out = cargo_build(home, tree, "console", features)
    got = []
    for name in CONSOLE_BINS:
        src = os.path.join(out, name)
        if not os.path.isfile(src):
            continue                          # e.g. the tray, on a platform it is not built for
        tmp = os.path.join(bindir, f".{name}.{os.getpid()}")
        shutil.copy2(src, tmp)
        os.replace(tmp, os.path.join(bindir, name))
        got.append(name)
    if not got:
        die("the console build produced none of its binaries")
    return got


def read_release(rel):
    """The KEY=value lines of a release's RELEASE file, or {} when it has none."""
    out = {}
    try:
        with open(os.path.join(rel, "RELEASE"), encoding="utf-8") as fh:
            for line in fh:
                k, sep, v = line.rstrip("\n").partition("=")
                if sep:
                    out[k] = v
    except OSError:
        pass
    return out


def console_of(rel):
    """The cargo features the console in this release was built with ("" for none), or None when
    the release has no console. A release from before the features were recorded (a `console=`
    line and no `console_features=`) has the console with the default features: an upgrade from
    it keeps the console rather than dropping it."""
    if not os.path.isfile(os.path.join(rel, "bin", CONSOLE_BINS[1])):
        return None
    return read_release(rel).get("console_features", "")


def wanted_console(home, requested, no_console):
    """The console features this install builds, or None for no console.

    Asked for: what was asked. Not asked for: whatever the release `current` points at has, so an
    ordinary upgrade keeps an installed tray -- its autostart entry names current/bin/hypermnesia,
    and a new release without it would take the binary away at the next login. --no-console
    means none."""
    if no_console:
        return None
    if requested is not None:
        return requested
    before = current_target(home)
    had = console_of(os.path.join(home, "releases", before)) if before else None
    if had is not None:
        say(f"-- the replaced release has the console (features: {had or 'none'}): building it "
            "again (--no-console to leave it out)")
    return had


def _record_console(rel, got, features):
    """Rewrite the console lines of a release's RELEASE file, keeping every other line."""
    path = os.path.join(rel, "RELEASE")
    with open(path, encoding="utf-8") as fh:
        keep = [l for l in fh if not l.startswith(("console=", "console_features="))]
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(keep)
        fh.write(f"console={' '.join(got)}\nconsole_features={features}\n")
    os.replace(tmp, path)


def build_release(home, sha, ref, mcp_binary, console, dry):
    releases = os.path.join(home, "releases")
    final = os.path.join(releases, sha)
    if os.path.exists(os.path.join(final, "RELEASE")):
        say(f"-- release {sha}: already installed, reused")
        bindir = os.path.join(final, "bin")
        if console is not None and console_of(final) != console:
            # Not there, or built with other features (a tray asked for over a console without
            # one): built into the release as asked, never silently reused.
            if dry:
                say(f"-- release {sha}: would build the console (features: {console or 'none'})")
            else:
                got = add_console(home, final, bindir, console)
                _record_console(final, got, console)
                say(f"-- release {sha}: console built (features: {console or 'none'}): "
                    f"{', '.join(got)}")
        return final
    if dry:
        say(f"-- release {sha}: would be built from {ref}")
        return final
    os.makedirs(releases, exist_ok=True)
    if os.path.exists(final):                 # a run that died halfway: start it over
        shutil.rmtree(final)
    tmp = tempfile.mkdtemp(prefix=f".{sha}.", dir=releases)
    try:
        archive = subprocess.run(["git", "-C", REPO, "archive", "--format=tar", sha],
                                 check=True, capture_output=True).stdout
        with tempfile.TemporaryFile() as fh:
            fh.write(archive)
            fh.seek(0)
            with tarfile.open(fileobj=fh) as tar:
                # git archive writes plain files and directories under the tree; `data` refuses
                # anything that would land outside it, on the Pythons that have the filter.
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(tmp, filter="data")
                else:
                    tar.extractall(tmp)
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        dst = os.path.join(bindir, BIN)
        if mcp_binary:
            shutil.copy2(mcp_binary, dst)
            built = f"copied from {os.path.abspath(mcp_binary)}"
        else:
            shutil.copy2(os.path.join(cargo_build(home, tmp, "mcp-server"), BIN), dst)
            built = "cargo build --release --locked"
        if not WINDOWS:
            os.chmod(dst, 0o755)
        extra = ""
        if console is not None:
            extra = (f"console={' '.join(add_console(home, tmp, bindir, console))}\n"
                     f"console_features={console}\n")
        with open(os.path.join(tmp, "RELEASE"), "w", encoding="utf-8") as fh:
            fh.write(f"commit={git('rev-parse', sha)}\nref={ref}\ninstalled={stamp()}\n"
                     f"mcp={built}\n{extra}repo={REPO}\n")
        os.rename(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    say(f"-- release {sha}: built")
    return final


# -- 3. the move out of ~/.claude ---------------------------------------------

def _live_lock(path):
    """Held by a running job of the old layout, by the rule acquire_lock() in hooks/_mem_common.py
    applies: a lock older than two hours is a leftover whatever its PID says; a younger one is
    held while its PID lives, or while it has no PID yet (its owner is between creating it and
    writing to it)."""
    try:
        if time.time() - os.path.getmtime(path) >= 7200:
            return False
        with open(path, encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
    except OSError:
        return False
    except ValueError:
        return True
    if not pid or WINDOWS:
        # On Windows os.kill(pid, 0) is not a probe: signal 0 is CTRL_C_EVENT, sent to the
        # process. Any young lock counts as held there.
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def legacy_jobs_running():
    return [os.path.basename(p) for g in LOCK_GLOBS
            for p in glob.glob(os.path.join(LEGACY, g)) if _live_lock(p)]


def _rename(src, dst):
    """Move by rename only. A writer holding the file open keeps writing into the same inode at its
    new name, so nothing appended during the move is lost; a copy-then-delete would lose whatever
    arrived between the two. Returns False when a rename is impossible (another filesystem)."""
    try:
        os.replace(src, dst)
        return True
    except OSError:
        return False


def migrate(home, dry):
    """Move the settings file and the state out of ~/.claude. See step 3 of the module doc."""
    old_env = os.path.join(LEGACY, "hypermnesia.env")
    new_env = os.path.join(home, "hypermnesia.env")
    link_tmp = old_env + ".hm-link"
    explicit = os.environ.get("HYPERMNESIA_ENV_FILE")
    if explicit:
        # Every reader goes to the file this names, wherever it is; moving it would take it away
        # from them. It is the owner's arrangement, so the settings file is not touched.
        say(f"-- HYPERMNESIA_ENV_FILE={explicit} is set: the settings file is left where it is")
    elif os.path.islink(old_env):
        if os.path.realpath(old_env) != os.path.realpath(new_env):
            # Someone's own arrangement (a file kept elsewhere, another home's link): the hooks
            # read it through the fallback while this home has no file of its own, so it is left
            # exactly as it is.
            say(f"-- {old_env} is a link to {os.path.realpath(old_env)}: left alone")
    elif os.path.exists(old_env) and os.path.exists(new_env):
        with open(old_env, "rb") as a_, open(new_env, "rb") as b_:
            same = a_.read() == b_.read()
        if not same:
            say(f"!! both {old_env} and {new_env} exist and differ: the second is used; the first "
                "was left alone -- delete it once you have compared them")
        # Identical: the copy a Windows install leaves for pre-switch sessions. Nothing to say.
    elif os.path.isfile(old_env) or (os.path.lexists(link_tmp) and os.path.exists(new_env)):
        if dry:
            say(f"-- would move {old_env} -> {new_env}")
        else:
            # Processes started before the switch -- an open session's MCP server, a hook from an
            # unwired config -- still read the old name, and losing it there means writing as
            # nobody. A link keeps them working; both loaders follow it and check the real file.
            # It is made BEFORE the move under a temporary name and renamed into place after, so
            # a run that dies in between leaves something the next run finishes (this branch).
            if not WINDOWS and not os.path.lexists(link_tmp):
                os.symlink(os.path.relpath(new_env, LEGACY), link_tmp)
            if WINDOWS:
                # No link without privileges there, so the old file stays for the processes
                # started before the switch (an MCP server rereads it on every call); new readers
                # take the copy in the home, which is preferred whenever it exists.
                shutil.copy2(old_env, new_env)
                say(f"-- copied {old_env} -> {new_env}; delete the old one once every session "
                    "has been restarted")
            elif os.path.isfile(old_env) and not _rename(old_env, new_env):
                shutil.copy2(old_env, new_env)    # read-only to every hook: a copy loses nothing
                os.remove(old_env)
            if not WINDOWS:
                say(f"-- moved {old_env} -> {new_env}")
            if not WINDOWS:
                os.replace(link_tmp, old_env)
                say(f"-- left {old_env} as a link to it, for processes started before the switch "
                    "(delete it when nothing old runs)")

    state = os.path.join(home, "state")
    first = not os.path.isdir(state)
    # The first time, files are gathered in state.moving and the directory is renamed to state/
    # at the end: state/ appearing is what tells the hooks to use it (state_path()), so it must
    # appear complete. A later run (or one finishing a dead run's state.moving) only takes what
    # does not collide; see below for what happens to the rest.
    dest = state + ".moving" if first else state
    found = [p for g in STATE_GLOBS for p in sorted(glob.glob(os.path.join(LEGACY, g)))
             if os.path.isfile(p) and not os.path.islink(p)]
    probe = home
    while not os.path.exists(probe):          # a dry run has not created it yet
        probe = os.path.dirname(probe)
    if found and first and os.stat(LEGACY).st_dev != os.stat(probe).st_dev:
        # Moves are renames (see _rename); across filesystems none would succeed, and publishing
        # state/ without them would hide the queue and the rest from every hook.
        die(f"{home} is on another filesystem than {LEGACY}: the state cannot be renamed into it. "
            "Put the home next to ~/.claude, or move the state by hand with every job stopped.")
    moved = left = 0
    staged = []
    for src in found:
        name = os.path.basename(src)
        dst = os.path.join(dest, name)
        if dry:
            say(f"-- would move {src} -> {os.path.join(state, name)}")
            continue
        os.makedirs(dest, mode=0o700, exist_ok=True)
        if os.path.lexists(dst):
            if name in APPEND_ONLY:
                # Both sides have entries. The extractor reads the ~/.claude queue as a second
                # source and removes it once it has been quiet for a day and is fully done
                # (legacy_twin() in hooks/_mem_common.py), so nothing is merged here under a
                # writer that may still be appending.
                say(f"-- {src}: left for the extractor to drain (state already has one)")
                left += 1
                continue
            if os.path.getmtime(src) <= os.path.getmtime(dst):
                os.remove(src)                # a cache or a marker: the newer one stands
                continue
        if _rename(src, dst):
            moved += 1
            staged.append((dst, src))
        elif first:
            for back, orig in reversed(staged):   # put back what this run moved: no split state
                if not os.path.lexists(orig):
                    _rename(back, orig)
            die(f"{src} could not be renamed into {dest}; state/ was not published, so every hook "
                "still uses ~/.claude. Fix the cause and re-run: the files already in "
                f"{dest} are picked up.")
        else:
            say(f"!! {src}: cannot be renamed into {dest} -- left in place")
            left += 1
    if dry:
        return
    if first:
        if os.path.isdir(dest):
            os.rename(dest, state)
        else:
            os.makedirs(state, mode=0o700)
    say(f"-- state: {moved} file(s) moved from {LEGACY} into {state}"
        + (f", {left} left in {LEGACY}" if left else ""))


# -- 4-5. switch and prune ----------------------------------------------------

def current_target(home):
    cur = os.path.join(home, "current")
    try:
        return os.path.basename(os.path.normpath(os.readlink(cur)))
    except OSError:
        return None


def _is_junction(p):
    if not WINDOWS:
        return False
    if hasattr(os.path, "isjunction"):        # 3.12+
        return os.path.isjunction(p)
    try:                                      # older: readlink() reads a junction since 3.8
        os.readlink(p)
        return True
    except (OSError, ValueError):
        return False


def previous(home):
    """The release `current` replaced last, as switch() recorded it."""
    try:
        with open(os.path.join(home, "previous"), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def switch(home, sha, dry):
    cur = os.path.join(home, "current")
    before = current_target(home)
    if before == sha:
        say(f"-- current is already {sha}")
        return before
    if os.path.exists(cur) and not os.path.islink(cur) and not _is_junction(cur):
        die(f"{cur} is a real directory, not a link -- move it away first")
    if dry:
        say(f"-- would switch current: {before} -> {sha}")
        return before
    if before:
        # Written BEFORE the switch, and kept beside the link: a re-run of the same commit (which
        # sees no change) still knows which release is the rollback and never prunes it, even if
        # this run dies right after the switch.
        with open(os.path.join(home, "previous"), "w", encoding="utf-8") as fh:
            fh.write(before + "\n")
    if WINDOWS:
        if os.path.lexists(cur):
            os.rmdir(cur)                     # removes the junction, not what it points at
        subprocess.run(["cmd", "/c", "mklink", "/J", cur, os.path.join(home, "releases", sha)],
                       check=True, capture_output=True)
    else:
        tmp = os.path.join(home, f".current.{os.getpid()}")
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(os.path.join("releases", sha), tmp)
        os.replace(tmp, cur)                  # one rename: a reader sees the old or the new
    if before and os.path.isdir(os.path.join(home, "releases", before)):
        # When it stopped being current: the age pruning goes by. A session opened on it today
        # keeps it for --prune-age days from now, however long ago it was installed.
        with open(os.path.join(home, "releases", before, "RETIRED"), "w", encoding="utf-8") as fh:
            fh.write(stamp() + "\n")
    if os.path.exists(os.path.join(home, "releases", sha, "RETIRED")):
        os.remove(os.path.join(home, "releases", sha, "RETIRED"))
    say(f"-- current: {before} -> {sha}")
    return before


def _in_use(releases):
    """Releases a running process was started from (Linux: /proc/*/exe). An MCP server pins its
    release at start-up and resolves its scripts there for the rest of the session, so deleting
    it would break that session's memory_ask and project_status."""
    used = set()
    for exe in glob.glob("/proc/[0-9]*/exe"):
        try:
            target = os.readlink(exe)
        except OSError:
            continue
        rel = os.path.relpath(target, os.path.realpath(releases))
        if not rel.startswith(".."):
            used.add(rel.split(os.sep)[0])
    return used


def prune(home, keep, protect, dry, min_age_days=30):
    """Keep the newest `keep`, plus anything protected, in use, or current within the last
    `min_age_days` (its RETIRED stamp, written when it was switched away from) -- an agent
    session can stay open for weeks on the release it started with, and where /proc is not
    available (macOS, Windows) that is the only evidence of it. A release with no stamp was never
    switched away from by this installer and is kept."""
    releases = os.path.join(home, "releases")
    if not os.path.isdir(releases):
        return
    done = [d for d in os.listdir(releases)
            if not d.startswith(".") and os.path.exists(os.path.join(releases, d, "RELEASE"))]
    done.sort(key=lambda d: os.path.getmtime(os.path.join(releases, d, "RELEASE")), reverse=True)
    busy = _in_use(releases)
    cutoff = time.time() - min_age_days * 86400
    for d in done[keep:]:
        if d in protect or d in busy:
            continue
        retired = os.path.join(releases, d, "RETIRED")
        if not os.path.exists(retired) or os.path.getmtime(retired) > cutoff:
            continue
        if dry:
            say(f"-- would remove old release {d}")
        else:
            shutil.rmtree(os.path.join(releases, d))
            say(f"-- removed old release {d}")


# -- 6. wiring ----------------------------------------------------------------

# The end of a path into a checkout: the MCP binary under a cargo target directory (a crate's own,
# or a target-* one from CARGO_TARGET_DIR), or a script in one of the code directories a config
# can name -- hooks/ for the hook commands, ingest/ for HM_SEARCH and HM_MEM_OPS, rerank/ for
# HM_RERANK, ci/. Separators may be doubled: that is how a Windows path looks inside a JSON or
# TOML string, and the files are edited as text, never decoded and re-encoded -- so escapes,
# comments, literal and multi-line strings all survive byte for byte.
S = r"(?:/|\\+)"
TAIL = re.compile(
    S + r"(?:(?:mcp-server" + S + r"target[^/\\\s\"']*|target-[^/\\\s\"']+)" + S + r"release" + S
    + r"(?P<bin>hypermnesia-mcp(?:\.exe)?)"
    + r"|(?P<dir>(?:hooks|ingest|rerank|ci)(?:" + S + r"[A-Za-z0-9_-]+)?)" + S  # one level deeper too
    + r"(?P<file>[A-Za-z0-9_.-]+\.(?:py|sh)))(?![A-Za-z0-9_.-])")
# Where a path can start: `/`, `~/`, a drive (`C:/`, `C:\`, `C:\\` as escaped in a string), or a
# variable a shell or cmd expands to the home (`$HOME/`, `${HOME}/`, `%USERPROFILE%\`).
ABSOLUTE = re.compile(r"(?:/|~/|[A-Za-z]:" + S + r"|\$\{?HOME\}?" + S + r"|%[A-Za-z_]+%" + S + ")")
# A path never runs across these (braces can: ${HOME}); < and > end a plist's <string>.
HARD_STOP = set('"=,[]<>\n')
SOFT_STOP = set(" \t'")                       # ...but may contain these (a space, an apostrophe)


def _norm(p):
    return re.sub(S, "/", p)


def _local(p):
    """A path as written in a config, as this machine would open it: separators unescaped, the
    home variables expanded."""
    p = re.sub(r"\\\\+", lambda m: "\\", p) if WINDOWS else _norm(p)
    p = re.sub(r"^\$\{?HOME\}?", lambda m: os.path.expanduser("~"), p)
    p = os.path.expandvars(p) if WINDOWS else p
    return os.path.expanduser(p)


def _is_checkout(prefix):
    """Is this directory a working copy of this repository? This one; any directory that has its
    hooks and its MCP crate; and, for a config that names a path on another machine, a directory
    named after the project. A project's own .claude/hooks/ is none of them."""
    p = _norm(prefix).rstrip("/")
    if not p:
        return False
    if p == _norm(REPO).rstrip("/") or p.rsplit("/", 1)[-1].lower() == "hypermnesia":
        return True
    p = _local(prefix)
    return (os.path.isfile(os.path.join(p, "hooks", "_mem_common.py"))
            and os.path.isfile(os.path.join(p, "mcp-server", "Cargo.toml")))


def _path_start(text, tail_start, tail_end):
    """Where the path whose tail is text[tail_start:tail_end] begins, or None.

    Scans back to the nearest boundary. A space or an apostrophe can be inside a path
    (`/Users/Jane Doe/...`, `/Users/O'Brien/...`) or between an interpreter and its script
    (`/usr/bin/python3 /x/HyperMnesia/hooks/a.py`), and the text alone cannot tell which. So the
    candidates -- one per soft stop, shortest first, up to a hard stop no path crosses -- are
    checked in turn: the first that starts like an absolute path AND exists on this machine wins;
    if none exists (the checkout is already gone, or on another machine), the shortest absolute
    one does, which never swallows an interpreter."""
    i, shortest = tail_start, None
    while True:
        while i > 0 and text[i - 1] not in HARD_STOP and text[i - 1] not in SOFT_STOP:
            i -= 1
        if ABSOLUTE.match(text, i):
            if os.path.isfile(_local(text[i:tail_end])):
                return i
            if shortest is None:
                shortest = i
        if i == 0 or text[i - 1] in HARD_STOP:
            return shortest
        i -= 1                                # step over the soft stop and keep looking


def rewrite(text, home, shipped):
    """The text with every checkout path pointed into `current`, and the list of what changed.
    A script is rewritten only when what precedes its directory is a checkout and the release
    ships it (pointing at nothing is worse than pointing at the old copy). Paths already under
    the home's `current` are a fixed point."""
    cur = os.path.join(home, "current").replace("\\", "/")
    out, changes, pos = [], [], 0
    for m in TAIL.finditer(text):
        start = _path_start(text, m.start(), m.end())
        if start is None or start < pos:
            continue
        old = text[start:m.end()]
        if _norm(old).startswith(cur + "/"):
            continue
        if m.group("bin"):
            new = f"{cur}/bin/{BIN}"
        else:
            rel = f"{_norm(m.group('dir'))}/{m.group('file')}"
            if rel not in shipped or not _is_checkout(text[start:m.start()]):
                continue
            new = f"{cur}/{rel}"
        out.append(text[pos:start] + new)
        changes.append(f"{old} -> {new}")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), changes


def wire_targets():
    files = ["~/.claude.json", "~/.claude/settings.json", "~/.codex/config.toml",
             "~/.codex/hooks.json", "~/.config/opencode/opencode.json"]
    return [os.path.expanduser(f) for f in files]


def _parses(path, text):
    """None when `text` is not a format this can check, else whether it parses."""
    try:
        if path.endswith(".json"):
            json.loads(text)
        elif path.endswith(".toml"):
            import tomllib                    # 3.11+; older Pythons skip the check
            tomllib.loads(text)
        elif path.endswith(".plist"):
            import plistlib
            plistlib.loads(text.encode("utf-8"))
        else:
            return None
    except ImportError:
        return None
    except Exception:                         # JSONDecodeError, tomllib and plist (expat) errors
        return False
    return True


def wire(home, sha, extra, apply, dry):
    shipped = set(git("ls-tree", "-r", "--name-only", sha).splitlines())
    when = stamp("%Y%m%dT%H%M%SZ")
    pending = 0
    for path in wire_targets() + [os.path.abspath(os.path.expanduser(p)) for p in extra]:
        if not os.path.isfile(path):
            if path not in wire_targets():
                say(f"!! --wire-file {path}: no such file")
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        new, changes = rewrite(text, home, shipped)
        if new == text:
            continue
        if _parses(path, text) and not _parses(path, new):
            say(f"!! {path}: the rewrite would not parse, so it was not written -- edit it by hand")
            continue
        changed = "; ".join(changes)
        if not apply or dry:
            pending += 1
            say(f"-- {path} still points into a checkout: {changed}")
            continue
        shutil.copy2(path, f"{path}.bak-hm-{when}")
        tmp = f"{path}.hm-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
        say(f"-- wired {path} (backup .bak-hm-{when}): {changed}")
    if pending and not apply:
        say("   (run with --wire to rewrite them; each file is backed up first)")
    if apply and not dry and sys.platform.startswith("linux") and shutil.which("systemctl"):
        install_units(home, when)


def install_units(home, when):
    """The release's systemd user units, when it carries any. None are shipped today; a release
    that adds them gets them installed without a change here."""
    units = os.path.expanduser("~/.config/systemd/user")
    changed = False
    for src in sorted(glob.glob(os.path.join(home, "current", "systemd", "*.service"))
                      + glob.glob(os.path.join(home, "current", "systemd", "*.timer"))):
        dst = os.path.join(units, os.path.basename(src))
        with open(src, encoding="utf-8") as fh:
            want = fh.read()
        have = None
        if os.path.exists(dst):
            with open(dst, encoding="utf-8") as fh:
                have = fh.read()
        if have == want:
            continue
        os.makedirs(units, exist_ok=True)
        if have is not None:
            shutil.copy2(dst, f"{dst}.bak-hm-{when}")
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(want)
        changed = True
        say(f"-- installed {dst}")
    if changed:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        say("-- systemctl --user daemon-reload")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", default="HEAD", help="commit to install (default HEAD)")
    ap.add_argument("--home", default=None, help="default: $HYPERMNESIA_HOME or ~/.HyperMnesia")
    ap.add_argument("--mcp-binary", help="use this hypermnesia-mcp instead of building one")
    ap.add_argument("--console", nargs="?", const="", default=None, metavar="FEATURES",
                    help="also build the console into the release (FEATURES: cargo features, "
                         "e.g. tray on Linux); by default it is built when the release being "
                         "replaced has it, with the same features")
    ap.add_argument("--no-console", action="store_true",
                    help="do not build the console, even when the replaced release has it")
    ap.add_argument("--keep", type=int, default=3, help="releases to keep (default 3)")
    ap.add_argument("--prune-age", type=int, default=30,
                    help="never prune a release that was current fewer than this many days ago "
                         "(default 30)")
    ap.add_argument("--wire", action="store_true", help="rewrite configs to point into current")
    ap.add_argument("--wire-file", action="append", default=[], help="another config to rewrite")
    ap.add_argument("--dry-run", action="store_true", help="say what would happen, change nothing")
    a = ap.parse_args()
    if a.keep < 1:
        die("--keep must be at least 1")
    if a.console is not None and a.no_console:
        die("--console and --no-console contradict each other")
    if a.mcp_binary and not os.path.isfile(a.mcp_binary):
        die(f"--mcp-binary {a.mcp_binary}: no such file")
    if os.path.exists(os.path.join(REPO, "RELEASE")):
        die(f"{REPO} is an installed release, not a checkout: run this from a working copy")
    home = os.path.abspath(os.path.expanduser(a.home)) if a.home else default_home()
    why = home_fault(a.home or os.environ.get("HYPERMNESIA_HOME"), home)
    if why:
        die(f"the home {home!r} {why}. Its paths go unquoted into hook commands, autostart "
            "entries and the wired configs, so it is refused rather than escaped. Use a plain "
            "path -- letters, digits and / . _ - + -- such as the default ~/.HyperMnesia.")
    try:
        sha = git("rev-parse", "--short=12", f"{a.ref}^{{commit}}")
    except (OSError, subprocess.CalledProcessError):
        die(f"{a.ref}: not a commit of {REPO}")
    say(f"-- home {home}, installing {a.ref} = {sha}")
    if a.wire and home != os.path.abspath(os.path.expanduser("~/.HyperMnesia")):
        # A wired hook is started by its client with that client's environment, which carries no
        # HYPERMNESIA_HOME: it would run the code of this home and read the state and settings of
        # the default one. Wiring any other home would point the configs at one directory and the
        # processes at another.
        die("--wire is for the default home (~/.HyperMnesia) only; for another home, point the "
            "configs at it yourself and give every process HYPERMNESIA_HOME")
    running = legacy_jobs_running()
    if running and not os.path.isdir(os.path.join(home, "state")):
        die(f"a job of the old layout is running ({', '.join(running)}): its state would be moved "
            "under it. Re-run when it has finished.")
    if not a.dry_run:
        make_private_dir(home)
        os.makedirs(os.path.join(home, "logs"), mode=0o700, exist_ok=True)
    console = wanted_console(home, a.console, a.no_console)
    build_release(home, sha, a.ref, a.mcp_binary, console, a.dry_run)
    running = legacy_jobs_running()           # again: a timer may have fired during the build
    if running and not os.path.isdir(os.path.join(home, "state")):
        die(f"a job of the old layout started during the build ({', '.join(running)}); the "
            "release is built, re-run when it has finished to move the state and switch")
    migrate(home, a.dry_run)
    before = switch(home, sha, a.dry_run)
    prune(home, a.keep, {sha, before, previous(home)}, a.dry_run, a.prune_age)
    wire(home, sha, a.wire_file, a.wire, a.dry_run)
    if not os.path.exists(os.path.join(home, "hypermnesia.env")) and not a.dry_run:
        say(f"!! {os.path.join(home, 'hypermnesia.env')} does not exist: create it with "
            "MEM_AUTHOR=<you> (mode 600), or nothing will be written as you")
    say(f"-- done: {os.path.join(home, 'current')} -> releases/{sha}")


if __name__ == "__main__":
    main()
