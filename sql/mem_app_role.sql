-- The database-level boundary: a role that CANNOT see past the rule, whatever the code does.
--
-- mem_multiuser.sql puts the rule in a function and calls it from the views. That is necessary
-- and it is not sufficient, which was established the expensive way: an adversarial audit of
-- the first version found four callers that reached the base tables instead -- a history flag
-- swapping the view out, a fetch-by-id that checked the scope but forgot membership, a write
-- that accepted any project, and a retract that checked nothing. Every one was a one-line fix.
-- That is exactly the problem: so is the next one.
--
-- If the application connects as the role that OWNS the tables, row-level security is inert --
-- owners bypass their own policies, and a superuser bypasses everything. `hm_app` is an
-- ordinary role, so the policies below bind it: a query that forgets the rule comes back with
-- no rows instead of every row, and the checks in the application become the second line rather
-- than the only one.
--
-- Run it after mem_multiuser.sql, and point the application's connection string at `hm_app`.
-- Idempotent: safe to re-run.

\set ON_ERROR_STOP on

-- No password is set here; see the note at the foot of this file.
--
-- Every identity comparison below goes through mem.reader() (sql/mem_multiuser.sql): on this
-- shared login it is the `mem.reader` setting the caller chose -- a filter, not a boundary --
-- and on a per-author login (sql/migrate_author_roles.sql) it is the author the database maps
-- that login to. Written `(SELECT mem.reader())` so the planner evaluates it once per query
-- rather than once per row.
--
-- This file is the LAST migration (`./hm migrate --list`) and re-states every policy on the
-- memory tables, including the ones the earlier migrations create, so that one file decides
-- what the application roles can reach. `./hm migrate` re-applies it whenever anything before it
-- was applied.
DO $$ BEGIN
  CREATE ROLE hm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER ROLE hm_app NOSUPERUSER NOBYPASSRLS;   -- re-asserted: the point of the role

GRANT USAGE ON SCHEMA mem, public TO hm_app;

-- Personal memory: the rows it may touch are decided by the policies below, not by the grant.
GRANT SELECT, INSERT, UPDATE, DELETE ON mem.memories, mem.assertions, mem.sources,
                                        mem.entities, mem.review_queue, mem.derivations
                                        TO hm_app;
-- Dreamer runs are an audit trail: the app records and finishes them, never erases them. Same
-- grant as sql/migrate_dreamer.sql; the REVOKE undoes the DELETE an earlier version of this
-- file handed out, since re-applying a GRANT never takes anything away.
GRANT SELECT, INSERT, UPDATE ON mem.dream_runs TO hm_app;
REVOKE DELETE ON mem.dream_runs FROM hm_app;
-- Not mem.project_members, and not mem.author_roles: together they are the directory of who
-- uses this store. The rule reads them through definer functions (mem.reader(),
-- mem.reader_projects()) that answer for the CALLER only. REVOKE, because an earlier version of
-- this file granted SELECT on the membership table and re-applying a GRANT never takes it away.
REVOKE ALL ON mem.project_members FROM hm_app;
REVOKE ALL ON mem.author_roles FROM hm_app;
GRANT EXECUTE ON FUNCTION mem.reader(), mem.reader_projects(), mem.whoami() TO hm_app;
-- Functions a migration may or may not have created yet in this store.
DO $$ BEGIN
  IF to_regprocedure('mem.premises_live(bigint)') IS NOT NULL THEN
    GRANT EXECUTE ON FUNCTION mem.premises_live(bigint) TO hm_app;   -- migrate_memory_lifecycle
  END IF;
END $$;
GRANT SELECT ON mem.active_memories, mem.all_memories TO hm_app;
GRANT SELECT ON mem.perspective_graph TO hm_app;
GRANT SELECT ON mem.peer_cards TO hm_app;
-- NOT mem.all_active_memories. It is the one relation with no rule on it, for maintenance.

-- The document corpus: read for search, write for the knowledge-page mirror. Conditional,
-- because the memory schema is usable on its own -- `schema.sql` is the document side, and a
-- store that only keeps memories should not fail to get a role.
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT unnest(ARRAY['documents','chunks']) LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO hm_app', t);
    END IF;
  END LOOP;
  FOR t IN SELECT unnest(ARRAY['components','constraints','relationships']) LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON public.%I TO hm_app', t);
    END IF;
  END LOOP;
  IF to_regclass('public.query_log') IS NOT NULL THEN
    GRANT SELECT, INSERT ON public.query_log TO hm_app;
  END IF;
  -- Read-only: `ci/doctor.py` reports missing migrations from it. Only `./hm migrate`, as the
  -- owner, writes it.
  IF to_regclass('public.hm_schema_migrations') IS NOT NULL THEN
    GRANT SELECT ON public.hm_schema_migrations TO hm_app;
  END IF;
END $$;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA mem TO hm_app;
DO $$ BEGIN
  EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hm_app';
EXCEPTION WHEN insufficient_privilege OR undefined_object THEN NULL; END $$;

-- Count every active memory the reader actually recalled, including project-shared rows
-- written by another author. The ordinary UPDATE policy is author-scoped, so this narrow
-- definer function performs the telemetry update only after rechecking visibility.
CREATE OR REPLACE FUNCTION mem.touch_memory_access(memory_ids bigint[]) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,mem AS $$
DECLARE touched bigint;
BEGIN
  UPDATE mem.memories m
     SET access_count=m.access_count+1, last_accessed_at=now()
   WHERE m.id=ANY(memory_ids) AND m.status='active'
     AND mem.may_read_as(m.author,m.scope,m.project,
                        (SELECT mem.reader()),(SELECT mem.reader_projects()));
  GET DIAGNOSTICS touched = ROW_COUNT;
  RETURN touched;
END $$;
REVOKE ALL ON FUNCTION mem.touch_memory_access(bigint[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mem.touch_memory_access(bigint[]) TO hm_app;

-- ── row-level security ───────────────────────────────────────────────────────────────────────
-- No FORCE: the owner bypasses its own policies, which is how the maintenance door stays open
-- -- migrations, restores, and the whole-store count a restore check must use. The policies
-- bind every other role. If one role has to do both jobs, add
-- `ALTER TABLE ... FORCE ROW LEVEL SECURITY` and give maintenance a separate BYPASSRLS login;
-- do not leave it with neither.
ALTER TABLE mem.memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.entities ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS entities_own ON mem.entities;
CREATE POLICY entities_own ON mem.entities FOR ALL TO hm_app
  USING (author = (SELECT mem.reader()))
  WITH CHECK (author = (SELECT mem.reader()));

DROP POLICY IF EXISTS mem_read ON mem.memories;
CREATE POLICY mem_read ON mem.memories FOR SELECT TO hm_app
  USING (mem.may_read_as(author, scope, project,
                        (SELECT mem.reader()), (SELECT mem.reader_projects())));

-- A write lands in your own name, and may only be shared with a project you belong to. The
-- application refuses both; this is what makes the refusal true even if it stops.
DROP POLICY IF EXISTS mem_insert ON mem.memories;
CREATE POLICY mem_insert ON mem.memories FOR INSERT TO hm_app
  WITH CHECK (author = (SELECT mem.reader())
              AND (scope <> 'project' OR mem.may_publish(project)));

-- Superseding, retracting and touching last_accessed_at are all UPDATEs. Yours only, and it
-- must still be yours afterwards.
DROP POLICY IF EXISTS mem_update ON mem.memories;
CREATE POLICY mem_update ON mem.memories FOR UPDATE TO hm_app
  USING (author = (SELECT mem.reader()))
  WITH CHECK (author = (SELECT mem.reader())
              AND (scope <> 'project' OR mem.may_publish(project)));

-- Deleting is for real removal -- a retraction is an UPDATE and keeps the row. Yours only:
-- superseding another author's memory is refused a few lines up, and deleting it outright is
-- the same act with less left behind.
DROP POLICY IF EXISTS mem_delete ON mem.memories;
CREATE POLICY mem_delete ON mem.memories FOR DELETE TO hm_app
  USING (author = (SELECT mem.reader()));

-- The review queue carries a paraphrase of the memories in its group, so it gets the same
-- treatment: yours to see, yours to resolve.
ALTER TABLE mem.review_queue ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS review_own ON mem.review_queue;
CREATE POLICY review_own ON mem.review_queue FOR ALL TO hm_app
  USING (author = (SELECT mem.reader()))
  WITH CHECK (author = (SELECT mem.reader()));

-- mem.sources holds session ids and verbatim transcript excerpts, keyed by memory_id alone --
-- readable by number with no check until now. It follows its memory: readable wherever the
-- memory is, changed only on a memory you wrote. Not one FOR ALL policy: its USING clause is
-- also DELETE's, so "readable" would let every project member delete the provenance and
-- assertions of everybody's shared rows (WITH CHECK is never evaluated for DELETE).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['sources', 'assertions'] LOOP
    EXECUTE format('ALTER TABLE mem.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON mem.%I',
                   CASE t WHEN 'sources' THEN 'sources_follow_memory'
                          ELSE 'assertions_follow_memory' END, t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON mem.%I', t || '_read', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON mem.%I', t || '_insert', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON mem.%I', t || '_update', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON mem.%I', t || '_delete', t);
    EXECUTE format($p$CREATE POLICY %I ON mem.%I FOR SELECT TO hm_app
      USING (EXISTS (SELECT 1 FROM mem.memories m
                      WHERE m.id = memory_id AND mem.may_read_as(m.author, m.scope, m.project,
                            (SELECT mem.reader()), (SELECT mem.reader_projects()))))$p$,
                   t || '_read', t);
    EXECUTE format($p$CREATE POLICY %I ON mem.%I FOR INSERT TO hm_app
      WITH CHECK (EXISTS (SELECT 1 FROM mem.memories m
                           WHERE m.id = memory_id AND m.author = (SELECT mem.reader())))$p$,
                   t || '_insert', t);
    EXECUTE format($p$CREATE POLICY %I ON mem.%I FOR UPDATE TO hm_app
      USING (EXISTS (SELECT 1 FROM mem.memories m
                      WHERE m.id = memory_id AND m.author = (SELECT mem.reader())))
      WITH CHECK (EXISTS (SELECT 1 FROM mem.memories m
                           WHERE m.id = memory_id AND m.author = (SELECT mem.reader())))$p$,
                   t || '_update', t);
    EXECUTE format($p$CREATE POLICY %I ON mem.%I FOR DELETE TO hm_app
      USING (EXISTS (SELECT 1 FROM mem.memories m
                      WHERE m.id = memory_id AND m.author = (SELECT mem.reader())))$p$,
                   t || '_delete', t);
  END LOOP;
END $$;

-- Evidence edges (sql/migrate_memory_evidence.sql): readable when both ends are, written in
-- your own name from evidence you can read onto a derived memory you own.
DO $$ BEGIN
  IF to_regclass('mem.derivations') IS NOT NULL THEN
    ALTER TABLE mem.derivations ENABLE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS derivations_read ON mem.derivations;
    CREATE POLICY derivations_read ON mem.derivations FOR SELECT TO hm_app
      USING (EXISTS (SELECT 1 FROM mem.memories m WHERE m.id=derived_memory_id
                      AND mem.may_read_as(m.author,m.scope,m.project,
                        (SELECT mem.reader()),(SELECT mem.reader_projects())))
             AND EXISTS (SELECT 1 FROM mem.memories m WHERE m.id=evidence_memory_id
                          AND mem.may_read_as(m.author,m.scope,m.project,
                        (SELECT mem.reader()),(SELECT mem.reader_projects()))));
    DROP POLICY IF EXISTS derivations_insert ON mem.derivations;
    CREATE POLICY derivations_insert ON mem.derivations FOR INSERT TO hm_app
      WITH CHECK (author=(SELECT mem.reader())
                  AND EXISTS (SELECT 1 FROM mem.memories m
                                WHERE m.id=evidence_memory_id
                                  AND mem.may_read_as(m.author,m.scope,m.project,
                        (SELECT mem.reader()),(SELECT mem.reader_projects())))
                  AND EXISTS (SELECT 1 FROM mem.memories m
                                WHERE m.id=derived_memory_id
                                  AND m.author=(SELECT mem.reader())));
    DROP POLICY IF EXISTS derivations_update ON mem.derivations;
    CREATE POLICY derivations_update ON mem.derivations FOR UPDATE TO hm_app
      USING (author=(SELECT mem.reader()))
      WITH CHECK (author=(SELECT mem.reader()));
    DROP POLICY IF EXISTS derivations_delete ON mem.derivations;
    CREATE POLICY derivations_delete ON mem.derivations FOR DELETE TO hm_app
      USING (author=(SELECT mem.reader()));
  END IF;
  -- Dreamer runs (sql/migrate_dreamer.sql) and peer cards (sql/migrate_peer_cards.sql): yours.
  IF to_regclass('mem.dream_runs') IS NOT NULL THEN
    ALTER TABLE mem.dream_runs ENABLE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS dream_runs_own ON mem.dream_runs;
    CREATE POLICY dream_runs_own ON mem.dream_runs FOR ALL TO hm_app
      USING (author=(SELECT mem.reader()))
      WITH CHECK (author=(SELECT mem.reader()));
  END IF;
  IF to_regclass('mem.peer_cards') IS NOT NULL THEN
    ALTER TABLE mem.peer_cards ENABLE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS peer_cards_own ON mem.peer_cards;
    CREATE POLICY peer_cards_own ON mem.peer_cards FOR SELECT TO hm_app
      USING (author=(SELECT mem.reader()));
  END IF;
END $$;

-- The views run as their OWNER unless told otherwise, and the owner bypasses the policies --
-- which would hand every policy-bound caller a way straight past RLS through a view.
-- security_invoker makes them run as whoever selects from them. Postgres 15+.
--
-- Re-created here, last, rather than only altered. A view's `m.*` is expanded once, when it is
-- created, so a column a later migration adds (retired_at, the perspective columns) is missing
-- from a view made before it; this file runs after every migration that adds one. Same rule as
-- sql/mem_multiuser.sql, in the form whose identity is resolved once per query
-- (mem.may_read_as with `(SELECT mem.reader())`), not once per row.
CREATE OR REPLACE VIEW mem.active_memories WITH (security_invoker = true) AS
  SELECT m.* FROM mem.memories m
  WHERE m.status = 'active'
    AND (m.valid_from IS NULL OR m.valid_from <= now())
    AND (m.valid_to   IS NULL OR m.valid_to   >  now())
    AND mem.may_read_as(m.author, m.scope, m.project,
                        (SELECT mem.reader()), (SELECT mem.reader_projects()));
CREATE OR REPLACE VIEW mem.all_memories WITH (security_invoker = true) AS
  SELECT m.* FROM mem.memories m
  WHERE mem.may_read_as(m.author, m.scope, m.project,
                        (SELECT mem.reader()), (SELECT mem.reader_projects()));
-- The maintenance view: no rule, and no grant to the application (see the grants above).
CREATE OR REPLACE VIEW mem.all_active_memories AS
  SELECT * FROM mem.memories
  WHERE status = 'active'
    AND (valid_from IS NULL OR valid_from <= now())
    AND (valid_to   IS NULL OR valid_to   >  now());

-- Any policy on a memory table that still compares to the raw setting is a hole on a per-author
-- login: the setting is the caller's to choose. Fail the migration rather than ship one.
DO $$ DECLARE bad text;
BEGIN
  SELECT string_agg(tablename || '.' || policyname, ', ') INTO bad
    FROM pg_policies
   WHERE schemaname = 'mem'
     AND (coalesce(qual, '') ~ 'current_setting\(''mem\.reader'''
          OR coalesce(with_check, '') ~ 'current_setting\(''mem\.reader''');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'policies still read the mem.reader setting directly: % -- use mem.reader()', bad;
  END IF;
END $$;

-- The password is set separately so it never sits in a file or in shell history:
--   read -rs APP_PW
--   printf "ALTER ROLE hm_app PASSWORD '%s';\n" "$APP_PW" | psql "$DATABASE_URL" -v ON_ERROR_STOP=1
-- and the same value goes wherever the application reads its connection string from.
