#!/usr/bin/env python3
"""SessionEnd + PreCompact hook -- enqueue the session transcript for extraction.

Fast (<1s), no LLM inline (claude-mem pattern: hooks only enqueue; a detached
worker distills). PreCompact is the safety net so material isn't lost to
compaction mid-session. The queue is consumed by mem_extract.py. Fail-open.
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mem_common import read_stdin_json

QUEUE = os.path.expanduser("~/.claude/mem-queue.jsonl")


def main():
    j = read_stdin_json()
    tp = j.get("transcript_path")
    if not tp:
        return
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "event": j.get("hook_event_name") or ("precompact" if "trigger" in j else "session_end"),
           "session_id": j.get("session_id"),
           "transcript_path": tp,
           "cwd": j.get("cwd")}
    with open(QUEUE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open
    sys.exit(0)
