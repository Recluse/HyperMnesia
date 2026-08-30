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
PROFILE_CACHE = "/tmp/hypermnesia-profile-cache.txt"

PROMPT = """You maintain a long-term-memory "page" for an AI coding agent about ONE project. Below \
are its active memory records (type + text). Write a concise, well-organized overview a teammate \
could read to get up to speed: group related facts under short headings, KEEP specifics (paths, \
names, decisions, gotchas, numbers), drop redundancy and chit-chat. No preamble or sign-off -- \
output only the page. Keep it under ~250 words."""


def reflect_one(project, dry):
    grp = json.loads(mem_ops("reflect_group", {"project": project}, timeout=30) or "[]")
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

    if os.path.exists(LOCK):                         # stale-lock safe (killed run -> 2h)
        if time.time() - os.path.getmtime(LOCK) < 7200:
            print("another reflect run holds the lock; exiting")
            return
        try:
            os.remove(LOCK)
        except OSError:
            pass
    open(LOCK, "w").close()
    try:
        if only:
            targets = [{"project": only}]
        else:
            targets = json.loads(mem_ops("reflect_targets", {"min": MIN_MEMS}, timeout=30) or "[]")
        print(f"{len(targets)} project(s) to reflect")
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
