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
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get("HM_PYTHON", "python3")
MEM_OPS = os.environ.get("HM_MEM_OPS", os.path.join(HERE, "..", "ingest", "mem_ops.py"))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://hm@localhost:5432/hypermnesia")

results = []


def mem(cmd, payload, timeout=60):
    p = subprocess.run([PY, MEM_OPS, cmd], input=json.dumps(payload, ensure_ascii=False).encode(),
                       capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"mem_ops {cmd} failed: {p.stderr.decode()[-400:]}")
    return p.stdout.decode().strip()


def psql(sql, timeout=30):
    subprocess.run(["psql", DATABASE_URL, "-tAX"], input=sql.encode(),
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
