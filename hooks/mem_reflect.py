#!/usr/bin/env python3
"""Reflect pass -- synthesize a per-project "knowledge page" from its active memories
(steal #3, from Hindsight's knowledge pages). Recall then surfaces one coherent overview instead
of N scattered fragments.

Anti-staleness by construction: each run REBUILDS every project's page from its *current* active
memories and supersedes the prior page (page_upsert). A page is never edited in place and never
drifts from its sources -- if the memories change, the next reflect regenerates it.

Pages are tagged metadata.kind='page' and are excluded from the novelty gate (mem_ops nearest) and
the consolidator, so they neither suppress capture of their own sources nor get merged into them.

Run out of band (cron/systemd/launchd), NOT from a blocking hook. Needs HM_LLM* (see _llm.py).

    python3 hooks/mem_reflect.py [--dry-run] [--project NAME]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mem_common import mem_ops  # noqa: E402
from _llm import complete        # noqa: E402

MIN_MEMS = int(os.environ.get("MEM_REFLECT_MIN", "5"))   # skip projects with fewer active memories
MAX_MEMS = int(os.environ.get("MEM_REFLECT_MAX", "80"))  # cap what we feed the LLM
LOCK = os.path.expanduser("~/.claude/mem-reflect.lock")
PROFILE_CACHE = os.path.expanduser("~/.claude/hypermnesia-profile-cache.txt")  # keep in sync with mem_profile.py

PROMPT = """You maintain a long-term-memory "page" for an AI coding agent about ONE project. Below \
are its active memory records (type + text). Write a concise, well-organized overview a teammate \
could read to get up to speed: group related facts under short headings, KEEP specifics (paths, \
names, decisions, gotchas, numbers), drop redundancy and chit-chat. No preamble or sign-off -- \
output only the page. Keep it under ~250 words."""


def reflect_one(project, dry):
    raw = mem_ops("reflect_group", {"project": project}, timeout=30)
    if raw is None:
        raise RuntimeError("store unreachable while fetching the group")
    grp = json.loads(raw)
    if len(grp) < MIN_MEMS:
        return False
    body = "\n".join(f"- [{m['type']}] {m['content']}" for m in grp[:MAX_MEMS])
    page = complete(PROMPT, f"Project: {project}\n\n{body}")
    if not page or not page.strip():
        print(f"  {project}: LLM returned nothing, skip")
        return False
    if dry:
        print(f"  [dry] {project} ({len(grp)} mems) ->\n{page.strip()[:400]}\n")
        return False
    r = mem_ops("page_upsert", {
        "project": project, "content": page.strip(), "importance": 0.6,
        "source": {"source_type": "reflection", "channel": "reflect"},
    }, timeout=30)
    ok = bool(r and "saved" in r)
    print(f"  {project} ({len(grp)} mems): {r.strip() if r else 'WRITE FAILED'}")
    return ok


def main():
    dry = "--dry-run" in sys.argv
    only = sys.argv[sys.argv.index("--project") + 1] if "--project" in sys.argv else None

    # The lock records the PID, so a run that was KILLED (its finally never ran) does not block
    # the next one for two hours while looking exactly like a run in progress. Age alone was the
    # only test before, and a stale lock is silent: the scheduled pass prints "another run holds
    # the lock" and does nothing.
    if os.path.exists(LOCK):
        try:
            holder = int(open(LOCK, encoding="utf-8").read().strip() or 0)
        except (OSError, ValueError):
            holder = 0
        alive = False
        if holder:
            try:
                os.kill(holder, 0)          # signal 0: liveness test, sends nothing
                alive = True
            except OSError:
                alive = False
        if alive and time.time() - os.path.getmtime(LOCK) < 7200:
            print(f"another reflect run (pid {holder}) holds the lock; exiting", flush=True)
            return
        if not alive:
            print(f"clearing a stale lock (pid {holder or '?'} is gone)", flush=True)
        try:
            os.remove(LOCK)
        except OSError:
            pass
    with open(LOCK, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    try:
        if only:
            targets = [{"project": only}]
        else:
            raw = mem_ops("reflect_targets", {"min": MIN_MEMS}, timeout=30)
            if raw is None:
                # Not "nothing to reflect": the store never answered. Printing 0 here made a
                # failed scheduled run indistinguishable from one with no work to do.
                print("store unreachable; exiting", flush=True)
                return
            targets = json.loads(raw)
        print(f"{len(targets)} project(s) to reflect", flush=True)
        changed = False
        for t in targets:
            try:
                changed |= reflect_one(t["project"], dry)
            except Exception as e:
                print(f"  {t.get('project')}: FAILED {e}")
        if changed and not dry and os.path.exists(PROFILE_CACHE):
            try:
                os.remove(PROFILE_CACHE)
                print("profile cache invalidated")
            except OSError:
                pass
    finally:
        os.remove(LOCK)


if __name__ == "__main__":
    main()
