-- Auditable evidence/derivation DAG between memories.
--
-- Direction is evidence_memory_id -> derived_memory_id. The derived row belongs to `author`;
-- evidence may be the author's own memory or a project memory currently visible to them.
-- A trigger checks visibility and rejects cycles under the same table lock used for insertion.

\set ON_ERROR_STOP on

DO $$ BEGIN
  CREATE ROLE hm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

BEGIN;
LOCK TABLE mem.memories IN SHARE ROW EXCLUSIVE MODE;

DO $$ BEGIN
  CREATE TYPE mem.derivation_relation AS ENUM
    ('supports','inferred_from','summarizes','supersedes','contradicts');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE UNIQUE INDEX IF NOT EXISTS mem_memories_author_id_idx ON mem.memories(author, id);

CREATE TABLE IF NOT EXISTS mem.derivations (
    id                 bigserial PRIMARY KEY,
    author             text NOT NULL,
    evidence_memory_id bigint NOT NULL REFERENCES mem.memories(id) ON DELETE CASCADE,
    derived_memory_id  bigint NOT NULL,
    relation           mem.derivation_relation NOT NULL DEFAULT 'supports',
    confidence         real NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    note               text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (evidence_memory_id <> derived_memory_id),
    UNIQUE (derived_memory_id, evidence_memory_id, relation),
    FOREIGN KEY (author, derived_memory_id) REFERENCES mem.memories(author, id)
      ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS mem_derivations_evidence_idx
  ON mem.derivations(evidence_memory_id);
CREATE INDEX IF NOT EXISTS mem_derivations_derived_idx
  ON mem.derivations(derived_memory_id);

-- Existing explicit version chains and consolidation source lists are already provenance;
-- materialise them before enabling the runtime guard. Only same-author endpoints are eligible,
-- matching the rules that current supersede/consolidation paths enforce.
DROP TRIGGER IF EXISTS derivations_guard ON mem.derivations;
INSERT INTO mem.derivations(author,evidence_memory_id,derived_memory_id,relation,note)
SELECT derived.author, derived.supersedes_id, derived.id, 'supersedes',
       'backfilled from memories.supersedes_id'
  FROM mem.memories derived
  JOIN mem.memories evidence ON evidence.id=derived.supersedes_id
 WHERE derived.supersedes_id IS NOT NULL AND evidence.author=derived.author
ON CONFLICT (derived_memory_id,evidence_memory_id,relation) DO NOTHING;

INSERT INTO mem.derivations(author,evidence_memory_id,derived_memory_id,relation,note)
SELECT derived.author, evidence.id, derived.id, 'summarizes',
       'backfilled from metadata.merged_from'
  FROM mem.memories derived
 CROSS JOIN LATERAL jsonb_array_elements_text(derived.metadata->'merged_from') member(raw)
  JOIN mem.memories evidence ON evidence.id=CASE WHEN member.raw ~ '^[0-9]+$'
                                                  THEN member.raw::bigint END
 WHERE jsonb_typeof(derived.metadata->'merged_from')='array'
   AND evidence.author=derived.author
ON CONFLICT (derived_memory_id,evidence_memory_id,relation) DO NOTHING;

DO $$ DECLARE has_cycle boolean;
BEGIN
  WITH RECURSIVE walk(start_id,current_id,path,cycle) AS (
    SELECT evidence_memory_id,derived_memory_id,
           ARRAY[evidence_memory_id,derived_memory_id],
           evidence_memory_id=derived_memory_id
      FROM mem.derivations
    UNION ALL
    SELECT w.start_id,d.derived_memory_id,w.path||d.derived_memory_id,
           d.derived_memory_id=ANY(w.path)
      FROM walk w JOIN mem.derivations d ON d.evidence_memory_id=w.current_id
     WHERE NOT w.cycle
  ) SELECT EXISTS (SELECT 1 FROM walk WHERE cycle) INTO has_cycle;
  IF has_cycle THEN
    RAISE EXCEPTION 'existing derivation data contains a cycle';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION mem.guard_derivation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,mem AS $$
DECLARE e record; d_author text; cycle_found boolean;
BEGIN
  -- Serialize edge additions: two concurrent acyclic inserts must not jointly form a cycle.
  LOCK TABLE mem.derivations IN SHARE ROW EXCLUSIVE MODE;
  SELECT author, scope, project INTO e FROM mem.memories WHERE id=NEW.evidence_memory_id;
  IF NOT FOUND OR NOT mem.may_read(e.author, e.scope, e.project) THEN
    RAISE EXCEPTION 'evidence memory % is not visible to %', NEW.evidence_memory_id, NEW.author;
  END IF;
  SELECT author INTO d_author FROM mem.memories WHERE id=NEW.derived_memory_id;
  IF d_author IS DISTINCT FROM NEW.author THEN
    RAISE EXCEPTION 'derived memory % is not owned by %', NEW.derived_memory_id, NEW.author;
  END IF;
  WITH RECURSIVE reachable(id) AS (
    SELECT NEW.derived_memory_id
    UNION
    SELECT d.derived_memory_id FROM mem.derivations d JOIN reachable r
      ON d.evidence_memory_id=r.id
  ) SELECT EXISTS (SELECT 1 FROM reachable WHERE id=NEW.evidence_memory_id)
      INTO cycle_found;
  IF cycle_found THEN
    RAISE EXCEPTION 'derivation cycle: % already descends from %',
                    NEW.evidence_memory_id, NEW.derived_memory_id;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER derivations_guard BEFORE INSERT OR UPDATE ON mem.derivations
  FOR EACH ROW EXECUTE FUNCTION mem.guard_derivation();
REVOKE ALL ON FUNCTION mem.guard_derivation() FROM PUBLIC;

ALTER TABLE mem.derivations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS derivations_read ON mem.derivations;
CREATE POLICY derivations_read ON mem.derivations FOR SELECT TO hm_app
  USING (EXISTS (SELECT 1 FROM mem.memories m WHERE m.id=derived_memory_id
                  AND mem.may_read(m.author,m.scope,m.project))
         AND EXISTS (SELECT 1 FROM mem.memories m WHERE m.id=evidence_memory_id
                      AND mem.may_read(m.author,m.scope,m.project)));
DROP POLICY IF EXISTS derivations_insert ON mem.derivations;
CREATE POLICY derivations_insert ON mem.derivations FOR INSERT TO hm_app
  WITH CHECK (author=(SELECT mem.reader())
              AND EXISTS (SELECT 1 FROM mem.memories m
                            WHERE m.id=evidence_memory_id
                              AND mem.may_read(m.author,m.scope,m.project))
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

GRANT SELECT, INSERT, UPDATE, DELETE ON mem.derivations TO hm_app;
GRANT USAGE, SELECT ON SEQUENCE mem.derivations_id_seq TO hm_app;

COMMIT;
