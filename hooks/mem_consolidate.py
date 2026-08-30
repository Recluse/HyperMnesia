#!/usr/bin/env python3
"""Memory consolidator (M3) -- the sleep-time pass over mem.* (sleep-time consolidation pattern:
a background agent owns the write path; 'write everything, never revisit' is the
canonical failure). Run daily from launchd/cron on the mac.

  python3 hooks/mem_consolidate.py [--dry-run]

1. Find groups of similar ACTIVE memories (pairwise cosine over mem embeddings, SQL).
2. the LLM decides per group: keep | merge (one canonical text) |
   supersede (one member is the current truth, others outdated).
3. Apply: merged/current memory written via mem_ops (source_type=consolidation),
   losers marked superseded via mem_ops mark. Profile cache invalidated.
Lock-serialized, small blast radius: touches only groups the LLM ruled on.
"""
import json, math, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mem_common import mem_ops, psql, defang
from _llm import complete

LOCK = os.path.expanduser("~/.claude/mem-consolidate.lock")
PROFILE_CACHE = "/tmp/hypermnesia-profile-cache.txt"
SIM_DIST = 0.35          # cosine distance ceiling for "similar enough to review"

GROUPS_SQL = f"""
SELECT a.id, b.id, round((a.embedding <=> b.embedding)::numeric, 3)
FROM mem.active_memories a
JOIN mem.active_memories b ON b.id > a.id
WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
  AND (a.metadata->>'kind') IS DISTINCT FROM 'page'   -- pages are syntheses of their sources:
  AND (b.metadata->>'kind') IS DISTINCT FROM 'page'   -- never merge a page with what it summarizes
  AND a.embedding <=> b.embedding < {SIM_DIST}
ORDER BY a.embedding <=> b.embedding;
"""

PROMPT = """You are a long-term-memory consolidator for an AI coding agent. Below is a group of \
similar ACTIVE memory records (id + type + text). Decide what to do with them:
- "keep" -- they are about different things; leave all as-is;
- "merge" -- they are duplicates/fragments of one piece of knowledge: give one canonical text \
(self-contained, no loss of specifics) that will REPLACE all records in the group;
- "supersede" -- the records contradict each other: give the id of the current one (winner_id), \
the rest will be marked outdated.

Answer STRICTLY as one JSON object, no prose. For merge/supersede add "confidence":0.0-1.0 -- how \
sure you are (low confidence -> a human reviews it; do not fabricate confidence):
{"action":"keep"} | {"action":"merge","content":"...","type":"preference|semantic|episodic|prospective|procedural","importance":0.0-1.0,"confidence":0.0-1.0} | {"action":"supersede","winner_id":N,"confidence":0.0-1.0}
"""

REVIEW_THRESHOLD = float(os.environ.get("MEM_REVIEW_THRESHOLD", "0.8"))  # below -> park for review


def load_memory(mid):
    out = mem_ops("get", {"id": mid}, timeout=20) or ""
    return out.strip().splitlines()[0] if out.strip() else None


def decide(group_lines):
    # LLM failure -> "keep" (never a destructive default when the model is unavailable).
    raw = complete(PROMPT, "\n".join(group_lines), timeout=300) or ""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"action": "keep"}
    try:
        return json.loads(raw[start:end + 1])
    except ValueError:
        return {"action": "keep"}


def union_groups(pairs):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _ in pairs:
        parent[find(a)] = find(b)
    groups = {}
    for x in list(parent):
        groups.setdefault(find(x), set()).add(x)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def main():
    dry = "--dry-run" in sys.argv
    if os.path.exists(LOCK):                         # stale-lock safe (killed run -> 2h)
        if time.time() - os.path.getmtime(LOCK) < 7200:
            print("another consolidate run holds the lock; exiting")
            return
        print("stale lock (>2h) -- removing")
        try:
            os.remove(LOCK)
        except OSError:
            pass
    open(LOCK, "w").close()
    try:
        raw = psql(GROUPS_SQL, timeout=30)
        if raw is None:
            print("database unreachable; exiting")
            return
        pairs = []
        for line in raw.strip().splitlines():
            a, b, d = line.split("|")
            pairs.append((int(a), int(b), float(d)))
        groups = union_groups(pairs)
        print(f"{len(groups)} similar group(s) to review")
        changed = False
        for g in groups:
            lines = [l for l in (load_memory(m) for m in g) if l]
            if len(lines) < 2:
                continue
            verdict = decide(lines)
            action = verdict.get("action", "keep")
            print(f"  group {g}: {action}")
            if dry or action == "keep":
                continue
            # confidence gate: only auto-mutate when the LLM is sure; otherwise park for review.
            try:
                conf = float(verdict.get("confidence"))
            except (TypeError, ValueError):
                conf = 0.0
            if not math.isfinite(conf):     # NaN/inf must NOT sneak past the gate (nan<0.8 is False)
                conf = 0.0
            if conf < REVIEW_THRESHOLD:
                r = mem_ops("review_add", {"action": action, "member_ids": g,
                            "proposal": verdict, "confidence": conf}, timeout=30)
                print(f"    low-confidence ({conf:.2f}) -> queued for review: {r and r.strip()}")
                continue  # no mutation -> profile cache stays valid
            if action == "merge" and verdict.get("content"):
                r = mem_ops("write", {
                    "type": verdict.get("type", "semantic"),
                    "content": defang(verdict["content"]),       # untrusted model output
                    "importance": min(0.7, max(0.0, float(verdict.get("importance", 0.6)))),
                    "source": {"source_type": "consolidation", "channel": "consolidator",
                               "excerpt": f"merged from {g}"}}, timeout=30)
                # NEVER hide the originals unless the replacement actually landed -- a failed
                # write (embedder/DB hiccup) would otherwise silently drop the whole group.
                if not (r and "saved" in r):
                    print(f"    merge write FAILED, keeping originals intact: {r!r}")
                    continue
                print(f"    {r.strip()}")
                for m in g:
                    mem_ops("mark", {"id": m, "status": "superseded"}, timeout=20)
                changed = True
            elif action == "supersede" and verdict.get("winner_id") in g:
                for m in g:
                    if m != verdict["winner_id"]:
                        mem_ops("mark", {"id": m, "status": "superseded"}, timeout=20)
                        print(f"    marked #{m} superseded (winner #{verdict['winner_id']})")
                changed = True
        if changed and not dry:
            try:
                os.remove(PROFILE_CACHE)
                print("profile cache invalidated")
            except OSError:
                pass
        print("done" + (" (dry-run)" if dry else ""))
    finally:
        os.remove(LOCK)


if __name__ == "__main__":
    main()
