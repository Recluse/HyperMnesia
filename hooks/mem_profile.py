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
REVIEW_LINE = ("[Waiting for your review] {n} consolidation proposal(s) queued (oldest {d}) -- run: python3 hooks/mem_review.py list")
DOWN_NOTE = ("Long-term memory is CURRENTLY UNAVAILABLE (the store did not answer). Proceed "
             "without it and do not treat it as empty: absence of memories here proves nothing.")
STALE_NOTE = "NOTE: the store did not answer; this profile is a cached copy, {age} old."
TRUNC = 220  # NB: content is whitespace-collapsed in SQL --
# a multi-line memory (e.g. a reflect page) otherwise emits continuation lines
# that match no #TAG# prefix and were silently dropped by the parser below.  # chars per memory line

SQL = r"""
SELECT '#PREF# ' || id || '|' || left(regexp_replace(content, '\s+', ' ', 'g'), %(t)s) FROM mem.active_memories
 WHERE memory_type='preference' ORDER BY importance DESC, created_at DESC LIMIT 10;
SELECT '#FACT# ' || id || '|' || left(regexp_replace(content, '\s+', ' ', 'g'), %(t)s) FROM mem.active_memories
 WHERE memory_type IN ('semantic','procedural') AND importance >= 0.6
 ORDER BY importance DESC, created_at DESC LIMIT 10;
SELECT '#PLAN# ' || id || '|' || left(regexp_replace(content, '\s+', ' ', 'g'), %(t)s) FROM mem.active_memories
 WHERE memory_type='prospective' ORDER BY importance DESC, created_at DESC LIMIT 5;
SELECT '#REVQ# ' || count(*) || '|' || coalesce(min(created_at)::date::text,'')
  FROM mem.review_queue WHERE status='pending';
""".replace("%(t)s", str(TRUNC))


def build_profile():
    raw = psql(SQL, timeout=12)
    if raw is None:
        return None
    prefs, facts, plans = [], [], []
    revq = None
    for line in raw.splitlines():
        if line.startswith("#PREF# "):
            prefs.append(line[7:].split("|", 1)[-1])
        elif line.startswith("#FACT# "):
            facts.append(line[7:].split("|", 1)[-1])
        elif line.startswith("#REVQ# "):
            revq = line[7:]
        elif line.startswith("#PLAN# "):
            plans.append(line[7:].split("|", 1)[-1])
    if not (prefs or facts or plans or revq):
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
    if revq:
        cnt, _, oldest = revq.partition("|")
        if cnt.isdigit() and int(cnt) > 0:
            out.append("")
            out.append(REVIEW_LINE.format(n=cnt, d=oldest or "?"))
    return "\n".join(out)


def main():
    read_stdin_json()  # source field unused -- all matchers get the same profile
    profile = None
    if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < TTL:
        try:
            profile = open(CACHE, encoding="utf-8").read()
        except OSError:
            pass
    stale_age = None
    if profile is None:
        profile = build_profile()
        if profile is not None:
            try:
                with open(CACHE, "w", encoding="utf-8") as f:
                    f.write(profile)
            except OSError:
                pass
        elif os.path.exists(CACHE):   # store unreachable -> serve the stale cache, but SAY so
            try:
                profile = open(CACHE, encoding="utf-8").read()
                stale_age = int((time.time() - os.path.getmtime(CACHE)) // 60)
            except OSError:
                profile = None
    if profile is None:
        # Unreachable AND no cache: emit the one honest line instead of nothing, so a silently
        # memory-less machine is visible rather than looking like an empty store.
        print(fence("profile", DOWN_NOTE, ""))
        return
    if stale_age is not None:
        age = f"{stale_age} min" if stale_age < 120 else f"{stale_age // 60} h"
        profile = STALE_NOTE.format(age=age) + "\n" + profile
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
