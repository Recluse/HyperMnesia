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
DO $$ BEGIN
  CREATE ROLE hm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER ROLE hm_app NOSUPERUSER NOBYPASSRLS;   -- re-asserted: the point of the role

GRANT USAGE ON SCHEMA mem, public TO hm_app;

-- Personal memory: the rows it may touch are decided by the policies below, not by the grant.
GRANT SELECT, INSERT, UPDATE, DELETE ON mem.memories, mem.assertions, mem.sources,
                                        mem.entities, mem.review_queue TO hm_app;
GRANT SELECT ON mem.project_members TO hm_app;
GRANT SELECT ON mem.active_memories, mem.all_memories TO hm_app;
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
END $$;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA mem TO hm_app;
DO $$ BEGIN
  EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hm_app';
EXCEPTION WHEN insufficient_privilege OR undefined_object THEN NULL; END $$;

-- ── row-level security ───────────────────────────────────────────────────────────────────────
-- No FORCE: the owner bypasses its own policies, which is how the maintenance door stays open
-- -- migrations, restores, and the whole-store count a restore check must use. The policies
-- bind every other role. If one role has to do both jobs, add
-- `ALTER TABLE ... FORCE ROW LEVEL SECURITY` and give maintenance a separate BYPASSRLS login;
-- do not leave it with neither.
ALTER TABLE mem.memories ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mem_read ON mem.memories;
CREATE POLICY mem_read ON mem.memories FOR SELECT TO hm_app
  USING (mem.may_read(author, scope, project));

-- A write lands in your own name, and may only be shared with a project you belong to. The
-- application refuses both; this is what makes the refusal true even if it stops.
DROP POLICY IF EXISTS mem_insert ON mem.memories;
CREATE POLICY mem_insert ON mem.memories FOR INSERT TO hm_app
  WITH CHECK (author = current_setting('mem.reader', true)
              AND (scope <> 'project' OR mem.may_publish(project)));

-- Superseding, retracting and touching last_accessed_at are all UPDATEs. Yours only, and it
-- must still be yours afterwards.
DROP POLICY IF EXISTS mem_update ON mem.memories;
CREATE POLICY mem_update ON mem.memories FOR UPDATE TO hm_app
  USING (author = current_setting('mem.reader', true))
  WITH CHECK (author = current_setting('mem.reader', true)
              AND (scope <> 'project' OR mem.may_publish(project)));

-- Deleting is for real removal -- a retraction is an UPDATE and keeps the row. Yours only:
-- superseding another author's memory is refused a few lines up, and deleting it outright is
-- the same act with less left behind.
DROP POLICY IF EXISTS mem_delete ON mem.memories;
CREATE POLICY mem_delete ON mem.memories FOR DELETE TO hm_app
  USING (author = current_setting('mem.reader', true));

-- The review queue carries a paraphrase of the memories in its group, so it gets the same
-- treatment: yours to see, yours to resolve.
ALTER TABLE mem.review_queue ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS review_own ON mem.review_queue;
CREATE POLICY review_own ON mem.review_queue FOR ALL TO hm_app
  USING (author IS NOT DISTINCT FROM current_setting('mem.reader', true))
  WITH CHECK (author IS NOT DISTINCT FROM current_setting('mem.reader', true));

-- mem.sources holds session ids and verbatim transcript excerpts, keyed by memory_id alone --
-- readable by number with no check until now. It follows its memory.
ALTER TABLE mem.sources ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sources_follow_memory ON mem.sources;
CREATE POLICY sources_follow_memory ON mem.sources FOR ALL TO hm_app
  USING (EXISTS (SELECT 1 FROM mem.memories m
                  WHERE m.id = memory_id AND mem.may_read(m.author, m.scope, m.project)))
  WITH CHECK (EXISTS (SELECT 1 FROM mem.memories m
                       WHERE m.id = memory_id
                         AND m.author = current_setting('mem.reader', true)));

ALTER TABLE mem.assertions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS assertions_follow_memory ON mem.assertions;
CREATE POLICY assertions_follow_memory ON mem.assertions FOR ALL TO hm_app
  USING (EXISTS (SELECT 1 FROM mem.memories m
                  WHERE m.id = memory_id AND mem.may_read(m.author, m.scope, m.project)))
  WITH CHECK (EXISTS (SELECT 1 FROM mem.memories m
                       WHERE m.id = memory_id
                         AND m.author = current_setting('mem.reader', true)));

-- The views run as their OWNER unless told otherwise, and the owner bypasses the policies --
-- which would hand every policy-bound caller a way straight past RLS through a view.
-- security_invoker makes them run as whoever selects from them. Postgres 15+.
ALTER VIEW mem.active_memories SET (security_invoker = true);
ALTER VIEW mem.all_memories    SET (security_invoker = true);

-- The password is set separately so it never sits in a file or in shell history:
--   read -rs APP_PW
--   printf "ALTER ROLE hm_app PASSWORD '%s';\n" "$APP_PW" | psql "$DATABASE_URL" -v ON_ERROR_STOP=1
-- and the same value goes wherever the application reads its connection string from.
