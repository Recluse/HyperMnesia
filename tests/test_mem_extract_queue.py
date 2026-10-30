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
import _isolate  # noqa: F401  (first: a throwaway HOME and HYPERMNESIA_HOME)
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"))
import mem_extract  # noqa: E402
from mem_extract import transcript_text  # noqa: E402

failures = []
ran = 0


def check(name, cond, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not cond
                                                     else ""))
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

    print("\n== injected memory never reaches the extractor ==")
    from _mem_common import fence
    strip = mem_extract.strip_memory_blocks
    with tempfile.TemporaryDirectory() as tmp:
        fenced = os.path.join(tmp, "fenced.jsonl")
        recalled = fence("recall", "Possibly relevant memories:", "[#7] (preference) prefers zed")
        with open(fenced, "w", encoding="utf-8") as f:
            f.write(json.dumps({"message": {"role": "user",
                                            "content": recalled + " please rebuild the index"}})
                    + "\n")
            # A fence cut off by the end of its turn: removed to the end of THAT turn only.
            f.write(json.dumps({"message": {"role": "assistant", "content": [
                {"type": "text", "text": "ok <personal-memory-profile nonce=c0ffee>pinned"}]}})
                    + "\n")
            f.write(json.dumps({"message": {"role": "user", "content": "next turn survives"}})
                    + "\n")
        text = transcript_text(fenced)
        check("a real fence() block is removed before extraction",
              "prefers zed" not in text and "personal-memory" not in text, text)
        check("a truncated fence is removed to the end of its turn, not beyond",
              "pinned" not in text and "ok" in text and "next turn survives" in text, text)
        check("the surrounding conversation survives", "please rebuild the index" in text, text)
    nested = ("<personal-memory-recall nonce=aa11><personal-memory-x nonce=bb22>inner"
              "</personal-memory-x nonce=bb22>SECRET</personal-memory-recall nonce=aa11> after")
    check("a nested block goes as a whole: nothing of the outer one is left behind",
          strip(nested).split() == ["after"], strip(nested))
    bare_nested = ("<personal-memory-recall><personal-memory-x>inner</personal-memory-x>"
                   "SECRET</personal-memory-recall>")
    check("tags without a nonce are not our fence and are left as written",
          strip(bare_nested) == bare_nested, strip(bare_nested))
    mention = "Explain the literal tag <personal-memory-recall> and preserve this normal request."
    check("mentioning the tag does not eat the rest of the message",
          strip(mention) == mention, strip(mention))
    reused = ("<personal-memory-recall nonce=aa11><personal-memory-recall nonce=aa11>x"
              "</personal-memory-recall nonce=aa11>LEAK</personal-memory-recall nonce=aa11> tail")
    check("nested fences reusing one kind and nonce go whole: nothing between closes leaks",
          strip(reused).split() == ["tail"], strip(reused))
    import time
    # Linear, judged by a budget so far from both outcomes that load cannot blur them. The old
    # check gave 20,000 units 0.5 s, which a busy machine missed while the scan was linear. A
    # ratio of two sizes turned out no better: the small size's timing alone varied 6x between
    # runs here. 80,000 units (about 3 MB) take well under 2 s when the scan is linear even
    # under load, and a quadratic one copies the text once per tag -- hundreds of GB, minutes.
    for name, unit in (("unclosed openings", "<personal-memory-recall nonce=abcd> "),
                       ("unpaired closes", "x </personal-memory-recall nonce=abcd> "),
                       ("open/close pairs", "<personal-memory-x nonce=ab12>y"
                                            "</personal-memory-x nonce=ab12> ")):
        started = time.perf_counter()
        strip(unit * 80000)
        took = time.perf_counter() - started
        check(f"linear on 80,000 {name} (under 10 s)", took < 10, f"{took:.2f} s")
    other_close = ("<personal-memory-recall nonce=aa11>a</personal-memory-recall nonce=bb22>"
                   " tail")
    check("a close with another nonce does not end the block (treated as truncated)",
          strip(other_close).strip() == "", strip(other_close))

    print("\n== a failed duplicate check is a failure, not 'not a duplicate' ==")
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "t.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "x" * 800}}) + "\n")
        queue, done = os.path.join(tmp, "q.jsonl"), os.path.join(tmp, "done")
        with open(queue, "w", encoding="utf-8") as f:
            f.write(json.dumps({"transcript_path": tp}) + "\n")

        def run(search_answer, write_answer='{"id": 5}', author="alice"):
            calls = []

            def fake_mem_ops(cmd, payload, timeout=10):
                calls.append((cmd, payload))
                if cmd == "search":
                    return search_answer
                if cmd == "nearest":
                    return "{}"
                return write_answer
            saved = {k: getattr(mem_extract, k) for k in
                     ("mem_ops", "QUEUE", "DONE", "LOCK", "extract", "identity",
                      "STATE_DIR", "LEGACY_DIR", "legacy_twin")}
            mem_extract.mem_ops = fake_mem_ops
            mem_extract.QUEUE, mem_extract.DONE = queue, done
            mem_extract.LOCK = os.path.join(tmp, "lock")
            # Every path the run can reach, not only the three it is about: the old-queue
            # retirement reads and deletes in LEGACY_DIR once STATE_DIR exists.
            mem_extract.STATE_DIR = os.path.join(tmp, "state")
            mem_extract.LEGACY_DIR = os.path.join(tmp, "legacy")
            mem_extract.legacy_twin = lambda name: None
            mem_extract.extract = lambda text: [{"type": "semantic", "content": "a new fact"}]
            mem_extract.identity = lambda: author
            argv, sys.argv = sys.argv, ["mem_extract.py"]
            try:
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    code = mem_extract.main()
            finally:
                sys.argv = argv
                for k, v in saved.items():
                    setattr(mem_extract, k, v)
            return code, calls

        code, calls = run(None)
        check("a search error skips the item and exits non-zero",
              code == 1 and not any(c == "write" for c, _ in calls), str((code, calls)))
        check("and the transcript is NOT marked done", not os.path.exists(done))
        probe = dict(calls)["search"]
        check("the probe does not count as recall, and asks for JSON",
              probe.get("track_access") is False and probe.get("format") == "json", str(probe))
        code, calls = run("[]", write_answer=None)
        check("a failed write exits non-zero", code == 1 and not os.path.exists(done))
        code, calls = run("[]")
        check("a clean run exits 0 and marks the transcript done",
              code == 0 and os.path.exists(done), str(code))
        check("the write asked for JSON", dict(calls)["write"].get("format") == "json")
        code, calls = run("[]", author=None)
        check("no MEM_AUTHOR: refuses to start (exit 2), touches nothing",
              code == 2 and not calls, str((code, calls)))

    print("\n== one bad input costs only itself (issue: a malformed item jammed the queue) ==")
    with tempfile.TemporaryDirectory() as tmp:
        tp = os.path.join(tmp, "bad.jsonl")
        with open(tp, "wb") as f:
            f.write(b'[1, 2]\n"a string"\n{"message": "not a dict"}\n{"message": {"role": "user", "content": 7}}\n')
            f.write(json.dumps({"message": {"role": "user", "content": "kept " * 10}}).encode() + b"\n")
            f.write(b'{"message": {"role": "user", "content": "cut mid-char \xd0"}}\n')
        text = transcript_text(tp)
        check("a transcript with non-object lines and a broken UTF-8 byte still reads",
              text is not None and "kept" in text and "cut mid-char" in text, repr(text))
    check("a score that is not a number falls back to the default",
          mem_extract._score(None, 0.5) == 0.5 and mem_extract._score("high", 0.7) == 0.7
          and mem_extract._score(float("nan"), 0.5) == 0.5 and mem_extract._score("0.9", 0.5) == 0.9)
    answer = json.dumps([
        {"type": "semantic", "content": ["a", "list"]}, {"type": "semantic", "content": 42},
        {"type": "semantic", "content": "   "}, {"type": "semantic", "content": "a real fact"}])

    saved_complete = mem_extract.complete
    mem_extract.complete = lambda prompt, text: answer
    try:
        got = mem_extract.extract("x")
    finally:
        mem_extract.complete = saved_complete
    check("only items whose content is text are kept",
          [i["content"] for i in got] == ["a real fact"], str(got))

    def dup(cand, existing):
        saved_ops = mem_extract.mem_ops
        mem_extract.mem_ops = lambda cmd, payload, timeout=10: (
            json.dumps([{"content": existing}]) if cmd == "search" else "{}")
        try:
            return mem_extract.is_duplicate(cand)
        finally:
            mem_extract.mem_ops = saved_ops
    check("a correction quoting the old memory is not a duplicate of it",
          not dup("never uses docker for local dev on macos", "uses docker for local dev"))
    check("a refinement with new facts is not a duplicate",
          not dup("prefers zed for rust, cursor for typescript", "prefers zed"))
    check("a fragment of what is held still is one",
          dup("uses docker for local dev", "Uses  docker for local dev on every machine"))
    check("a fragment that flips polarity against its sentence is not",
          not dup("uses docker for local dev", "never uses docker for local dev"))
    check("a number inside a longer number is not a match (whole words only)",
          not dup("monthly budget is 100", "monthly\tbudget is 1000"))
    check("spacing and a closing full stop do not hide a real fragment",
          dup("Budget is 1000.", "The monthly budget  is 1000 per team"))
    for cand, held in (("prefers C# for backend development", "prefers C++ for backend development"),
                       ("target temperature is -20 C", "target temperature is +20 C"),
                       ("deploys to api-prod", "deploys to api.prod"),
                       ("request timeout is .5 seconds", "request timeout is 5 seconds"),
                       ("loads settings from .env on startup", "loads settings from env on startup"),
                       ("uses glob *.log? for log cleanup.", "uses glob *.log for log cleanup."),
                       ("the accepted score range is [0,1).", "the accepted score range is [0,1].")):
        check(f"a symbol is part of the word: {cand!r} is not {held!r}", not dup(cand, held))

    with tempfile.TemporaryDirectory() as tmp:
        tps = []
        for n in ("first", "second"):
            tp = os.path.join(tmp, f"{n}.jsonl")
            with open(tp, "w", encoding="utf-8") as f:
                f.write(json.dumps({"message": {"role": "user", "content": n * 200}}) + "\n")
            tps.append(tp)
        queue, done = os.path.join(tmp, "q.jsonl"), os.path.join(tmp, "done")
        with open(queue, "w", encoding="utf-8") as f:
            for tp in tps:
                f.write(json.dumps({"transcript_path": tp}) + "\n")
        writes = []

        def fake_ops(cmd, payload, timeout=10):
            if cmd == "write":
                writes.append(payload)
                return '{"id": 9}'
            return "[]" if cmd == "search" else "{}"
        real_text = mem_extract.transcript_text

        def flaky_text(path):
            if path.endswith("first.jsonl"):
                raise LookupError("something nobody anticipated")
            return real_text(path)
        saved = {k: getattr(mem_extract, k) for k in
                 ("mem_ops", "QUEUE", "DONE", "LOCK", "extract", "identity", "transcript_text",
                  "legacy_twin", "STATE_DIR", "LEGACY_DIR")}
        mem_extract.legacy_twin = lambda name: None
        mem_extract.STATE_DIR = os.path.join(tmp, "no-state")
        mem_extract.LEGACY_DIR = os.path.join(tmp, "no-legacy")
        mem_extract.mem_ops, mem_extract.QUEUE, mem_extract.DONE = fake_ops, queue, done
        mem_extract.LOCK = os.path.join(tmp, "lock")
        mem_extract.transcript_text = flaky_text
        mem_extract.extract = lambda text: [{"type": "semantic", "content": "a fact",
                                             "importance": None, "confidence": "high"}]
        mem_extract.identity = lambda: "alice"
        argv, sys.argv = sys.argv, ["mem_extract.py"]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = mem_extract.main()
        finally:
            sys.argv = argv
            for k, v in saved.items():
                setattr(mem_extract, k, v)
        marked = open(done, encoding="utf-8").read() if os.path.exists(done) else ""
        check("an unexpected error in one transcript does not stop the next one",
              code == 1 and len(writes) == 1 and "second" in marked and "first" not in marked,
              str((code, len(writes), marked)))
        check("a null or wordy score is written as the default, not a crash",
              writes and writes[0]["importance"] == 0.5 and writes[0]["confidence"] == 0.7,
              str(writes))

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
