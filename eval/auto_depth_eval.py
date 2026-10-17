#!/usr/bin/env python3
"""Read-only evaluation gate for automatic memory-context depth.

Each probe is sent unchanged to ``depth=auto`` and to every fixed depth.  The
report contains hashes and aggregate selection metadata, never query text or
memory content.  The evaluator invokes only the read-only ``context`` operation;
it neither calls a model nor writes memory.

Usage:
    python3 eval/auto_depth_eval.py --probes /path/to/probes.json \
        --output eval/results/auto-depth.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DEPTHS = ("minimal", "low", "medium", "high", "max")
ALL_DEPTHS = ("auto",) + DEPTHS


def _query_hash(query):
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _ids(context):
    return [item["id"] for section in context.get("sections", [])
            for item in section.get("items", [])]


def _content_tokens(context):
    """Comparable memory payload size, excluding request-specific audit metadata."""
    return sum(len(str(item.get("content", "")).encode("utf-8"))
               for section in context.get("sections", [])
               for item in section.get("items", []))


def _summary(context, latencies_ms, expected_ids):
    selected_ids = _ids(context)
    selected = set(selected_ids)
    expected = set(expected_ids)
    hits = sorted(expected & selected)
    return {
        "actual_depth": context.get("depth"),
        "selection_reason": (context.get("scope", {}).get("depth_selection", {})
                             .get("reason")),
        "latency_ms": round(statistics.median(latencies_ms), 2),
        "selected_ids": selected_ids,
        "selected_count": len(selected_ids),
        "expected_hits": hits,
        "citation_yield": (round(len(hits) / len(expected), 4) if expected else None),
        "estimated_tokens": context.get("estimated_tokens"),
        "content_tokens": _content_tokens(context),
        "token_budget": context.get("token_budget"),
        "truncated": bool(context.get("truncated")),
        "section_counts": {section["name"]: len(section.get("items", []))
                           for section in context.get("sections", [])},
    }


def _validate_probe(probe):
    allowed = {"name", "query", "project", "routing", "token_budget", "session_id",
               "expected", "gates"}
    unknown = set(probe) - allowed
    if unknown:
        raise ValueError(f"probe {probe.get('name')!r}: unknown keys {sorted(unknown)}")
    if not isinstance(probe.get("name"), str) or not probe["name"].strip():
        raise ValueError("every probe needs a non-empty name")
    if not isinstance(probe.get("query"), str) or not probe["query"].strip():
        raise ValueError(f"probe {probe['name']!r}: query must be a non-empty string")
    routing = probe.get("routing", "project")
    if routing not in ("project", "global"):
        raise ValueError(f"probe {probe['name']!r}: routing must be project or global")
    expected = probe.get("expected") or {}
    unknown_expected = set(expected) - {"memory_ids", "auto_depths", "auto_reasons", "abstain"}
    if unknown_expected:
        raise ValueError(
            f"probe {probe['name']!r}: unknown expected keys {sorted(unknown_expected)}")
    ids = expected.get("memory_ids", [])
    if not isinstance(ids, list) or any(not isinstance(mid, int) for mid in ids):
        raise ValueError(f"probe {probe['name']!r}: expected.memory_ids must be integers")
    for key in ("auto_depths", "auto_reasons"):
        if key in expected and not isinstance(expected[key], list):
            raise ValueError(f"probe {probe['name']!r}: expected.{key} must be a list")
    gates = probe.get("gates") or {}
    allowed_gates = {"match_selected_fixed", "max_extra_ids", "max_extra_tokens",
                     "max_latency_ratio"}
    unknown_gates = set(gates) - allowed_gates
    if unknown_gates:
        raise ValueError(f"probe {probe['name']!r}: unknown gates {sorted(unknown_gates)}")


def _run_one(runner, payload, repeats):
    contexts, latencies = [], []
    for _ in range(repeats):
        started = time.perf_counter()
        context = runner(payload)
        latencies.append((time.perf_counter() - started) * 1000)
        if not isinstance(context, dict):
            raise RuntimeError("context backend did not return a JSON object")
        contexts.append(context)
    signatures = [{
        "depth": c.get("depth"), "ids": _ids(c),
        "tokens": c.get("estimated_tokens"), "truncated": c.get("truncated"),
        "reason": c.get("scope", {}).get("depth_selection", {}).get("reason"),
    } for c in contexts]
    if any(signature != signatures[0] for signature in signatures[1:]):
        raise RuntimeError("the same depth returned non-deterministic context selections")
    return contexts[0], latencies


def evaluate_probe(probe, runner, repeats=1):
    _validate_probe(probe)
    expected = probe.get("expected") or {}
    expected_ids = expected.get("memory_ids", [])
    payload = {
        "query": probe["query"],
        "routing": probe.get("routing", "project"),
        "token_budget": int(probe.get("token_budget", 4000)),
    }
    if probe.get("project") is not None:
        payload["project"] = probe["project"]
    if probe.get("session_id") is not None:
        payload["session_id"] = probe["session_id"]

    candidates = {}
    errors = []
    for requested_depth in ALL_DEPTHS:
        try:
            context, latencies = _run_one(
                runner, {**payload, "depth": requested_depth}, repeats)
            candidates[requested_depth] = _summary(context, latencies, expected_ids)
            if context.get("scope", {}).get("requested_depth") != requested_depth:
                errors.append(f"{requested_depth}: requested_depth was not preserved")
            if context.get("estimated_tokens", 10**9) > context.get("token_budget", -1):
                errors.append(f"{requested_depth}: token budget exceeded")
        except Exception as exc:
            candidates[requested_depth] = {"error": str(exc)}
            errors.append(f"{requested_depth}: {exc}")

    valid_fixed = [(depth, candidates[depth]) for depth in DEPTHS
                   if "error" not in candidates[depth]]
    if expected_ids:
        best_yield = max((row["citation_yield"] for _, row in valid_fixed), default=0.0)
        eligible = [(depth, row) for depth, row in valid_fixed
                    if row["citation_yield"] == best_yield]
    elif expected.get("abstain") is True:
        best_yield = None
        smallest = min((row["selected_count"] for _, row in valid_fixed), default=0)
        eligible = [(depth, row) for depth, row in valid_fixed
                    if row["selected_count"] == smallest]
    else:
        # With no labelled support there is no quality signal from which to infer an oracle.
        # Use the cheapest policy level, rather than rewarding a wider level merely because
        # budget truncation happened to leave it with fewer items.
        best_yield = None
        eligible = [(depth, row) for depth, row in valid_fixed if depth == "minimal"]
    oracle_depth, oracle = min(
        eligible, key=lambda pair: (pair[1]["content_tokens"], DEPTHS.index(pair[0]))) \
        if eligible else (None, None)

    auto = candidates["auto"]
    gates = probe.get("gates") or {}
    if "error" not in auto:
        allowed_depths = expected.get("auto_depths")
        if allowed_depths and auto["actual_depth"] not in allowed_depths:
            errors.append(f"auto: depth {auto['actual_depth']!r} not in {allowed_depths}")
        allowed_reasons = expected.get("auto_reasons")
        if allowed_reasons and auto["selection_reason"] not in allowed_reasons:
            errors.append(f"auto: reason {auto['selection_reason']!r} not in {allowed_reasons}")
        missing = sorted(set(expected_ids) - set(auto["selected_ids"]))
        if missing:
            errors.append(f"auto: expected memory ids absent: {missing}")
        if expected.get("abstain") is True and auto["selected_count"] != 0:
            errors.append("auto: expected an empty context for abstention")
        if expected_ids and oracle and auto["citation_yield"] < best_yield:
            errors.append(
                f"auto: citation yield {auto['citation_yield']} below fixed-depth best {best_yield}")
        selected_fixed = candidates.get(auto["actual_depth"], {})
        if "error" not in selected_fixed:
            fixed_ids, auto_ids = set(selected_fixed["selected_ids"]), set(auto["selected_ids"])
            union = fixed_ids | auto_ids
            auto["selected_fixed_overlap"] = (
                round(len(fixed_ids & auto_ids) / len(union), 4) if union else 1.0)
            auto["lost_ids_vs_selected_fixed"] = sorted(fixed_ids - auto_ids)
            auto["added_ids_vs_selected_fixed"] = sorted(auto_ids - fixed_ids)
        if gates.get("match_selected_fixed", False) and "error" not in selected_fixed:
            if (auto["selected_ids"] != selected_fixed["selected_ids"] or
                    auto["content_tokens"] != selected_fixed["content_tokens"]):
                errors.append("auto: output differs from its selected fixed depth")
        if oracle:
            auto["oracle_depth"] = oracle_depth
            auto["extra_ids_vs_oracle"] = sorted(
                set(auto["selected_ids"]) - set(oracle["selected_ids"]))
            auto["extra_tokens_vs_oracle"] = (
                auto["content_tokens"] - oracle["content_tokens"])
            if ("max_extra_ids" in gates and
                    len(auto["extra_ids_vs_oracle"]) > int(gates["max_extra_ids"])):
                errors.append("auto: unnecessary context id gate exceeded")
            if ("max_extra_tokens" in gates and
                    auto["extra_tokens_vs_oracle"] > int(gates["max_extra_tokens"])):
                errors.append("auto: unnecessary context token gate exceeded")
            if "max_latency_ratio" in gates and oracle["latency_ms"] > 0:
                ratio = auto["latency_ms"] / oracle["latency_ms"]
                auto["latency_ratio_vs_oracle"] = round(ratio, 3)
                if ratio > float(gates["max_latency_ratio"]):
                    errors.append("auto: latency ratio gate exceeded")

    return {
        "name": probe["name"],
        "query_sha256": _query_hash(probe["query"]),
        "routing": payload["routing"],
        "project": payload.get("project"),
        "token_budget": payload["token_budget"],
        "expected": expected,
        "gates": gates,
        "oracle_depth": oracle_depth,
        "best_fixed_citation_yield": best_yield,
        "candidates": candidates,
        "passed": not errors,
        "errors": errors,
    }


def evaluate(spec, runner, repeats=1):
    if spec.get("version") != 1 or not isinstance(spec.get("probes"), list):
        raise ValueError("probe file must contain version=1 and a probes list")
    access_before = runner.access_count() if hasattr(runner, "access_count") else None
    probes = [evaluate_probe(probe, runner, repeats) for probe in spec["probes"]]
    access_after = runner.access_count() if hasattr(runner, "access_count") else None
    access_unchanged = (access_before == access_after) if access_before is not None else None
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only-context-no-model",
        "probe_count": len(probes),
        "access_count": {"before": access_before, "after": access_after,
                         "unchanged": access_unchanged},
        "passed": all(probe["passed"] for probe in probes) and access_unchanged is not False,
        "probes": probes,
    }


def live_runner(timeout):
    hooks = Path(__file__).resolve().parents[1] / "hooks"
    sys.path.insert(0, str(hooks))
    from _mem_common import identity, mem_ops, psql

    if not identity():
        raise RuntimeError("MEM_AUTHOR is required for a scoped live evaluation")

    class LiveRunner:
        def __call__(self, payload):
            raw = mem_ops("context", payload, timeout=timeout)
            if raw is None:
                raise RuntimeError("memory context backend is unavailable")
            return json.loads(raw)

        def access_count(self):
            raw = psql("SELECT coalesce(sum(access_count),0) FROM mem.memories "
                       "WHERE author=current_setting('mem.reader',true);", timeout=timeout)
            if raw is None or not raw.strip().isdigit():
                raise RuntimeError("could not read the scoped access counter")
            return int(raw.strip())

    return LiveRunner()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probes", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 20:
        parser.error("--repeats must be between 1 and 20")
    spec = json.loads(args.probes.read_text(encoding="utf-8"))
    report = evaluate(spec, live_runner(args.timeout), args.repeats)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
