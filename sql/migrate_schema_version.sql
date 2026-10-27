-- Which migration files this database has had applied, and the exact bytes of each.
--
-- Before this table the apply order lived as a copied file list in four places, and nothing
-- could tell a store that had every migration from one that had skipped the newest: both
-- answered queries, and the second failed only when a feature reached a column it lacked.
--
-- `./hm migrate` is the only writer. It applies the files in the order it lists
-- (`./hm migrate --list`), records each one's name and SHA-256 here, skips a file whose recorded
-- checksum matches, and REFUSES to continue when an already-applied file has changed on disk --
-- an edited migration is a decision for a person, not something to replay silently.
-- `ci/doctor.py` reads this table to report what is missing.
--
-- Idempotent: safe to re-run.

\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS public.hm_schema_migrations (
    name       text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    checksum   text NOT NULL
);

COMMENT ON TABLE public.hm_schema_migrations IS
  'Applied sql/ files: name, when, and the SHA-256 of the bytes applied. Written by ./hm migrate only.';
