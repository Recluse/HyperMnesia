#!/usr/bin/env python3
"""SessionStart hook -- inject the pinned personal-memory profile (~1-1.5k tokens).

Registered with matcher startup|resume|clear|compact: injected context does NOT
survive compaction, so the compact matcher re-injects it.
SQL-only (no embedding) -> one psql call. Stale cache beats nothing when the
cluster is unreachable. Fail-open.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mem_common import psql, read_stdin_json, fence

CACHE = "/tmp/hypermnesia-profile-cache.txt"
TTL = 300
TRUNC = 220  # chars per memory line

SQL = r"""
SELECT '#PREF# ' || id || '|' || left(content, %(t)s) FROM mem.active_memories
 WHERE memory_type='preference' ORDER BY importance DESC, created_at DESC LIMIT 10;
SELECT '#FACT# ' || id || '|' || left(content, %(t)s) FROM mem.active_memories
 WHERE memory_type IN ('semantic','procedural') AND importance >= 0.6
 ORDER BY importance DESC, created_at DESC LIMIT 10;
SELECT '#PLAN# ' || id || '|' || left(content, %(t)s) FROM mem.active_memories
 WHERE memory_type='prospective' ORDER BY importance DESC, created_at DESC LIMIT 5;
""".replace("%(t)s", str(TRUNC))


def build_profile():
    raw = psql(SQL, timeout=12)
    if raw is None:
        return None
    prefs, facts, plans = [], [], []
    for line in raw.splitlines():
        if line.startswith("#PREF# "):
            prefs.append(line[7:].split("|", 1)[-1])
        elif line.startswith("#FACT# "):
            facts.append(line[7:].split("|", 1)[-1])
        elif line.startswith("#PLAN# "):
            plans.append(line[7:].split("|", 1)[-1])
    if not (prefs or facts or plans):
        return ""
    out = []                                   # body only; main() wraps it via fence()
    if prefs:
        out.append("[Preferences]")
        out += [f"- {p}" for p in prefs]
    if facts:
        out.append("[Key facts]")
        out += [f"- {f}" for f in facts]
    if plans:
        out.append("[Open plans/intentions]")
        out += [f"- {p}" for p in plans]
    return "\n".join(out)


def main():
    read_stdin_json()  # source field unused -- all matchers get the same profile
    profile = None
    if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < TTL:
        try:
            profile = open(CACHE, encoding="utf-8").read()
        except OSError:
            pass
    if profile is None:
        profile = build_profile()
        if profile is not None:
            try:
                with open(CACHE, "w", encoding="utf-8") as f:
                    f.write(profile)
            except OSError:
                pass
        elif os.path.exists(CACHE):  # cluster down -> stale cache
            try:
                profile = open(CACHE, encoding="utf-8").read()
            except OSError:
                profile = None
    if profile:
        print(fence("profile",
                    "Owner's long-term memory (pinned; full search: the memory_search tool):",
                    profile))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open
    sys.exit(0)
