#!/usr/bin/env python3
"""Personal-memory quality probes -- the LongMemEval failure modes, checked mechanically against
the LIVE serving path (steal #5, inspired by Hindsight's use of LongMemEval). Pass/fail,
self-cleaning. This is the memory-tier counterpart to the doc-RAG recall@k eval: instead of a
score, it asserts the properties a memory store must have or it's silently wrong.

  1. staleness   -- a superseded fact must NOT surface in default search; the new one must, and
                    the old one must still be reachable with include_inactive.
  2. abstention  -- an unknown-topic query must return NOTHING, not top-k noise.
  3. temporal    -- a fact whose valid_to is in the past is hidden by default, visible with
                    include_inactive.
  4. recall      -- a paraphrased query finds a just-written distinctive fact in the top-3.

Runs against whatever DATABASE_URL + embedder (EMBED_BACKEND) point at -- run it after a change
to the memory path. Test rows are tagged metadata.probe=true and hard-deleted afterwards.

Usage: python3 eval/mem_probes.py     (exit 1 if any probe fails)
Env: DATABASE_URL, EMBED_BACKEND (+ OLLAMA_URL/TEI_URL), HM_PYTHON, HM_MEM_OPS.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get("HM_PYTHON", "python3")
MEM_OPS = os.environ.get("HM_MEM_OPS", os.path.join(HERE, "..", "ingest", "mem_ops.py"))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://hm@localhost:5432/hypermnesia")

results = []

# The probes exercise the live path, so they have to BE somebody: writes are refused without an
# author and reads are scoped to one. Their own identity rather than a real person's, so a probe
# row that failed to clean up cannot end up in anyone's profile.
PROBE_AUTHOR = os.environ.get("MEM_PROBE_AUTHOR", "probe")
if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", PROBE_AUTHOR):
    sys.exit(f"MEM_PROBE_AUTHOR {PROBE_AUTHOR!r} is not a plain name ([A-Za-z0-9._-], up to 64)")
# A second identity, so isolation can be probed rather than assumed.
OTHER_AUTHOR = PROBE_AUTHOR + "-other"


def mem(cmd, payload, timeout=60, who=None):
    payload = dict(payload)
    payload.setdefault("_identity", who or PROBE_AUTHOR)
    p = subprocess.run([PY, MEM_OPS, cmd], input=json.dumps(payload, ensure_ascii=False).encode(),
                       capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"mem_ops {cmd} failed: {p.stderr.decode()[-400:]}")
    return p.stdout.decode().strip()


def mem_may_fail(cmd, payload, who=None):
    """Like `mem`, but a refusal is an ANSWER here rather than a crash."""
    try:
        return mem(cmd, payload, who=who)
    except RuntimeError as e:
        return f"REFUSED: {e}"


def psql(sql, timeout=30):
    subprocess.run(["psql", DATABASE_URL, "-tAX", "-v", "ON_ERROR_STOP=1"], input=sql.encode(),
                   capture_output=True, timeout=timeout, check=True)


def mem_id(out):
    return int(out.split("[#")[1].split("]")[0])


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


def main():
    ids = []
    meta = {"probe": True}
    try:
        print("== probe: staleness (supersede leak) ==")
        a = mem_id(mem("write", {"type": "semantic", "importance": 0.3, "metadata": meta,
                                 "content": "PROBE-STALE: the Probe-Cloud hosting plan is 1000 credits per month."}))
        ids.append(a)
        b = mem_id(mem("supersede", {"old_id": a, "metadata": meta,
                                     "content": "PROBE-STALE: the Probe-Cloud hosting plan is 2000 credits per month (raised)."}))
        ids.append(b)
        out = mem("search", {"query": "what is the Probe-Cloud plan price", "k": 5})
        check("new fact surfaces", f"[#{b}]" in out)
        check("superseded fact hidden", f"[#{a}]" not in out)
        hist = mem("search", {"query": "what is the Probe-Cloud plan price", "k": 5, "include_inactive": True})
        check("history visible on demand", f"[#{a}]" in hist and "[superseded]" in hist)

        print("== probe: abstention (unknown topic) ==")
        # a topic wholly orthogonal to a coding-agent's store; near-topic queries can still slip
        # under a loose distance gate (see MEM_SEM_MAXDIST tuning), which this probe is not testing.
        out = mem("search", {"query": "a recipe for sourdough bread with rye flour at high altitude", "k": 5})
        check("unknown topic returns nothing", out == "(no memories found)", out[:200])

        print("== probe: temporal (expired validity window) ==")
        c = mem_id(mem("write", {"type": "semantic", "importance": 0.3, "metadata": meta,
                                 "valid_from": "2026-01-01", "valid_to": "2026-03-01",
                                 "content": "PROBE-TEMP: in Feb 2026 the Probe host used a temporary Probe-CA certificate."}))
        ids.append(c)
        out = mem("search", {"query": "temporary Probe-CA certificate", "k": 5})
        check("expired fact hidden by default", f"[#{c}]" not in out)
        hist = mem("search", {"query": "temporary Probe-CA certificate", "k": 5, "include_inactive": True})
        check("expired fact in history", f"[#{c}]" in hist)

        print("== probe: recall (paraphrase) ==")
        d = mem_id(mem("write", {"type": "preference", "importance": 0.4, "metadata": meta,
                                 "content": "PROBE-RECALL: the owner asks that the test cluster Quasar-9 be referred to only by its codename."}))
        ids.append(d)
        out = mem("search", {"query": "how should I address the experimental cluster", "k": 3})
        check("paraphrase finds fact top-3", f"[#{d}]" in out, out[:200])
        # 5. isolation -- the probe that fails if the multi-user rule is taken off.
        #
        # Every probe above passes with the rule deleted entirely, because they write and read
        # as ONE identity: they cannot tell a store that isolates from one that does not. This
        # writes as one author and reads as a second, which is the only arrangement that can.
        print("== probe: isolation (a second author) ==")
        e = mem_id(mem("write", {"type": "preference", "importance": 0.4, "metadata": meta,
                                 "content": "PROBE-PRIVATE: drinks tea only from the blue mug."}))
        ids.append(e)
        q = "which mug is the tea drunk from"
        check("the author finds their own private fact", f"[#{e}]" in mem("search", {"query": q, "k": 5}))
        check("a second author does not",
              f"[#{e}]" not in mem("search", {"query": q, "k": 5}, who=OTHER_AUTHOR))
        check("nor through history",
              f"[#{e}]" not in mem("search", {"query": q, "k": 5, "include_inactive": True},
                                   who=OTHER_AUTHOR))
        check("nor by id", f"[#{e}]" not in mem_may_fail("get", {"id": e}, who=OTHER_AUTHOR))
        # Checked by what did NOT happen, not by the wording: with row-level security on, the
        # row is invisible to the other author, so `mark` honestly answers "not found" rather
        # than refusing by name -- the same answer `get` gives, and for the same reason.
        out = mem_may_fail("mark", {"id": e, "status": "retracted"}, who=OTHER_AUTHOR)
        check("and cannot be retracted by them", "marked" not in out, out[:120])
        out = mem_may_fail("supersede", {"old_id": e, "content": "replaced"}, who=OTHER_AUTHOR)
        check("nor superseded by them", "saved" not in out, out[:120])
        check("the fact survived both attempts", f"[#{e}]" in mem("search", {"query": q, "k": 5}))
    finally:
        if ids:
            psql(f"DELETE FROM mem.sources WHERE memory_id IN ({','.join(map(str, ids))});")
            psql(f"DELETE FROM mem.memories WHERE id IN ({','.join(map(str, ids))});")
            print(f"(cleaned {len(ids)} probe rows)")

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} probes passed"
          + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
