#!/usr/bin/env python3
"""A queued transcript must leave the queue, one way or the other.

The extractor keeps a queue of transcripts and a `done` list, and processes only the first
`--limit` (20) entries per run. Anything that can stay pending forever therefore does not just
delay itself -- it occupies a slot, and once twenty such entries collect at the front, nothing
else is ever reached. That happened: a live queue held 59 entries, 56 of them
transcripts deleted weeks earlier, and the three real ones sat at positions 45, 52 and 59.
Extraction had stopped for weeks while the log printed "will retry" and read like activity.

Two states could produce it, and both are here:
  * the file is GONE -- the caller checks os.path.exists() and marks it done;
  * the file reads fine and contains no user/assistant text -- `transcript_text` must return ""
    (falls into the "tiny -> done" branch) and NOT None, which means "could not read, retry".

Only a genuine read failure may return None, because only that one can succeed later.

    python3 tests/test_mem_extract_queue.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"))
from mem_extract import transcript_text  # noqa: E402

failures = []
ran = 0


def check(name, cond):
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


# A real session line, and the bookkeeping-only lines a transcript can consist entirely of.
REAL = '{"message": {"role": "user", "content": "how do I rebuild the index"}}\n'
NOISE = ('{"type": "summary", "summary": "compacted"}\n'
         '{"message": {"role": "system", "content": "ignored"}}\n'
         '{"message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]}}\n')


def main():
    with tempfile.TemporaryDirectory() as tmp:
        empty = os.path.join(tmp, "empty.jsonl")
        with open(empty, "w", encoding="utf-8") as f:
            f.write("")
        noise = os.path.join(tmp, "noise.jsonl")
        with open(noise, "w", encoding="utf-8") as f:
            f.write(NOISE)
        real = os.path.join(tmp, "real.jsonl")
        with open(real, "w", encoding="utf-8") as f:
            f.write(REAL)

        # The distinction the queue depends on. "" routes to done; None routes to retry.
        check("an empty transcript reads as empty, not as unreadable",
              transcript_text(empty) == "")
        check("a transcript with only tool calls and summaries reads as empty too",
              transcript_text(noise) == "")
        check("a real transcript still comes back with its text",
              "how do I rebuild the index" in (transcript_text(real) or ""))

        # None is reserved for "try again later", and both of these can succeed on a later run
        # only in the second case -- which is why the CALLER checks os.path.exists() first.
        check("a missing file is unreadable", transcript_text(os.path.join(tmp, "nope.jsonl")) is None)

        if os.geteuid() != 0:          # root reads anything; the check would be vacuous
            locked = os.path.join(tmp, "locked.jsonl")
            with open(locked, "w", encoding="utf-8") as f:
                f.write(REAL)
            os.chmod(locked, 0o000)
            try:
                check("an unreadable file is unreadable", transcript_text(locked) is None)
            finally:
                os.chmod(locked, 0o600)

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
