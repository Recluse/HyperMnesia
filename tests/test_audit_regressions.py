#!/usr/bin/env python3
"""Failure-boundary regressions: corpus policy, scheduled jobs, and dry-run bookkeeping.

Runs the real CLI/main paths against temporary state. No store, LLM or service is contacted.
"""
import _isolate  # noqa: F401 -- first: isolate HOME before any hook can be loaded
import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    with tempfile.TemporaryDirectory(prefix="hm-regression-") as raw:
        home = Path(raw)
        os.environ.update(HOME=str(home), USERPROFILE=str(home),
                          HYPERMNESIA_HOME=str(home / ".HyperMnesia"), MEM_AUTHOR="audit")
        os.environ.pop("HYPERMNESIA_ENV_FILE", None)
        (home / ".HyperMnesia/state").mkdir(parents=True)
        sys.path.insert(0, str(ROOT / "hooks"))
        ing = load("audit_ingest", "ingest/ingest_repo.py")
        repo = home / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("# Public\nPublic information.\n")
        (repo / "private").mkdir()
        (repo / "private/notes.md").write_text("# Private\nPRIVATE_CANARY_MUST_NOT_BE_INDEXED\n")
        policy = repo / ing.IGNORE_FILE
        policy.write_text("private/\n")
        out = home / "out.sql"
        cmd = [sys.executable, str(ROOT / "ingest/ingest_repo.py"), str(repo), "audit", str(out), "--walk"]
        subprocess.run(cmd, check=True, capture_output=True)
        assert "PRIVATE_CANARY" not in out.read_text()
        good_output = out.read_bytes()
        # A directory in place of the policy fails even when CI runs as root. EACCES/EIO are
        # also checked independently of host permissions; no failure may become zero exclusions.
        policy.unlink()
        policy.mkdir()
        assert subprocess.run(cmd, capture_output=True).returncode != 0
        assert out.read_bytes() == good_output
        policy.rmdir()
        policy.symlink_to(repo / "missing-policy")
        assert subprocess.run(cmd, capture_output=True).returncode != 0
        assert out.read_bytes() == good_output
        policy.unlink()
        for error in (PermissionError(errno.EACCES, "policy unreadable"), OSError(errno.EIO, "policy I/O error")):
            with patch("builtins.open", side_effect=error):
                try:
                    ing.read_ignore(str(repo))
                except OSError as caught:
                    assert caught.errno == error.errno
                else:
                    raise AssertionError("policy failure opened the corpus")
        assert ing.read_ignore(str(repo)) == []
        print("corpus policy failures are closed; previous output survives")

        dream = load("audit_dream", "hooks/mem_dream.py")
        reflect = load("audit_reflect", "hooks/mem_reflect.py")
        group = {"observer": {"id": 1, "name": "audit"}, "observed": {"id": 1, "name": "audit"},
                 "memories": [{"id": i, "type": "semantic", "project": "audit", "scope": "private",
                                "content": "a sufficiently long synthetic memory"} for i in range(1, 4)]}
        for job in (dream, reflect):
            for scenario in ("no_work", "success", "transport", "model", "start", "finish", "queue", "empty", "write", "publish"):
                if job is dream and scenario in ("empty", "write", "publish"):
                    continue
                if job is reflect and scenario in ("start", "finish", "queue"):
                    continue
                if job is reflect and scenario == "publish" and not hasattr(job, "synthesize"):
                    continue
                calls = []
                def transport(op, payload, **kwargs):
                    calls.append((op, payload))
                    if scenario == "transport":
                        return None
                    if op == "dream_inputs":
                        return json.dumps({"groups": [] if scenario == "no_work" else [group]})
                    if op == "dream_start":
                        return None if scenario == "start" else json.dumps({"started": True, "id": 1})
                    if op == "dream_finish":
                        return None if scenario == "finish" else "recorded"
                    if op == "review_add":
                        return None if scenario == "queue" else "queued"
                    if op == "reflect_targets":
                        return json.dumps([] if scenario == "no_work" else [{"project": "audit"}])
                    if op == "reflect_group":
                        return json.dumps([{"type": "semantic", "content": "synthetic"}] * 6)
                    if op == "page_upsert":
                        return None if scenario == "write" else json.dumps({"id": 5})
                    if op == "page_publish":
                        return None if scenario == "publish" else "published"
                    raise AssertionError(op)
                def model(*args):
                    if scenario == "model":
                        raise RuntimeError("synthetic model failure")
                    if job is dream:
                        return [{"reasoning_level": "deductive", "content": "A synthetic conclusion of sufficient length.",
                                 "evidence_ids": [1], "confidence": .8, "importance": .5}]
                    return "" if scenario == "empty" else "A synthetic page."
                job.mem_ops = transport
                job.identity = lambda: "audit"
                job.LOCK = str(home / (job.__name__ + ".lock"))
                if job is dream:
                    job.generate = model
                elif hasattr(job, "synthesize"):
                    job.synthesize = model
                else:
                    job.complete = model
                sys.argv = [job.__name__]
                with contextlib.redirect_stdout(io.StringIO()):
                    result = job.main()
                assert result == (0 if scenario in ("no_work", "success") else 1), (job.__name__, scenario, result)
                assert not Path(job.LOCK).exists(), (job.__name__, scenario, "lock retained")
                if scenario == "model" and job is dream:
                    assert any(op == "dream_finish" and p["status"] == "failed" for op, p in calls)
        print("scheduled failures exit 1, success/no-work exit 0, locks released")

        extract = load("audit_extract", "hooks/mem_extract.py")
        extract.identity = lambda: "audit"
        extract.legacy_twin = lambda name: None
        def forbidden(*args, **kwargs):
            raise AssertionError("unexpected store/model call")
        extract.mem_ops = extract.extract = forbidden
        tiny = home / "tiny.jsonl"
        tiny.write_text(json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n")
        records = [{"transcript_path": str(tiny)}, {"transcript_path": str(home / "gone.jsonl")}]
        queue = Path(extract.QUEUE)
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text("".join(json.dumps(record) + "\n" for record in records))
        before = queue.read_bytes()
        sys.argv = ["mem_extract.py", "--dry-run"]
        with contextlib.redirect_stdout(io.StringIO()):
            assert extract.main() == 0
        assert queue.read_bytes() == before and not Path(extract.DONE).exists()
        sys.argv = ["mem_extract.py"]
        with contextlib.redirect_stdout(io.StringIO()):
            assert extract.main() == 0
        assert len(Path(extract.DONE).read_text().splitlines()) == 2
        print("dry-run leaves durable queue unchanged; normal run records legitimate skips")


if __name__ == "__main__":
    main()
