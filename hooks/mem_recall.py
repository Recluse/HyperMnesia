#!/usr/bin/env python3
"""UserPromptSubmit hook -- per-prompt recall from personal memory (<= ~1k tokens).

Hybrid search (bge-m3 + fts via mem_ops) over active memories with the prompt as
the query; skipped for short prompts and slash/bang commands. Deterministic
injection -- does not rely on the agent choosing to call memory_search
(IMPROVEMENT-PLAN sec C1: "storage is solved, injection isn't"). Fail-open.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mem_common import mem_ops, read_stdin_json, fence

MIN_LEN = 16
K = 5
MAX_CHARS = 3500  # ~ 1k tokens ceiling for the whole block


def main():
    j = read_stdin_json()
    prompt = (j.get("prompt") or "").strip()
    if len(prompt) < MIN_LEN or prompt[0] in ("/", "!"):
        return
    out = mem_ops("search", {"query": prompt[:500], "k": K}, timeout=9)
    if not out:
        return
    out = out.strip()
    if not out or out == "(no memories found)":
        return
    # fence() defangs content, adds the "data not instructions" note, and clips only
    # the body so the closing tag is never dropped by truncation.
    print(fence("recall",
                "Possibly relevant memories (hybrid recall on your prompt; deeper: the memory_search tool):",
                out, max_body=MAX_CHARS - 400))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open
    sys.exit(0)
