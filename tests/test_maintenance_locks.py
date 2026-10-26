#!/usr/bin/env python3
"""Concurrency and stale-owner checks for the background maintenance locks.

    python3 tests/test_maintenance_locks.py
"""
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
from _mem_common import acquire_lock, release_lock  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    lock = str(Path(td) / "maintenance.lock")
    assert acquire_lock(lock)
    assert Path(lock).read_text() == str(os.getpid())
    assert not acquire_lock(lock), "a second caller entered while the owner was alive"
    release_lock(lock)
    assert not Path(lock).exists()

    Path(lock).write_text("99999999")
    assert acquire_lock(lock), "a lock whose process is gone was not recovered"
    release_lock(lock)

    Path(lock).write_text("not-a-pid")
    assert not acquire_lock(lock), "a fresh legacy/partial lock must fail closed"
    os.unlink(lock)

    # After a reboot the recorded PID is routinely reused by an unrelated live process. A lock
    # older than stale_after is stale whatever that PID says, or the job never runs again.
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        Path(lock).write_text(str(live.pid))
        assert not acquire_lock(lock), "a young lock with a live PID must hold"
        old = time.time() - 3 * 3600
        os.utime(lock, (old, old))
        assert acquire_lock(lock), "an old lock was kept alive by a reused PID"
        assert Path(lock).read_text() == str(os.getpid())
        release_lock(lock)
    finally:
        live.kill()
        live.wait()
    leftovers = [p for p in os.listdir(td) if ".stale." in p]
    assert not leftovers, f"recovery left renamed files behind: {leftovers}"

    # A live foreign process holds a lock we did not take: release must not remove it.
    Path(lock).write_text("1")
    release_lock(lock)
    assert Path(lock).exists(), "release_lock removed a lock owned by another process"
    os.unlink(lock)

# Every scheduled job takes the shared atomic lock; exists-then-create let two runs both enter.
for job in ("mem_extract.py", "mem_consolidate.py", "mem_reflect.py", "mem_dream.py"):
    src = (ROOT / "hooks" / job).read_text(encoding="utf-8")
    assert "acquire_lock(LOCK)" in src and "release_lock(LOCK)" in src, job
    assert not re.search(r"os\.path\.exists\(LOCK\)|os\.remove\(LOCK\)", src), job

# Profile-cache invalidation must target the file mem_profile.py actually writes.
for job in ("mem_consolidate.py", "mem_reflect.py"):
    src = (ROOT / "hooks" / job).read_text(encoding="utf-8")
    assert "PROFILE_CACHE = profile_cache_path()" in src, job
assert "CACHE = profile_cache_path()" in (ROOT / "hooks/mem_profile.py").read_text()

print("maintenance lock checks passed")
