#!/usr/bin/env python3
"""Contract checks for the automatic-depth evaluation gate."""
import _isolate  # noqa: F401  (first: a throwaway HOME and HYPERMNESIA_HOME)
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "auto_depth_eval", ROOT / "eval/auto_depth_eval.py")
auto_depth_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto_depth_eval)


def context(request):
    query = request["query"]
    requested = request["depth"]
    selected = ("minimal" if "unknown" in query else
                "low" if requested == "auto" else requested)
    if requested != "auto":
        selected = requested
    reason = ("no_relevant_hits" if "unknown" in query else
              "explicit_support_only") if requested == "auto" else "caller_selected"
    ids = [] if "unknown" in query else [101]
    if selected != "minimal" and ids:
        ids.append(42)
    result = {
        "version": 5, "depth": selected, "token_budget": request["token_budget"],
        "estimated_tokens": 500 + len(ids) * 100, "truncated": False,
        "scope": {"requested_depth": requested,
                  "depth_selection": {"selected": selected, "reason": reason}},
        "sections": ([{"name": "relevant", "items": [
            {"id": mid, "content": "x" * 100} for mid in ids]}]
                     if ids else []),
    }
    return result


def main():
    raw_query = "why does the long operation wait"
    report = auto_depth_eval.evaluate({
        "version": 1,
        "probes": [{
            "name": "supported",
            "query": raw_query,
            "routing": "global",
            "expected": {"memory_ids": [101], "auto_depths": ["low"],
                         "auto_reasons": ["explicit_support_only"]},
            "gates": {"match_selected_fixed": True,
                      "max_extra_ids": 1, "max_extra_tokens": 100},
        }, {
            "name": "abstention",
            "query": "unknown orthogonal subject",
            "expected": {"abstain": True, "auto_depths": ["minimal"],
                         "auto_reasons": ["no_relevant_hits"]},
        }],
    }, context, repeats=2)
    assert report["passed"] is True, report
    assert report["mode"] == "read-only-context-no-model"
    supported = report["probes"][0]
    assert supported["expected"]["memory_ids"] == [101]
    assert supported["gates"]["max_extra_tokens"] == 100
    assert supported["oracle_depth"] == "minimal"
    assert supported["candidates"]["auto"]["citation_yield"] == 1.0
    assert supported["candidates"]["auto"]["extra_ids_vs_oracle"] == [42]
    assert supported["candidates"]["auto"]["extra_tokens_vs_oracle"] == 100
    abstention = report["probes"][1]
    assert abstention["candidates"]["auto"]["selected_count"] == 0
    rendered = json.dumps(report)
    assert raw_query not in rendered
    assert len(supported["query_sha256"]) == 64

    committed = json.loads((ROOT / "eval/auto_depth_probes.json").read_text())
    assert committed["version"] == 1 and len(committed["probes"]) == 3
    assert all(probe.get("expected") for probe in committed["probes"])

    failed = auto_depth_eval.evaluate({
        "version": 1,
        "probes": [{
            "name": "over-selection",
            "query": raw_query,
            "expected": {"memory_ids": [101]},
            "gates": {"max_extra_ids": 0},
        }],
    }, context)
    assert failed["passed"] is False
    assert failed["probes"][0]["errors"] == [
        "auto: unnecessary context id gate exceeded"]
    print(json.dumps({"ok": True, "probes": report["probe_count"]}))


if __name__ == "__main__":
    main()
