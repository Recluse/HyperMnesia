#!/usr/bin/env python3
"""sql/migrate_memory_lifecycle.sql on a store that predates it.

The upgrade has to reconstruct what the old code never recorded, and two legacy shapes are easy
to get wrong:

  * a row retired by a supersede VERDICT points (metadata.superseded_by) at a PRE-EXISTING
    winner, sometimes older than the row itself. Taking that winner's created_at as retired_at
    made a row retired before it was learned; it must fall through to the next source, and no
    backfilled retired_at may precede its row's created_at;
  * a conclusion approved before the upgrade has no metadata.premise_ids. Without a backfill,
    deleting one premise outright (its edge cascades away) left the others to vouch for it.

Each run creates a scratch database, applies the migrations that precede this one in the
documented order, writes those legacy rows the way the old code did, applies the lifecycle
migration TWICE (it must be re-runnable), checks, and drops the scratch database. psql only.

    DATABASE_URL=postgresql://... python3 tests/test_memory_lifecycle_migration_live.py
"""
import _isolate  # noqa: F401  (first: a throwaway HOME and HYPERMNESIA_HOME)
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ.get("DATABASE_URL", "")
# The documented order up to this migration. Files a checkout does not have yet are skipped.
BEFORE = ["schema.sql", "migrate_schema_version.sql", "schema_mem.sql", "mem_multiuser.sql",
          "migrate_entity_ownership.sql", "migrate_memory_perspectives.sql",
          "migrate_memory_evidence.sql", "migrate_peer_cards.sql", "migrate_dreamer.sql"]

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def psql(url, sql=None, path=None, reader=None):
    argv = ["psql", url, "-tAX", "-q", "-v", "ON_ERROR_STOP=1", "-v", "owner=ci"]
    argv += ["-f", str(path)] if path else []
    env = {**os.environ, **({"PGOPTIONS": f"-c mem.reader={reader}"} if reader else {})}
    p = subprocess.run(argv, input=(sql or "").encode(), capture_output=True, timeout=120,
                       env=env)
    if p.returncode != 0:
        raise SystemExit(f"psql failed ({path or 'inline'}):\n{p.stderr.decode()[-2000:]}")
    return p.stdout.decode().strip()


def with_db(url, name):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


LEGACY = """
-- the old code's shapes, written directly
INSERT INTO mem.memories (id, memory_type, content, author, scope, created_at, status,
                          valid_to, metadata) VALUES
 -- a pre-existing winner, OLDER than the row its verdict retired
 (201,'semantic','winner, learned long ago','ci','private', now()-interval '30 days','active',
  NULL,'{}'),
 -- retired by that supersede verdict 3 days ago (the old mark wrote now() into valid_to)
 (202,'semantic','loser, learned later','ci','private', now()-interval '10 days','superseded',
  now()-interval '3 days','{"superseded_by":201}'),
 -- same shape with no valid_to at all: nothing but its own timestamps to go on
 (203,'semantic','loser without valid_to','ci','private', now()-interval '12 days','superseded',
  NULL,'{"superseded_by":201}'),
 -- a pre-existing winner NEWER than the loser but older than the retirement: the old
 -- valid_to (written at retirement) must outrank the winner's created_at
 (206,'semantic','winner, learned later','ci','private', now()-interval '8 days','active',
  NULL,'{}'),
 (207,'semantic','loser of a newer pre-existing winner','ci','private',
  now()-interval '10 days','superseded', now()-interval '3 days','{"superseded_by":206}'),
 -- metadata the old code never wrote but a hand edit can: must not abort the migration
 (208,'semantic','overflowing superseded_by','ci','private', now()-interval '6 days',
  'superseded', NULL,'{"superseded_by":"9223372036854775808"}'),
 -- retired by a row written to replace it: that row's created_at IS the retirement
 (204,'semantic','replaced','ci','private', now()-interval '9 days','superseded',
  now()-interval '2 days','{}'),
 -- two premises and a conclusion approved from them, before premise_ids existed
 (211,'semantic','premise a','ci','private', now()-interval '5 days','active',NULL,'{}'),
 (212,'semantic','premise b','ci','private', now()-interval '5 days','active',NULL,'{}'),
 (213,'semantic','conclusion from a and b','ci','private', now()-interval '4 days','active',
  NULL,'{"kind":"dream","reasoning_level":"inductive"}'),
 (214,'semantic','conclusion with a bad stamp','ci','private', now()-interval '4 days','active',
  NULL,'{"kind":"dream","reasoning_level":"inductive",
         "premise_ids":[212, "99999999999999999999"]}');
INSERT INTO mem.memories (id, memory_type, content, author, scope, created_at, supersedes_id)
VALUES (205,'semantic','replacement','ci','private', now()-interval '1 day', 204);
INSERT INTO mem.derivations (author, evidence_memory_id, derived_memory_id, relation) VALUES
 ('ci', 211, 213, 'inferred_from'), ('ci', 212, 213, 'inferred_from'),
 ('ci', 212, 214, 'inferred_from');
"""


def main():
    scratch = f"hm_lifecycle_mig_{os.getpid()}"
    admin = DATABASE_URL
    psql(admin, f"DROP DATABASE IF EXISTS {scratch}; CREATE DATABASE {scratch};")
    url = with_db(DATABASE_URL, scratch)
    try:
        psql(url, "CREATE EXTENSION IF NOT EXISTS vector;")
        for name in BEFORE:
            if (ROOT / "sql" / name).exists():
                psql(url, path=ROOT / "sql" / name)
        psql(url, LEGACY, reader="ci")   # the evidence guard checks visibility as the author
        for _ in range(2):
            psql(url, path=ROOT / "sql/migrate_memory_lifecycle.sql")

        print("== retired_at backfill ==")
        bad = psql(url, "SELECT coalesce(string_agg(id::text, ','), '') FROM mem.memories "
                        "WHERE status <> 'active' AND (retired_at IS NULL "
                        "OR retired_at < created_at)")
        check("every retired row has retired_at, never before its own created_at", bad == "",
              bad)
        ago = dict(line.split("|") for line in psql(url, """
            SELECT id, round(extract(epoch FROM now() - retired_at) / 86400)::int
              FROM mem.memories WHERE id IN (202, 203, 204, 207) ORDER BY id""").splitlines())
        check("a verdict's older winner is not taken as the retirement time; the old valid_to is",
              ago.get("202") == "3", str(ago))
        check("with nothing else to go on, the row's own timestamps (not the winner's)",
              psql(url, "SELECT retired_at >= created_at FROM mem.memories WHERE id=203") == "t")
        check("a row written to replace it dates the retirement", ago.get("204") == "1", str(ago))
        check("the old valid_to outranks a newer pre-existing winner's created_at",
              ago.get("207") == "3", str(ago))
        check("an overflowing superseded_by neither aborts the run nor goes unbackfilled",
              psql(url, "SELECT retired_at >= created_at FROM mem.memories WHERE id=208") == "t")
        check("event time is left alone",
              psql(url, "SELECT valid_to IS NULL FROM mem.memories WHERE id=203") == "t")

        print("\n== premise_ids backfill ==")
        check("a pre-upgrade conclusion gets its premises stamped",
              psql(url, "SELECT metadata->'premise_ids' FROM mem.memories WHERE id=213")
              == "[211, 212]")
        check("live while both premises are",
              psql(url, "SELECT mem.premises_live(213)", reader="ci") == "t")
        check("an overflowing premise id is ignored, not an error",
              psql(url, "SELECT mem.premises_live(214)", reader="ci") == "t")
        psql(url, "DELETE FROM mem.memories WHERE id=211;")
        check("the edge cascaded away with the deleted premise",
              psql(url, "SELECT count(*) FROM mem.derivations WHERE derived_memory_id=213") == "1")
        check("and the conclusion is stale anyway: the stamp still names it",
              psql(url, "SELECT mem.premises_live(213)", reader="ci") == "f")
    finally:
        psql(admin, f"DROP DATABASE IF EXISTS {scratch};")

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


def unavailable():
    if not DATABASE_URL:
        return "DATABASE_URL is not set"
    if not shutil.which("psql"):
        return "psql is not on PATH"
    try:
        p = subprocess.run(["psql", DATABASE_URL, "-tAX", "-c",
                            "SELECT rolcreatedb OR rolsuper FROM pg_roles "
                            "WHERE rolname = current_user"],
                           capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"psql could not run ({exc})"
    if p.returncode != 0:
        return f"the database did not answer: {p.stderr.decode(errors='replace').strip()}"
    if p.stdout.decode().strip() != "t":
        return "the connecting role may not create a scratch database"
    return None


if __name__ == "__main__":
    reason = unavailable()
    if reason:
        if os.environ.get("CI"):
            sys.exit(f"FAIL: {reason} -- CI provides a database for this test")
        print(f"SKIP: {reason}; this test needs a live database it may create databases on")
        sys.exit(0)
    sys.exit(main())
