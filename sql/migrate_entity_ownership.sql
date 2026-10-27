-- Give mutable entity rows the same author boundary as personal memories.
--
-- Usage (maintenance role; owner is only used for currently unreferenced legacy entities):
--   psql -v ON_ERROR_STOP=1 -v owner=your-handle -f sql/migrate_entity_ownership.sql
--
-- Referenced rows are attributed from their memories. If a legacy entity was shared by more
-- than one author, it is cloned and every memory/assertion is repointed to its author's clone.
-- Idempotent after the first successful run.

\set ON_ERROR_STOP on
\if :{?owner}
\else
  \echo 'owner variable is required'
  \quit 2
\endif

-- Policies name the application role. A fresh install reaches this migration before the full
-- grant/RLS script, so create the unprivileged shell here rather than making install order rely
-- on a role left over from another database in the same cluster.
DO $$ BEGIN
  CREATE ROLE hm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

BEGIN;
LOCK TABLE mem.entities, mem.memories, mem.assertions IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE mem.entities ADD COLUMN IF NOT EXISTS author text;

-- Preserve every legacy id and the set of authors that references it before anything moves.
CREATE TEMP TABLE entity_author_refs ON COMMIT DROP AS
SELECT entity_id, author
FROM (
  SELECT subject_entity_id AS entity_id, author
    FROM mem.memories WHERE subject_entity_id IS NOT NULL
  UNION
  SELECT a.subject_entity_id, m.author
    FROM mem.assertions a JOIN mem.memories m ON m.id=a.memory_id
   WHERE a.subject_entity_id IS NOT NULL
  UNION
  SELECT a.object_entity_id, m.author
    FROM mem.assertions a JOIN mem.memories m ON m.id=a.memory_id
   WHERE a.object_entity_id IS NOT NULL
) r
WHERE author IS NOT NULL;

-- One existing row becomes the first author's copy. An unreferenced legacy row belongs to the
-- explicitly supplied owner rather than to an implicit/global identity.
UPDATE mem.entities e
   SET author = COALESCE(e.author,
       (SELECT min(r.author) FROM entity_author_refs r WHERE r.entity_id=e.id), :'owner')
 WHERE e.author IS NULL;

DO $$ DECLARE c text;
BEGIN
  SELECT conname INTO c
    FROM pg_constraint
   WHERE conrelid='mem.entities'::regclass AND contype='u'
     AND pg_get_constraintdef(oid) = 'UNIQUE (namespace, entity_type, canonical_name)';
  IF c IS NOT NULL THEN EXECUTE format('ALTER TABLE mem.entities DROP CONSTRAINT %I', c); END IF;
END $$;

-- Fresh schema_mem.sql already has an equivalent UNIQUE constraint. Name-only IF NOT EXISTS
-- would create a second physical index because the constraint's backing index has another name.
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_index i
     WHERE i.indrelid='mem.entities'::regclass AND i.indisunique
       AND (SELECT array_agg(a.attname::text ORDER BY k.ord)
              FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ord)
              JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum)
           = ARRAY['author','namespace','entity_type','canonical_name']
  ) THEN
    CREATE UNIQUE INDEX mem_entities_author_identity_idx
      ON mem.entities(author, namespace, entity_type, canonical_name);
  END IF;
END $$;

-- Materialise per-author clones for a legacy entity shared by several authors.
INSERT INTO mem.entities(author, namespace, entity_type, canonical_name, aliases, attributes,
                         created_at)
SELECT r.author, e.namespace, e.entity_type, e.canonical_name, e.aliases, e.attributes, e.created_at
  FROM entity_author_refs r JOIN mem.entities e ON e.id=r.entity_id
 WHERE r.author <> e.author
ON CONFLICT (author, namespace, entity_type, canonical_name) DO NOTHING;

CREATE TEMP TABLE entity_author_map ON COMMIT DROP AS
SELECT r.entity_id AS old_id, r.author, target.id AS new_id
  FROM entity_author_refs r
  JOIN mem.entities old ON old.id=r.entity_id
  JOIN mem.entities target
    ON target.author=r.author
   AND target.namespace=old.namespace
   AND target.entity_type=old.entity_type
   AND target.canonical_name=old.canonical_name;

UPDATE mem.memories m
   SET subject_entity_id=map.new_id
  FROM entity_author_map map
 WHERE m.subject_entity_id=map.old_id AND m.author=map.author
   AND m.subject_entity_id IS DISTINCT FROM map.new_id;

UPDATE mem.assertions a
   SET subject_entity_id=map.new_id
  FROM mem.memories m, entity_author_map map
 WHERE a.memory_id=m.id AND a.subject_entity_id=map.old_id AND m.author=map.author
   AND a.subject_entity_id IS DISTINCT FROM map.new_id;

UPDATE mem.assertions a
   SET object_entity_id=map.new_id
  FROM mem.memories m, entity_author_map map
 WHERE a.memory_id=m.id AND a.object_entity_id=map.old_id AND m.author=map.author
   AND a.object_entity_id IS DISTINCT FROM map.new_id;

ALTER TABLE mem.entities ALTER COLUMN author SET NOT NULL;

ALTER TABLE mem.entities ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS entities_own ON mem.entities;
CREATE POLICY entities_own ON mem.entities FOR ALL TO hm_app
  USING (author = (SELECT mem.reader()))
  WITH CHECK (author = (SELECT mem.reader()));

COMMIT;
