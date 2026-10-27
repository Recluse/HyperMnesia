-- Memory lifecycle: two time axes kept apart, and derived memory judged by its premises.
--
-- Event time and belief time were one column. `valid_to` is EVENT time -- when a fact stopped
-- holding in the world -- but merge, mark and supersede all wrote now() into it. Merging two
-- duplicates then claimed the fact had stopped being true today, and correcting a mistake
-- claimed the mistake had been true until today. Nothing recorded when the store stopped
-- BELIEVING a row, so "what did I hold true a month ago" could not be answered at all.
--
--   retired_at  ingestion-time end of belief: set when a row leaves the active set (merge,
--               mark, supersede), cleared when it is marked active again. NULL while active.
--   valid_to    event time only. Changed only when a caller states event time (a supersede
--               with an explicit valid_from closes the old row's window there).
--
-- `mem.premises_live(id)` decides at QUERY time whether a derived conclusion may still be
-- served: every premise it was drawn from must be active and readable by the caller. Nothing
-- is cascaded -- a premise that comes back makes its conclusion servable again.
--
-- Runs after migrate_dreamer.sql and BEFORE mem_app_role.sql (which grants on what this
-- creates). Idempotent and re-runnable.

\set ON_ERROR_STOP on

BEGIN;
LOCK TABLE mem.memories IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE mem.memories ADD COLUMN IF NOT EXISTS retired_at timestamptz;
COMMENT ON COLUMN mem.memories.retired_at IS
  'Ingestion time at which the store stopped believing this row (left the active set). '
  'NULL while active. valid_to is event time and is not touched by retirement.';

-- Backfill: the best record of WHEN a non-active row was retired, in order of trust.
--   1. the created_at of a row written TO REPLACE it (its supersedes_id is this row): supersede
--      created it in the same transaction that retired this one;
--   2. valid_to, when it lies between the row's creation and now: every retiring path wrote
--      now() there, which is exactly the instant this column records (a caller-stated event
--      time outside that range is not mistaken for one);
--   3. the created_at of the row named in metadata.superseded_by, when not older than this
--      one -- a merge's replacement. A supersede VERDICT names a PRE-EXISTING winner instead,
--      whose created_at says nothing about this retirement (it can precede this row's own
--      creation), which is why rule 2 outranks this one;
--   4. the row's last update, else its creation.
-- valid_to itself is left as it is: which of those values were stated event time and which
-- were the old paths' now() cannot be told apart after the fact.
-- A row is never retired before it was learned: the final GREATEST holds whatever the sources.
UPDATE mem.memories m
   SET retired_at = GREATEST(m.created_at, COALESCE(
         (SELECT min(s.created_at) FROM mem.memories s
           WHERE s.supersedes_id = m.id AND s.created_at >= m.created_at),
         CASE WHEN m.valid_to >= m.created_at AND m.valid_to <= now() THEN m.valid_to END,
         -- CASE, not AND: only CASE guarantees the regex is tested before the cast, and the
         -- length bound keeps a 19-digit value from overflowing bigint and aborting the run.
         (SELECT s.created_at FROM mem.memories s
           WHERE s.id = CASE WHEN (m.metadata->>'superseded_by') ~ '^[0-9]{1,18}$'
                             THEN (m.metadata->>'superseded_by')::bigint END
             AND s.created_at >= m.created_at),
         GREATEST(m.updated_at, m.created_at)))
 WHERE m.status <> 'active' AND m.retired_at IS NULL;

-- An active row has not been retired. (A row re-activated by hand before this column existed.)
UPDATE mem.memories SET retired_at = NULL WHERE status = 'active' AND retired_at IS NOT NULL;

-- An event window that ends before it starts. The old supersede path produced these when the
-- replacement's valid_from preceded the old row's own valid_from. Such a row was never visible
-- (no instant satisfies both bounds), so collapsing the window to its start changes nothing a
-- reader sees -- and the original value is kept in metadata rather than lost.
UPDATE mem.memories
   SET metadata = metadata || jsonb_build_object('valid_to_repaired_from', valid_to),
       valid_to = valid_from
 WHERE valid_from IS NOT NULL AND valid_to IS NOT NULL AND valid_to < valid_from;

DO $$ BEGIN
  ALTER TABLE mem.memories ADD CONSTRAINT mem_valid_window_ordered
    CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER TABLE mem.memories VALIDATE CONSTRAINT mem_valid_window_ordered;

-- The premises a conclusion was approved from, stamped on conclusions approved before this
-- migration (approval stamps them from now on). Without the stamp, deleting a premise outright
-- cascades its edge away and the remaining premises alone would satisfy mem.premises_live.
-- Only where absent, so a re-run or a later approval's stamp is never overwritten.
UPDATE mem.memories m
   SET metadata = m.metadata || jsonb_build_object('premise_ids', e.ids)
  FROM (SELECT d.derived_memory_id AS id,
               jsonb_agg(DISTINCT d.evidence_memory_id ORDER BY d.evidence_memory_id) AS ids
          FROM mem.derivations d
         WHERE d.relation IN ('inferred_from', 'supports')
         GROUP BY d.derived_memory_id) e
 WHERE m.id = e.id
   AND m.metadata->>'reasoning_level' IN ('deductive', 'inductive')
   AND NOT (m.metadata ? 'premise_ids');

CREATE INDEX IF NOT EXISTS mem_memories_retired_idx
  ON mem.memories(retired_at) WHERE retired_at IS NOT NULL;

-- The reader as a function, in filter mode: the session's `mem.reader` setting. Created ONLY
-- when absent -- migrate_author_roles.sql (later in the order) replaces it with the enforced
-- version that maps a per-author login to its author, and re-running this file must never
-- downgrade that.
DO $$ BEGIN
  IF to_regprocedure('mem.reader()') IS NULL THEN
    EXECUTE $f$CREATE FUNCTION mem.reader() RETURNS text
      LANGUAGE sql STABLE SET search_path = pg_catalog
      AS 'SELECT current_setting(''mem.reader'', true)'$f$;
  END IF;
END $$;

-- A timestamp from stored JSON (metadata.retired_intervals), or NULL when it is not one. A
-- malformed entry is then ignored by `as_of` rather than failing the whole search.
CREATE OR REPLACE FUNCTION mem.try_timestamptz(value text) RETURNS timestamptz
LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
BEGIN
  IF value IS NULL OR value !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN
    RETURN NULL;
  END IF;
  RETURN value::timestamptz;
EXCEPTION WHEN others THEN
  RETURN NULL;
END $$;

-- The views were created with `SELECT m.*` before later migrations added columns, and a view's
-- `*` is expanded once, at creation. Re-created here so retired_at (and the perspective columns
-- added since) are readable through them; the definitions are otherwise those of
-- mem_multiuser.sql. CREATE OR REPLACE VIEW resets options, so security_invoker is restated.
CREATE OR REPLACE VIEW mem.active_memories WITH (security_invoker = true) AS
  SELECT m.* FROM mem.memories m
  WHERE m.status = 'active'
    AND (m.valid_from IS NULL OR m.valid_from <= now())
    AND (m.valid_to   IS NULL OR m.valid_to   >  now())
    AND mem.may_read(m.author, m.scope, m.project);

CREATE OR REPLACE VIEW mem.all_memories WITH (security_invoker = true) AS
  SELECT m.* FROM mem.memories m
  WHERE mem.may_read(m.author, m.scope, m.project);

CREATE OR REPLACE VIEW mem.all_active_memories AS
  SELECT * FROM mem.memories
  WHERE status = 'active'
    AND (valid_from IS NULL OR valid_from <= now())
    AND (valid_to   IS NULL OR valid_to   >  now());

-- May this derived conclusion still be served to the caller?
--
-- True only when the conclusion is readable, has at least one premise on record, and EVERY
-- premise -- `inferred_from` / `supports` edges plus the ids stamped in metadata.premise_ids at
-- approval -- is active, inside its validity window, and readable by the caller. A premise that
-- was deleted outright drops its edge (ON DELETE CASCADE), which is why the stamped ids count
-- too: without them, losing one of two premises would look like having only ever had one.
--
-- SECURITY DEFINER because mem.derivations is under RLS that hides an edge whose premise the
-- caller cannot read -- and a hidden edge would make an unreadable premise look like no premise
-- at all, i.e. like a conclusion with nothing wrong with it. Visibility is rechecked with the
-- caller's identity (mem.may_read reads the session's reader), and the answer is a single
-- boolean about a row the caller may already read.
CREATE OR REPLACE FUNCTION mem.premises_live(derived_id bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, mem AS $$
  WITH target AS (
    SELECT m.id, m.metadata FROM mem.memories m
     WHERE m.id = derived_id AND mem.may_read(m.author, m.scope, m.project)
  ), premise AS (
    SELECT d.evidence_memory_id AS id FROM mem.derivations d, target t
     WHERE d.derived_memory_id = t.id AND d.relation IN ('inferred_from', 'supports')
    UNION
    SELECT p.id FROM target t
     CROSS JOIN LATERAL jsonb_array_elements_text(
       CASE WHEN jsonb_typeof(t.metadata->'premise_ids') = 'array'
            THEN t.metadata->'premise_ids' ELSE '[]'::jsonb END) x(raw)
     CROSS JOIN LATERAL (SELECT CASE WHEN x.raw ~ '^[0-9]{1,18}$'
                                     THEN x.raw::bigint END AS id) p
     WHERE p.id IS NOT NULL
  )
  SELECT EXISTS (SELECT 1 FROM target)
     AND EXISTS (SELECT 1 FROM premise)
     AND NOT EXISTS (
       SELECT 1 FROM premise pr LEFT JOIN mem.memories p ON p.id = pr.id
        WHERE p.id IS NULL
           OR p.status <> 'active'
           OR (p.valid_from IS NOT NULL AND p.valid_from > now())
           OR (p.valid_to IS NOT NULL AND p.valid_to <= now())
           OR NOT mem.may_read(p.author, p.scope, p.project));
$$;
REVOKE ALL ON FUNCTION mem.premises_live(bigint) FROM PUBLIC;

-- The app role is granted in mem_app_role.sql. Also here when it already exists, so re-running
-- this file on a live store leaves the function callable.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hm_app') THEN
    GRANT EXECUTE ON FUNCTION mem.premises_live(bigint) TO hm_app;
    GRANT EXECUTE ON FUNCTION mem.reader() TO hm_app;
    GRANT EXECUTE ON FUNCTION mem.try_timestamptz(text) TO hm_app;
  END IF;
END $$;

COMMIT;
