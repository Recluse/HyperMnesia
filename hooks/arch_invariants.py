#!/usr/bin/env python3
"""Claude Code PreToolUse hook (Edit|Write|MultiEdit): inject Tier-0/1 invariants.

Before an edit runs, resolve the file being touched to its HyperMnesia component(s)
and inject the applicable `must` constraints as additionalContext -- so the agent sees
the rules for that file deterministically, not from memory, and without having to think
to call the get_constraints tool. This is the architectural-memory counterpart to the
personal-memory recall hook.

FAIL-OPEN but NOT fail-silent: any error exits 0 and never blocks the edit, yet a fault that
makes the hook stop injecting says so ONCE. The two are different failures that used to look
identical from the agent's seat -- "this file has no rules" and "the rules never arrived" --
and the second one silently turns the whole feature off. Registered in the project's
.claude/settings.json under PreToolUse, matcher "Edit|Write|MultiEdit". Scope: HM_REPO (or the
cwd basename), same as the MCP server.

Announced once per episode (see flag_flip): the store not answering, a graph that will not
parse, and a repo scope that matches no component at all -- the last being the common
misconfiguration, because the scope is an exact string and a folder named `Infra` ingested as
`infra` resolves to nothing, forever, quietly.

Paths are resolved against the REPO ROOT, not the session's cwd: CLAUDE_PROJECT_DIR if the host
sets it, else `git rev-parse --show-toplevel` from the cwd, else the cwd. Stripping the cwd
meant that after `cd src/` every path lost its `src/` and no glob matched any more. The MCP
server strips HM_ROOT (else its own git top level, else its cwd) the same way.

Env: DATABASE_URL, HM_REPO (optional; default the repo root's folder name), HM_PYTHON
(optional), CLAUDE_PROJECT_DIR (set by the host).
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mem_common import psql, flag_flip, store_down_flip  # noqa: E402
from _arch import GRAPH_SQL, get_constraints, is_abs, scope_note  # noqa: E402


def _root(cwd):
    """The repo root: CLAUDE_PROJECT_DIR, else the git top level of `cwd`, else `cwd`."""
    explicit = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if explicit:
        return explicit
    if cwd and os.path.isdir(cwd):
        try:
            top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                                 capture_output=True, timeout=3)
            if top.returncode == 0 and top.stdout.strip():
                return top.stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return cwd or ""


def _repo(root):
    raw = os.environ.get("HM_REPO") or (os.path.basename(root.rstrip("/\\")) if root else "")
    safe = "".join(c for c in raw if c.isalnum() or c in "._-")
    return safe or "default"


def _abs(paths, cwd):
    """Tool paths made absolute: a relative one is relative to the session's cwd. A
    drive-qualified path (`C:/work/x.py`) is absolute, on every platform. Containment and
    symlinks are decided by the shared resolver (_arch.rel_to_root), not here."""
    out = []
    for p in paths:
        pp = (p or "").replace("\\", "/")
        if pp and not is_abs(pp) and cwd:
            pp = cwd.replace("\\", "/").rstrip("/") + "/" + pp
        if pp:
            out.append(pp)
    return out


def _warn_once(flag, text):
    """Emit a fault notice the first time this fault appears, then exit 0.

    Still fail-open -- the edit proceeds either way. The point is only that the agent learns
    the difference between "no rules apply" and "the rules never loaded".
    """
    if flag_flip(flag, True):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": text}}))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tn = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    # Edit, Write AND MultiEdit all carry the target file at tool_input.file_path -- MultiEdit's
    # edits[] hold only old_string/new_string, NOT a path. (Reading edits[].file_path silently
    # skipped MultiEdit entirely -- exactly where invariants matter most.)
    if tn in ("Edit", "Write", "MultiEdit"):
        paths = [ti["file_path"]] if ti.get("file_path") else []
    else:
        sys.exit(0)

    cwd = data.get("cwd", "") or ""
    root = _root(cwd)
    rel = _abs(paths, cwd)
    if not rel:
        sys.exit(0)

    repo = _repo(root)

    # GRAPH_SQL wraps everything in coalesce(), so a healthy store always returns one row --
    # an empty answer means the query never really ran, same as a failure.
    raw = psql(GRAPH_SQL, timeout=8)
    if raw is None or not raw.strip():
        _warn_once("store-down", "HyperMnesia: the architecture store did not answer, so NO "
                                 "invariants were injected for this edit. Absence of rules "
                                 "below is not evidence that none apply.")
    store_down_flip(False)

    try:
        graph = json.loads(raw.strip())
    except ValueError:
        _warn_once("graph-unparseable",
                   "HyperMnesia: the architecture graph came back unparseable, so NO invariants "
                   "were injected. This is a defect, not an empty map -- check the store.")
    flag_flip("graph-unparseable", False)

    # An exact-string scope with no components behind it is the failure that hides best: every
    # edit resolves to nothing and the agent is told nothing, indefinitely. Name the tags that
    # DO exist, because the fix is almost always one of them (a case difference, a renamed
    # folder) rather than a missing map.
    note = scope_note(graph, repo)       # the same text get_constraints / locate print
    if note:
        _warn_once("no-repo-" + repo, "HyperMnesia: " + note)
    flag_flip("no-repo-" + repo, False)

    try:
        res = get_constraints(rel, graph, repo, root or None)
    except Exception:
        sys.exit(0)

    musts = [c for c in res["constraints"] if c.get("severity") == "must"]
    # A file outside the root this hook resolved against is SAID to be outside, never matched
    # against this repo's globs: with CLAUDE_PROJECT_DIR naming one repo and the edit landing in
    # another, `**/*.py` would otherwise hand the first repo's rules to the second's files.
    outside_note = (f"HyperMnesia: {', '.join(res['outside'])} is outside the repo root "
                    f"{root!r} this hook resolves against "
                    f"({'CLAUDE_PROJECT_DIR' if os.environ.get('CLAUDE_PROJECT_DIR', '').strip() else 'git top level / cwd'}), "
                    f"so no rule of {repo!r} was applied to it."
                    if res["outside"] else "")
    if not musts:
        if outside_note:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "additionalContext": outside_note}}))
        sys.exit(0)

    shown = [p for p, _ in res["touched"]] + res["unmatched"]
    lines = [f"HyperMnesia -- applicable architecture invariants for {', '.join(shown)}:"]
    for c in musts:
        tag = ("global" if c.get("scope") == "global"
               else c.get("component", "") if c.get("_via") == "direct"
               else f"via {c.get('component', '')}")
        lines.append(f"  - [{tag}] {c.get('title', '')}: {c.get('statement', '')}")
    if res["unmatched"]:
        lines.append(f"  (note: {', '.join(res['unmatched'])} maps to no component -- "
                     f"consider adding it to the map)")
    if outside_note:
        lines.append("  (" + outside_note + ")")

    # Inject context ONLY -- deliberately no permissionDecision. Emitting "allow" here would
    # auto-approve every edit that happens to have a `must` invariant, silently bypassing the
    # normal approval flow; "no opinion" is expressed by omitting the key.
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "\n".join(lines),
    }}))
    sys.exit(0)


if __name__ == "__main__":
    main()
