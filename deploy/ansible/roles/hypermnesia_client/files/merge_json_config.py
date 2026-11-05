#!/usr/bin/env python3
"""Merge HyperMnesia's Claude/Codex hook entries without owning the rest of the file."""
import argparse
import datetime
import json
import os
import pathlib
import shutil
import tempfile


def load(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: top-level JSON must be an object")
    return data


def add_unique(seq, value):
    if value not in seq:
        seq.append(value)


def hook(command, timeout, matcher):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command,
                                             "timeout": timeout}]}


def write(path, data):
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        print("unchanged")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.before-hypermnesia-{stamp}.bak")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("changed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=("claude", "claude-hooks", "codex-hooks"), required=True)
    ap.add_argument("--path", type=pathlib.Path, required=True)
    ap.add_argument("--home", type=pathlib.Path, required=True)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--recall", action="store_true")
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    data = load(args.path)
    binary = str(args.home / "current/bin/hypermnesia-mcp")

    if args.kind == "claude":
        servers = data.setdefault("mcpServers", {})
        old = servers.get("hypermnesia")
        desired = {"type": "stdio", "command": binary, "args": []}
        if old is not None and old != desired and not args.force:
            raise SystemExit("mcpServers.hypermnesia differs from the complete descriptor; "
                             "set hypermnesia_force_client_merge only after reviewing it")
        servers["hypermnesia"] = desired
    else:
        hooks = data.setdefault("hooks", {})
        if args.kind == "claude-hooks":
            deny = data.setdefault("permissions", {}).setdefault("deny", [])
            add_unique(deny, "mcp__hypermnesia__memory_write")
            add_unique(deny, "mcp__hypermnesia__memory_supersede")
        py = "python3"
        if args.profile:
            add_unique(hooks.setdefault("SessionStart", []), hook(
                f'{py} "{args.home}/current/hooks/mem_profile.py"', 15,
                "startup|resume|clear|compact"))
        if args.recall:
            add_unique(hooks.setdefault("UserPromptSubmit", []), hook(
                f'{py} "{args.home}/current/hooks/mem_recall.py"',
                10 if args.kind == "claude-hooks" else 15, ""))
        if args.capture and args.kind == "claude-hooks":
            for event in ("SessionEnd", "PreCompact"):
                add_unique(hooks.setdefault(event, []), hook(
                    f'{py} "{args.home}/current/hooks/mem_capture.py"', 5, ""))
    write(args.path, data)


if __name__ == "__main__":
    main()
