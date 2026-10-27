#!/usr/bin/env python3
"""Grounding and abstention contracts for the synchronous memory Dialectic."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mem_dialectic", ROOT / "hooks/mem_dialectic.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


CONTEXT = {
    "version": 5, "depth": "high", "token_budget": 4000, "estimated_tokens": 1200,
    "truncated": False, "scope": {"project": "sample-project"},
    "sections": [
        {"name": "global", "items": [
            {"id": 1, "type": "preference", "status": "active", "project": None,
             "reasoning_level": "explicit", "content": "prefers concise reports",
             "query_match": True},
        ]},
        {"name": "derived", "items": [
            {"id": 4, "type": "semantic", "status": "active", "project": "sample-project",
             "reasoning_level": "inductive", "content": "prefers audited rollout reports"},
        ]},
        {"name": "evidence", "items": [
            {"id": 5, "type": "episodic", "status": "superseded", "project": "sample-project",
             "reasoning_level": "explicit", "content": "asked for checksums",
             "evidence_for": [4], "evidence_depth": 1, "evidence_paths": [[4, 5]]},
        ]},
    ],
}


def main():
    valid = {"abstained": False, "claims": [
        {"text": "Alice prefers audited rollout reports.", "memory_ids": [4]},
        {"text": "Concise reporting is preferred.", "memory_ids": [1, 1]},
    ]}
    claims, reason = mod.validate_reply(valid, CONTEXT)
    assert reason is None and claims[1]["memory_ids"] == [1]
    result = mod.build_result("How should I report?", "high", CONTEXT, claims)
    assert result["version"] == 3 and result["answer"].endswith("[M1]")
    derived = next(x for x in result["citations"] if x["memory_id"] == 4)
    assert derived["content"] == "prefers audited rollout reports"
    assert derived["premises"] == [{"memory_id": 5, "status": "superseded",
                                    "reasoning_level": "explicit", "depth": 1,
                                    "paths": [[4, 5]]}]
    assert result["context"]["selected_ids"] == [1, 4, 5]
    assert "How should I report?" not in json.dumps(result)

    for bad in [
        {"abstained": False, "claims": [{"text": "fabricated", "memory_ids": [999]}]},
        {"abstained": False, "claims": [{"text": "unsupported", "memory_ids": []}]},
        {"abstained": False, "claims": [{"text": "stale", "memory_ids": [5]}]},
        {"abstained": False, "claims": [{"text": "forged [M999]", "memory_ids": [1]}]},
        {"abstained": True, "claims": [{"text": "hidden answer", "memory_ids": [1]}]},
    ]:
        try:
            mod.validate_reply(bad, CONTEXT)
            raise AssertionError("unsupported model output was accepted")
        except ValueError:
            pass

    original = mod.mem_ops
    seen = {}
    try:
        def fake_mem_ops(cmd, payload, timeout=0):
            seen.update(cmd=cmd, payload=payload, timeout=timeout)
            return json.dumps(CONTEXT)
        mod.mem_ops = fake_mem_ops
        live = mod.answer({"query": "How should I report?", "project": "sample-project",
                           "depth": "auto", "routing": "global", "token_budget": 4000},
                          generator=lambda query, context: valid)
    finally:
        mod.mem_ops = original
    assert seen["cmd"] == "context" and seen["payload"]["project"] == "sample-project"
    assert seen["payload"]["depth"] == "auto" and seen["payload"]["routing"] == "global"
    assert live["abstained"] is False

    # The identity the MCP server resolved is forwarded, not re-resolved here.
    try:
        mod.mem_ops = fake_mem_ops
        mod.answer({"query": "How should I report?", "_identity": "alice"},
                   generator=lambda query, context: valid)
        assert seen["payload"]["_identity"] == "alice", seen["payload"]
        mod.answer({"query": "How should I report?"}, generator=lambda query, context: valid)
        assert "_identity" not in seen["payload"], seen["payload"]   # mem_ops() resolves it
        try:
            mod.answer({"query": "x", "_identity": "alice; DROP"}, generator=lambda *_: valid)
            raise AssertionError("a malformed identity was accepted")
        except ValueError:
            pass
    finally:
        mod.mem_ops = original

    # R6: abstention is decided in code when nothing matched the query. Profile rows alone are
    # not an answer, and the model is never asked.
    profile_only = json.loads(json.dumps(CONTEXT))
    for section in profile_only["sections"]:
        for entry in section["items"]:
            entry.pop("query_match", None)
    asked = []
    try:
        mod.mem_ops = lambda cmd, payload, timeout=0: json.dumps(profile_only)
        abstained = mod.answer({"query": "Unrelated question?"},
                               generator=lambda q, c: asked.append(q) or valid)
    finally:
        mod.mem_ops = original
    assert abstained["abstained"] is True and not asked, abstained
    assert "matched the query" in abstained["reason"]

    # An older store's context (version 4, no query_match at all) is an error, not an
    # abstention: read as "nothing matched", every question would look like an empty memory.
    old_store = json.loads(json.dumps(profile_only))
    old_store["version"] = 4
    try:
        mod.mem_ops = lambda cmd, payload, timeout=0: json.dumps(old_store)
        try:
            mod.answer({"query": "Anything?"}, generator=lambda q, c: asked.append(q) or valid)
            raise AssertionError("a version-4 context was answered")
        except RuntimeError as exc:
            assert "older than this client" in str(exc), exc
    finally:
        mod.mem_ops = original
    assert not asked

    # R4: a conclusion the context reports as stale cannot be cited, even if an item slipped in.
    stale_ctx = json.loads(json.dumps(CONTEXT))
    stale_ctx["scope"]["stale_premises"] = [4]
    try:
        mod.validate_reply({"abstained": False, "claims": [
            {"text": "Alice prefers audited rollout reports.", "memory_ids": [4]}]}, stale_ctx)
        raise AssertionError("a stale conclusion was citable")
    except ValueError:
        pass

    invalid = mod.answer
    for payload in ({"query": ""}, {"query": "x", "depth": "infinite"},
                    {"query": "x", "routing": "unbounded"},
                    {"query": "x", "token_budget": 1}):
        try:
            invalid(payload, generator=lambda *_: valid)
            raise AssertionError("invalid request was accepted")
        except ValueError:
            pass

    # The whole run is bounded by MEM_DIALECTIC_TIMEOUT: the context call and the model call
    # share one deadline, and neither is handed more than what is left of it.
    budget = {}
    original_complete = mod.complete
    try:
        mod.mem_ops = lambda cmd, payload, timeout=0: (
            budget.update(context=timeout) or json.dumps(CONTEXT))
        mod.complete = lambda prompt, text, timeout=0: (
            budget.update(model=timeout) or json.dumps(valid))
        mod.answer({"query": "How should I report?"})
    finally:
        mod.mem_ops, mod.complete = original, original_complete
    assert 0 < budget["context"] <= min(mod.CONTEXT_TIMEOUT, mod.TOTAL_TIMEOUT), budget
    assert 0 < budget["model"] <= mod.TOTAL_TIMEOUT, budget
    assert mod.TOTAL_TIMEOUT == 300 or os.environ.get("MEM_DIALECTIC_TIMEOUT")
    # Same parse and clamp as the MCP server's dialectic_kill_secs (default 300 -> 420 s there).
    for raw, want in (("45", 45), ("9999", 600), ("1", 30), ("junk", 300)):
        os.environ["MEM_DIALECTIC_TIMEOUT"] = raw
        assert mod._total_timeout() == want, (raw, mod._total_timeout())
    os.environ.pop("MEM_DIALECTIC_TIMEOUT", None)
    rust = (ROOT / "mcp-server/src/main.rs").read_text()
    assert "const DIALECTIC_MARGIN_SECS: u64 = 120;" in rust
    assert "dialectic_kill_secs()," in rust

    # A CLI backend is killed as a process GROUP: its own helpers must not outlive the timeout.
    sys.path.insert(0, str(ROOT / "hooks"))
    import _llm
    with tempfile.TemporaryDirectory() as td:
        pidfile = Path(td) / "grandchild.pid"
        try:
            _llm._cli(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], "", 1)
            raise AssertionError("a hung CLI backend was not timed out")
        except subprocess.TimeoutExpired:
            pass
        grandchild = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the backend's child survived the timeout")

    print(json.dumps({"ok": True, "citations": len(result["citations"]),
                      "context_sha256": result["context"]["sha256"]}))


if __name__ == "__main__":
    main()
