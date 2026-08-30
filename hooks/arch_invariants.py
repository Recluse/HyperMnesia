#!/usr/bin/env python3
"""Claude Code PreToolUse hook (Edit|Write|MultiEdit): inject Tier-0/1 invariants.

Before an edit runs, resolve the file being touched to its HyperMnesia component(s)
and inject the applicable `must` constraints as additionalContext -- so the agent sees
the rules for that file deterministically, not from memory, and without having to think
to call the get_constraints tool. This is the architectural-memory counterpart to the
personal-memory recall hook.

FAIL-OPEN: any error (DB down, no graph, no match) -> exit 0, no output, never blocks the
edit. Register in the project's .claude/settings.json under PreToolUse, matcher
"Edit|Write|MultiEdit". Scope: HM_REPO (or the cwd basename), same as the MCP server.

Env: DATABASE_URL, HM_REPO (optional), HM_PYTHON (optional).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mem_common import psql  # noqa: E402
from _arch import GRAPH_SQL, get_constraints  # noqa: E402


def _repo(cwd):
    raw = os.environ.get("HM_REPO") or (os.path.basename(cwd) if cwd else "")
    safe = "".join(c for c in raw if c.isalnum() or c in "._-")
    return safe or "default"


def _rel(paths, cwd):
    cwd = (cwd or "").replace("\\", "/").rstrip("/")
    out = []
    for p in paths:
        pp = (p or "").replace("\\", "/")
        if cwd and pp.lower().startswith(cwd.lower()):
            pp = pp[len(cwd):].lstrip("/")
        if pp:
            out.append(pp)
    return out


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tn = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    if tn in ("Edit", "Write"):
        paths = [ti["file_path"]] if ti.get("file_path") else []
    elif tn == "MultiEdit":
        paths = [e["file_path"] for e in ti.get("edits", []) if e.get("file_path")]
    else:
        sys.exit(0)

    cwd = data.get("cwd", "") or ""
    rel = _rel(paths, cwd)
    if not rel:
        sys.exit(0)

    raw = psql(GRAPH_SQL, timeout=8)
    if not raw or not raw.strip():
        sys.exit(0)
    try:
        graph = json.loads(raw.strip())
        res = get_constraints(rel, graph, _repo(cwd))
    except Exception:
        sys.exit(0)

    musts = [c for c in res["constraints"] if c.get("severity") == "must"]
    if not musts:
        sys.exit(0)

    lines = [f"HyperMnesia -- applicable architecture invariants for {', '.join(rel)}:"]
    for c in musts:
        tag = ("global" if c.get("scope") == "global"
               else c.get("component", "") if c.get("_via") == "direct"
               else f"via {c.get('component', '')}")
        lines.append(f"  - [{tag}] {c.get('title', '')}: {c.get('statement', '')}")
    if res["unmatched"]:
        lines.append(f"  (note: {', '.join(res['unmatched'])} maps to no component -- "
                     f"consider adding it to the map)")

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "additionalContext": "\n".join(lines),
    }}))
    sys.exit(0)


if __name__ == "__main__":
    main()
