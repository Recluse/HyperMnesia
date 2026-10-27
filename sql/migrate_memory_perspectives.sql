-- First-class observer -> observed perspectives for personal memory.
--
-- `author` remains the owner/RLS principal. A perspective is different: it records whose
-- point of view a statement represents and who/what that statement is about. Both ends reuse
-- the existing, author-owned entity graph; no second identity directory can drift from it.
--
-- Legacy rows are backfilled as author -> subject, or author -> self when no subject existed.
-- Old writers remain compatible through the trigger: `subject_entity_id` is the historical
-- name for the observed endpoint. New writes must keep both names equal.
-- Idempotent after a successful run.

\set ON_ERROR_STOP on

BEGIN;
LOCK TABLE mem.entities, mem.memories IN SHARE ROW EXCLUSIVE MODE;

-- Composite foreign keys make it impossible to connect one author's memory to another
-- author's private entity, even through direct SQL or a future caller that forgets the rule.
CREATE UNIQUE INDEX IF NOT EXISTS mem_entities_author_id_idx
  ON mem.entities(author, id);

ALTER TABLE mem.memories
  ADD COLUMN IF NOT EXISTS observer_entity_id bigint,
  ADD COLUMN IF NOT EXISTS observed_entity_id bigint;

-- Every identity gets a stable self node. It is deliberately an entity rather than a string:
-- aliases and later peer-card materialisation then have one canonical home.
INSERT INTO mem.entities(author, namespace, entity_type, canonical_name)
SELECT DISTINCT author, 'identity', 'peer', author
  FROM mem.memories
 WHERE author IS NOT NULL
ON CONFLICT (author, namespace, entity_type, canonical_name) DO NOTHING;

UPDATE mem.memories m
   SET observer_entity_id = self.id
  FROM mem.entities self
 WHERE m.observer_entity_id IS NULL
   AND self.author=m.author AND self.namespace='identity'
   AND self.entity_type='peer' AND self.canonical_name=m.author;

UPDATE mem.memories
   SET observed_entity_id = COALESCE(observed_entity_id, subject_entity_id,
                                    observer_entity_id)
 WHERE observed_entity_id IS NULL;

-- Keep the old column as a compatibility alias. Existing readers and assertion code continue
-- to see the same subject, while new code can use the directionally precise name.
UPDATE mem.memories
   SET subject_entity_id = observed_entity_id
 WHERE subject_entity_id IS NULL AND observed_entity_id IS NOT NULL;

CREATE OR REPLACE FUNCTION mem.sync_memory_perspective() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE self_id bigint;
BEGIN
  IF NEW.author IS NULL THEN
    RAISE EXCEPTION 'memory author is required before perspective resolution';
  END IF;
  INSERT INTO mem.entities(author, namespace, entity_type, canonical_name)
    VALUES (NEW.author, 'identity', 'peer', NEW.author)
    ON CONFLICT (author, namespace, entity_type, canonical_name)
    DO UPDATE SET canonical_name=EXCLUDED.canonical_name
    RETURNING id INTO self_id;
  NEW.observer_entity_id := COALESCE(NEW.observer_entity_id, self_id);
  NEW.observed_entity_id := COALESCE(NEW.observed_entity_id,
                                     NEW.subject_entity_id,
                                     NEW.observer_entity_id);
  IF NEW.subject_entity_id IS NOT NULL
     AND NEW.subject_entity_id <> NEW.observed_entity_id THEN
    RAISE EXCEPTION 'subject_entity_id (%) and observed_entity_id (%) disagree',
                    NEW.subject_entity_id, NEW.observed_entity_id;
  END IF;
  NEW.subject_entity_id := NEW.observed_entity_id;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS memories_perspective ON mem.memories;
CREATE TRIGGER memories_perspective
  BEFORE INSERT OR UPDATE OF author, subject_entity_id, observer_entity_id, observed_entity_id
  ON mem.memories FOR EACH ROW EXECUTE FUNCTION mem.sync_memory_perspective();

ALTER TABLE mem.memories ALTER COLUMN observer_entity_id SET NOT NULL;
ALTER TABLE mem.memories ALTER COLUMN observed_entity_id SET NOT NULL;

DO $$ BEGIN
  ALTER TABLE mem.memories ADD CONSTRAINT mem_observer_owned_fk
    FOREIGN KEY (author, observer_entity_id) REFERENCES mem.entities(author, id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE mem.memories ADD CONSTRAINT mem_observed_owned_fk
    FOREIGN KEY (author, observed_entity_id) REFERENCES mem.entities(author, id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS mem_memories_observer_idx
  ON mem.memories(observer_entity_id);
CREATE INDEX IF NOT EXISTS mem_memories_observed_idx
  ON mem.memories(observed_entity_id);
CREATE INDEX IF NOT EXISTS mem_memories_perspective_idx
  ON mem.memories(observer_entity_id, observed_entity_id);

-- This is the deliberate owner-run exception to the security_invoker views in mem_app_role.sql:
-- it needs author-owned entity rows for an otherwise RLS-visible shared memory, but exposes only
-- the four public
-- identity fields of nodes attached to a memory the current reader may read. Entity aliases and
-- attributes stay behind the stricter entity RLS policy, so sharing one project memory cannot
-- leak metadata accumulated from private memories about the same entity.
CREATE OR REPLACE VIEW mem.perspective_graph WITH (security_barrier=true) AS
SELECT m.id AS memory_id, m.author, m.memory_type, m.status, m.project, m.created_at,
       jsonb_build_object('id', observer.id, 'namespace', observer.namespace,
                          'entity_type', observer.entity_type,
                          'name', observer.canonical_name) AS observer,
       jsonb_build_object('id', observed.id, 'namespace', observed.namespace,
                          'entity_type', observed.entity_type,
                          'name', observed.canonical_name) AS observed
  FROM mem.memories m
  JOIN mem.entities observer ON observer.id=m.observer_entity_id
  JOIN mem.entities observed ON observed.id=m.observed_entity_id
 -- The identity is looked up once per query, not once per row (see may_read_as in
 -- mem_multiuser.sql); mem.reader() reads session_user, so the owner-run view still
 -- answers for the caller.
 WHERE mem.may_read_as(m.author, m.scope, m.project,
                       (SELECT mem.reader()), (SELECT mem.reader_projects()));

COMMENT ON VIEW mem.perspective_graph IS
  'Minimal observer -> observed identities for memories visible to mem.reader; aliases and entity attributes are intentionally omitted.';

COMMIT;
