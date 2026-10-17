#!/usr/bin/env python3
"""Grounding and abstention contracts for the synchronous memory Dialectic."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mem_dialectic", ROOT / "hooks/mem_dialectic.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


CONTEXT = {
    "version": 2, "depth": "high", "token_budget": 4000, "estimated_tokens": 1200,
    "truncated": False, "scope": {"project": "sample-project"},
    "sections": [
        {"name": "global", "items": [
            {"id": 1, "type": "preference", "status": "active", "project": None,
             "reasoning_level": "explicit", "content": "prefers concise reports"},
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

    invalid = mod.answer
    for payload in ({"query": ""}, {"query": "x", "depth": "infinite"},
                    {"query": "x", "routing": "unbounded"},
                    {"query": "x", "token_budget": 1}):
        try:
            invalid(payload, generator=lambda *_: valid)
            raise AssertionError("invalid request was accepted")
        except ValueError:
            pass

    print(json.dumps({"ok": True, "citations": len(result["citations"]),
                      "context_sha256": result["context"]["sha256"]}))


if __name__ == "__main__":
    main()
