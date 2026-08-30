#!/usr/bin/env python3
"""Owner CLI for the consolidation review queue (A2).

The daily consolidator parks low-confidence merge/supersede proposals instead of
auto-mutating. Review them here:

    python3 hooks/mem_review.py list
    python3 hooks/mem_review.py approve <id>
    python3 hooks/mem_review.py reject  <id>

Runs through the same local mem_ops path as the hooks (_mem_common.mem_ops).
"""
import os, sys
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
    else:
        print("usage: mem_review.py list | approve <id> | reject <id>")


if __name__ == "__main__":
    main()
