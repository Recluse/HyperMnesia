#!/usr/bin/env python3
"""Memory consolidator (M3) -- the sleep-time pass over mem.* (sleep-time consolidation pattern:
a background agent owns the write path; 'write everything, never revisit' is the
canonical failure). Run daily from cron or a user-level scheduler.

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
PROFILE_CACHE = os.path.expanduser("~/.claude/hypermnesia-profile-cache.txt")  # keep in sync with mem_profile.py
# Cosine ceiling for "close enough to be the same fact". 0.35 was "same topic", not "same
# fact" -- unrelated queries start around 0.52 on bge-m3, so a third of that span is still
# broadly topical. Measured on a 528-memory store: at 0.35 the candidate graph is one connected
# blob; at 0.20 it is 61 small groups. Tune with MEM_CONSOLIDATE_MAXDIST.
SIM_DIST = float(os.environ.get("MEM_CONSOLIDATE_MAXDIST", "0.20"))

# No group larger than this is ever acted on, whatever the model's confidence. A merge replaces
# every member with one text, so the blast radius of a wrong verdict is the group size -- that
# has to be bounded by the code, not by the model's judgement.
MAX_GROUP = int(os.environ.get("MEM_CONSOLIDATE_MAX_GROUP", "6"))

# Groups examined per run. Each one is an LLM call; draining slowly beats a nightly job that
# blocks on a hundred of them.
MAX_GROUPS_PER_RUN = int(os.environ.get("MEM_CONSOLIDATE_MAX_GROUPS", "10"))

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


def group_project(lines):
    """The project every member shares, or None if they disagree.

    A merge that forgets it moves the replacement into the unscoped bucket: the fact survives,
    and every search scoped to that project stops finding it. Silent, and invisible in the
    counts unless you go looking -- which is how the first ten merges lost eight tags.
    """
    seen = set()
    for l in lines:
        # "[#123] (type imp=0.7 @project, date) text"  -- the tag is optional
        head = l.split(")", 1)[0]
        seen.add(head.split("@", 1)[1].split(",")[0].strip() if "@" in head else None)
    return seen.pop() if len(seen) == 1 else None


def decide(group_lines):
    # LLM failure -> "keep" (never a destructive default when the model is unavailable).
    raw = complete(PROMPT, "\n".join(group_lines), timeout=300) or ""
    # An unparseable reply is a FAILED VERDICT, not the verdict "keep". Both are
    # non-destructive, which is why this was easy to miss, but they read completely differently:
    # a run that says "keep" for every group tells the operator memory is already consolidated,
    # when in fact the model never answered (rate limit, refusal, truncation).
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"action": "keep", "_failed": "no JSON object in the model's reply"}
    try:
        return json.loads(raw[start:end + 1])
    except ValueError as exc:
        return {"action": "keep", "_failed": f"unparseable JSON: {exc}"}


def candidate_groups(pairs, max_size=None):
    """Groups where EVERY member is close to EVERY other -- not merely chained to one.

    The first version unioned any two memories under the threshold and let that spread
    transitively. On a real store that collapses: A is close to B, B to C, C to D, and a few
    hops later "a group of similar memories" holds most of the database. Measured before this
    was changed: 369 of 528 active memories in a single group, handed to a model whose merge
    verdict would have replaced all 369 with one text.

    So the unit is a maximal clique instead. A pair stays a candidate; a chain never becomes
    one. Returns (groups, skipped) -- groups sorted tightest-first, skipped counting the
    cliques dropped for exceeding `max_size`, which the caller must report rather than swallow.
    """
    max_size = max_size or MAX_GROUP
    adj, dist = {}, {}
    for a, b, d in pairs:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
        dist[(a, b)] = dist[(b, a)] = d

    found = []

    def bron_kerbosch(r, p, x):
        if not p and not x:
            if len(r) > 1:
                found.append(sorted(r))
            return
        pivot = max(p | x, key=lambda v: len(adj[v]))
        for v in list(p - adj[pivot]):
            bron_kerbosch(r | {v}, p & adj[v], x & adj[v])
            p.discard(v)
            x.add(v)

    if adj:
        bron_kerbosch(set(), set(adj), set())

    kept = [g for g in found if len(g) <= max_size]
    skipped = len(found) - len(kept)

    def tightness(g):
        ds = [dist[(a, b)] for i, a in enumerate(g) for b in g[i + 1:]]
        return sum(ds) / len(ds)

    kept.sort(key=tightness)
    return kept, skipped


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
        groups, skipped = candidate_groups(pairs)
        total = len(groups)
        if skipped:
            # Said out loud: a run that quietly dropped them would read as "everything was
            # examined", which is the failure this whole pass is supposed to prevent.
            print(f"{skipped} candidate group(s) larger than {MAX_GROUP} were NOT reviewed "
                  f"-- too wide to act on safely; tighten MEM_CONSOLIDATE_MAXDIST")
        if total > MAX_GROUPS_PER_RUN:
            print(f"{total} group(s) to review; taking the {MAX_GROUPS_PER_RUN} tightest, "
                  f"{total - MAX_GROUPS_PER_RUN} left for the next run")
            groups = groups[:MAX_GROUPS_PER_RUN]
        else:
            print(f"{total} similar group(s) to review")
        changed = False
        failed = 0
        for g in groups:
            lines = [l for l in (load_memory(m) for m in g) if l]
            if len(lines) < 2:
                continue
            if dry and os.environ.get("MEM_CONSOLIDATE_DRY_VERDICTS", "") not in ("1", "true"):
                # The point of a dry run is to see WHAT would be reviewed. Asking the model
                # anyway made the preview cost exactly as much as the real pass -- and made it
                # impossible to look at the grouping at all while the quota was out.
                print(f"  group {g}: (dry-run, no verdict asked)")
                for l in lines:
                    print(f"      {l[:160]}")
                continue
            try:
                verdict = decide(lines)
            except Exception as exc:
                # A transient failure -- a rate limit, a network blip -- used to propagate out
                # of main() and end the whole pass at the first group, leaving every later one
                # unexamined and the run looking like a crash rather than partial work.
                failed += 1
                print(f"  group {g}: NO VERDICT ({exc}) -- left untouched")
                continue
            action = verdict.get("action", "keep")
            # Second gate, independent of the grouping: a merge replaces every member, so the
            # size of what one verdict can destroy is bounded here too. If the grouping is ever
            # loosened again, this still holds.
            if action != "keep" and len(g) > MAX_GROUP:
                print(f"  group {g}: {action} REFUSED -- {len(g)} members exceeds the "
                      f"{MAX_GROUP} this may act on")
                continue
            if verdict.get("_failed"):
                failed += 1
                print(f"  group {g}: NO VERDICT ({verdict['_failed']}) -- left untouched")
            else:
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
                payload = {
                    "type": verdict.get("type", "semantic"),
                    "content": defang(verdict["content"]),       # untrusted model output
                    "importance": min(0.7, max(0.0, float(verdict.get("importance", 0.6)))),
                    "source": {"source_type": "consolidation", "channel": "consolidator",
                               "excerpt": f"merged from {g}"}}
                # The verdict may name a project (the members disagreed and a human settled it);
                # otherwise inherit the one they all share. Never invent one.
                project = verdict.get("project") or group_project(lines)
                if project:
                    payload["project"] = project
                r = mem_ops("write", payload, timeout=30)
                # NEVER hide the originals unless the replacement actually landed -- a failed
                # write (embedder/DB hiccup) would otherwise silently drop the whole group.
                if not (r and "saved" in r):
                    print(f"    merge write FAILED, keeping originals intact: {r!r}")
                    continue
                print(f"    {r.strip()}")
                # link every loser to the replacement, so "what displaced this fact" stays
                # answerable; the merge path never sets supersedes_id (separate write + mark).
                try:
                    new_id = int(r.split("[#", 1)[1].split("]", 1)[0])
                except (IndexError, ValueError):
                    new_id = None
                for m in g:
                    mem_ops("mark", {"id": m, "status": "superseded", "by": new_id}, timeout=20)
                changed = True
            elif action == "supersede" and verdict.get("winner_id") in g:
                for m in g:
                    if m != verdict["winner_id"]:
                        mem_ops("mark", {"id": m, "status": "superseded",
                                         "by": verdict["winner_id"]}, timeout=20)
                        print(f"    marked #{m} superseded (winner #{verdict['winner_id']})")
                changed = True
        if changed and not dry:
            try:
                os.remove(PROFILE_CACHE)
                print("profile cache invalidated")
            except OSError:
                pass
        if failed:
            # Say it in the last line too: a run whose verdicts all failed otherwise reads as
            # "every group examined, nothing to do".
            print(f"WARNING: {failed} group(s) got no verdict from the model -- "
                  f"they were NOT reviewed, not 'kept'")
        print("done" + (" (dry-run)" if dry else ""))
    finally:
        os.remove(LOCK)


if __name__ == "__main__":
    main()
