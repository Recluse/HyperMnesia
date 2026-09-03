#!/usr/bin/env python3
"""Owner CLI for the consolidation review queue (A2).

The daily consolidator parks low-confidence merge/supersede proposals instead of
auto-mutating. Review them here:

    python3 hooks/mem_review.py list
    python3 hooks/mem_review.py approve <id>
    python3 hooks/mem_review.py reject  <id>
    python3 hooks/mem_review.py stale [days]   # old, never-recalled facts to confirm or retire

Runs through the same local mem_ops path as the hooks (_mem_common.mem_ops).
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mem_common import mem_ops


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "list"
    if cmd == "list":
        print((mem_ops("review_list", {}, timeout=20) or "(database unreachable)").rstrip())
    elif cmd in ("approve", "reject") and len(args) == 2:
        decision = "approved" if cmd == "approve" else "rejected"
        out = mem_ops("review_resolve", {"id": int(args[1]), "decision": decision}, timeout=30)
        print((out or "(failed / database unreachable)").rstrip())
    elif cmd == "stale":
        days = int(args[1]) if len(args) > 1 else 180
        raw = mem_ops("stale_list", {"days": days, "limit": 50}, timeout=30)
        try:
            rows = json.loads(raw) if raw else []
        except ValueError:
            rows = []
        if not rows:
            print(f"(nothing active older than {days}d that has gone unrecalled that long)")
        for r in rows:
            proj = f" @{r['project']}" if r.get("project") else ""
            print(f"[#{r['id']}] {r['age_days']}d {r['type']}{proj}: {r['content']}")
    else:
        print("usage: mem_review.py list | approve <id> | reject <id> | stale [days]")


if __name__ == "__main__":
    main()
