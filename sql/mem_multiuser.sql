-- Multi-user personal memory: author + audience, not one namespace per person.
--
-- WHY NOT A `user_id`. The rows in this store are not all of one kind with respect to sharing.
-- A `preference` ("runs commands one at a time, reads the result before the next") is about ONE
-- person and is actively harmful injected into somebody else's session -- they take another
-- person's habits for project rules. A `semantic` or `procedural` fact ("production lives in
-- namespace X", "run the whole test suite from the module root after touching that interface")
-- is about the PROJECT, and a second engineer having memory is worth something precisely
-- because what they learn reaches everyone. Per-person namespaces give the worst of both: their
-- findings reach nobody, and personal preferences still need hiding by some other means.
--
-- Every row therefore records WHO learned it (`author`) and WHO may see it (`scope`), and
-- `mem.project_members` says which projects an author may read the shared memory of.
--
-- The surrounding agent-memory products this was measured against (mem0's user_id/agent_id/
-- run_id, Zep/Graphiti's group_id) all do namespace isolation and none has a visibility model.
-- Zep's own documentation makes the point this file is built on: a namespace filter is not
-- authorization, and the identifier must never come from an untrusted request. Here it comes
-- from the environment locally, and from the authenticated key at any remote boundary.
--
-- Apply AFTER schema_mem.sql, and follow it with mem_app_role.sql -- the rule below is enforced
-- by row-level security there, which is what makes it hold for a caller that forgets it.
-- Idempotent: safe to re-run.

DO $$ BEGIN
  CREATE TYPE mem.memory_scope AS ENUM ('private', 'project');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE mem.memories
  ADD COLUMN IF NOT EXISTS author text,
  -- DEFAULT 'private', and that is the safety property, not a preference: a write that forgets
  -- to say who may see it must not end up shared. Guessing wrong towards "did not show it" is
  -- cheap; guessing wrong towards "showed someone else's" is not.
  ADD COLUMN IF NOT EXISTS scope mem.memory_scope NOT NULL DEFAULT 'private';

-- The review queue holds the consolidator's proposal text -- a paraphrase of the memories in
-- its group -- and belongs to whoever wrote them.
ALTER TABLE mem.review_queue ADD COLUMN IF NOT EXISTS author text;

COMMENT ON COLUMN mem.memories.author IS
  'Who learned this. Recorded, never inferred. NULL only during migration.';
COMMENT ON COLUMN mem.memories.scope IS
  'Who may read it. private = the author alone; project = everyone with access to its project. '
  'A row with no project tag must never be ''project'': there is no audience to define.';

CREATE TABLE IF NOT EXISTS mem.project_members (
    author     text NOT NULL,
    project    text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (author, project)
);

COMMENT ON TABLE mem.project_members IS
  'Which projects an author may read the shared memory of. Membership is granted explicitly; '
  'there is no "all projects" row on purpose -- a new project is unshared until someone says '
  'otherwise, which is the direction this store errs in everywhere else.';

CREATE INDEX IF NOT EXISTS mem_scope_project_idx
    ON mem.memories(scope, project) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS mem_author_idx ON mem.memories(author);

-- A row claiming a project audience without a project names no audience at all.
DO $$ BEGIN
  ALTER TABLE mem.memories ADD CONSTRAINT mem_project_scope_needs_a_project
    CHECK (scope <> 'project' OR project IS NOT NULL) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── the rule, defined ONCE ───────────────────────────────────────────────────────────────────
-- As a FUNCTION, not only as a view. The first version of this feature put the rule in a view
-- and trusted every caller to go through it. An adversarial audit then found four callers that
-- did not: a history flag swapping the view for the base table, a fetch-by-id that checked the
-- scope but forgot membership, a write that accepted any project, and a retract that checked
-- nothing. Each was a one-line fix, which is the point -- the next one would have been too.
--
-- A rule that lives in a view can only be reused by whoever remembers the view.
CREATE OR REPLACE FUNCTION mem.may_read(m_author text, m_scope mem.memory_scope, m_project text)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT m_author = current_setting('mem.reader', true)
      OR (m_scope = 'project' AND m_project IS NOT NULL AND EXISTS (
            SELECT 1 FROM mem.project_members pm
             WHERE pm.author = current_setting('mem.reader', true)
               AND pm.project = m_project));
$$;

-- May this reader PUBLISH to this project? Writing `scope='project'` for a project you do not
-- belong to puts a row where you cannot read it back and its members can -- a way into other
-- people's sessions from outside their project.
CREATE OR REPLACE FUNCTION mem.may_publish(m_project text)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT m_project IS NOT NULL AND EXISTS (
            SELECT 1 FROM mem.project_members pm
             WHERE pm.author = current_setting('mem.reader', true)
               AND pm.project = m_project);
$$;

-- `mem.reader` is a session setting each connection sets to the identity it reads as. When it
-- is unset, `current_setting(..., true)` is NULL and only rows shared with a project the reader
-- belongs to come back -- which for an unidentified reader is none. That is the failure
-- direction this store wants: a caller who forgot to say who it is sees LESS than it should
-- (its own preferences stop appearing in its profile -- loud, and noticed at once), never more.
--
-- Plain `=`, deliberately, NOT `IS NOT DISTINCT FROM`: with the latter an unset reader and a
-- NULL author compare EQUAL, so a caller that never identified itself would see every row
-- nobody has claimed -- which, during the migration window, is the entire store.
CREATE OR REPLACE VIEW mem.active_memories AS
  SELECT m.* FROM mem.memories m
  WHERE m.status = 'active'
    AND (m.valid_from IS NULL OR m.valid_from <= now())
    AND (m.valid_to   IS NULL OR m.valid_to   >  now())
    AND mem.may_read(m.author, m.scope, m.project);

-- Every row this reader may see, superseded and out-of-window included. A history flag used to
-- reach this by swapping the view for the BASE TABLE, which has no rule on it at all. History
-- is a narrower question than "the whole store", and this is the view that answers it.
CREATE OR REPLACE VIEW mem.all_memories AS
  SELECT m.* FROM mem.memories m
  WHERE mem.may_read(m.author, m.scope, m.project);

-- Everything, regardless of reader -- the ONLY relation here with no rule on it, named so that
-- using it is a decision. For maintenance that must not under-report: a post-restore row count,
-- a migration. Nothing a second author can reach may query this.
CREATE OR REPLACE VIEW mem.all_active_memories AS
  SELECT * FROM mem.memories
  WHERE status = 'active'
    AND (valid_from IS NULL OR valid_from <= now())
    AND (valid_to   IS NULL OR valid_to   >  now());

-- ---------------------------------------------------------------------------------------
-- BACKFILL -- a decision, run deliberately, not part of the structural migration above.
--
-- An existing store's rows were all written by one person; that part is not in question. What
-- their audience should be IS:
--
--   (A) everything private, then promote project facts by review. Nobody else sees anything
--       until it has been looked at. Safe, and a second author starts with an empty store.
--
--   (B) by type -- preference stays private, the rest goes to its project. The second author is
--       useful immediately, and something you would not have shared can go with it.
--
-- (B) is only honest with the review actually done BEFORE anyone else connects. Both are
-- written out here because neither is a default.
--
--   \set owner 'your-handle'
--
-- UPDATE mem.memories SET author = :'owner' WHERE author IS NULL;
-- UPDATE mem.review_queue SET author = :'owner' WHERE author IS NULL;
-- ALTER TABLE mem.memories ALTER COLUMN author SET NOT NULL;
--
-- -- (A): nothing -- the column already defaults to 'private'.
--
-- -- (B):
-- -- UPDATE mem.memories SET scope = 'project'
-- --  WHERE project IS NOT NULL AND memory_type <> 'preference';
--
-- -- Whoever the rows belong to must be a member of their own projects, or the backfill takes
-- -- the store away from the person it belongs to:
-- -- INSERT INTO mem.project_members (author, project)
-- --   SELECT DISTINCT :'owner', project FROM mem.memories WHERE project IS NOT NULL
-- --   ON CONFLICT DO NOTHING;
--
-- -- Then, before anyone else connects, READ what (B) just published:
-- -- SELECT id, memory_type, project, left(content, 160) FROM mem.memories
-- --  WHERE scope = 'project' ORDER BY project, id;
--
-- ALTER TABLE mem.memories VALIDATE CONSTRAINT mem_project_scope_needs_a_project;
