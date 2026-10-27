-- Constraint -> source document by (repo, path, hash), not only by row id.
--
-- constraints.source_doc_id is a plain FK with ON DELETE SET NULL, and a re-ingest deletes and
-- re-inserts a changed document. So editing exactly the document a rule was written from cleared
-- the rule's link, and "freshness flags drift" could never fire: the one change it exists to
-- notice destroyed the evidence. The document is now also named by what survives a re-insert --
-- its repo and path -- plus the content hash the rule was written against.
--
--   source_repo, source_path  the document, by name. Backfilled from the currently linked row.
--   source_hash               documents.content_hash when the rule was linked; ci/freshness.py
--                             compares it with the file as it is now ("source changed since the
--                             rule was written") and reports a source_path that no longer exists.
--
-- Two triggers keep it consistent without any writer having to know:
--   * BEFORE INSERT/UPDATE OF source_doc_id ON constraints fills the three columns from the
--     linked document when they are empty, so an older seed that sets only source_doc_id keeps
--     working; and when an UPDATE points source_doc_id at a DIFFERENT document (another
--     repo/path), the identity and hash follow it -- otherwise the old name would stay behind
--     and the next re-insert of the old document would silently link the rule back to it.
--     Re-linking to a new row of the SAME document keeps the recorded hash: that is a re-ingest,
--     and the hash is what freshness compares against;
--   * AFTER INSERT ON documents re-links every constraint naming that (repo, path) to the new
--     row id -- the re-link an ingest needs, for every ingest path at once. It never overwrites
--     a recorded source_hash: re-accepting a changed source is a review decision
--     (`ci/freshness.py --accept-sources`), not something an ingest may do by itself.
--
-- The document trigger is SECURITY DEFINER with a pinned search_path because ingest may run as
-- the app login, which can write documents but not constraints (sql/mem_app_role.sql). It writes
-- only those two columns, and only for constraints that already name the inserted path.
--
-- Idempotent and re-runnable. Apply after schema.sql (see `hm migrate` for the full order).

\set ON_ERROR_STOP on

BEGIN;

ALTER TABLE constraints ADD COLUMN IF NOT EXISTS source_repo text;
ALTER TABLE constraints ADD COLUMN IF NOT EXISTS source_path text;
ALTER TABLE constraints ADD COLUMN IF NOT EXISTS source_hash text;

-- Backfill from the currently linked document. Only rows that name no source yet, so a re-run
-- never rewrites a hash that freshness is comparing against.
UPDATE constraints c
   SET source_repo = d.repo, source_path = d.path, source_hash = d.content_hash
  FROM documents d
 WHERE d.id = c.source_doc_id AND c.source_path IS NULL;

CREATE INDEX IF NOT EXISTS constraints_source_idx ON constraints (source_repo, source_path);

CREATE OR REPLACE FUNCTION public.hm_constraint_source_fill() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE d_repo text; d_path text; d_hash text;
BEGIN
  IF NEW.source_doc_id IS NULL THEN
    RETURN NEW;                       -- unlinked (or ON DELETE SET NULL): the name stays
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.source_doc_id IS NOT DISTINCT FROM OLD.source_doc_id THEN
    RETURN NEW;
  END IF;
  SELECT d.repo, d.path, d.content_hash INTO d_repo, d_path, d_hash
    FROM public.documents d WHERE d.id = NEW.source_doc_id;
  IF NOT FOUND THEN
    RETURN NEW;
  END IF;
  IF NEW.source_path IS NULL THEN
    NEW.source_repo := d_repo; NEW.source_path := d_path;
    NEW.source_hash := coalesce(NEW.source_hash, d_hash);
  ELSIF TG_OP = 'UPDATE'
        AND (d_repo, d_path) IS DISTINCT FROM (NEW.source_repo, NEW.source_path)
        AND (NEW.source_repo, NEW.source_path) IS NOT DISTINCT FROM (OLD.source_repo, OLD.source_path) THEN
    -- Re-pointed at another document without naming it: the name and hash follow the row.
    NEW.source_repo := d_repo; NEW.source_path := d_path; NEW.source_hash := d_hash;
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS constraints_source_fill ON constraints;
CREATE TRIGGER constraints_source_fill
  BEFORE INSERT OR UPDATE OF source_doc_id ON constraints
  FOR EACH ROW EXECUTE FUNCTION public.hm_constraint_source_fill();

CREATE OR REPLACE FUNCTION public.hm_constraint_source_relink() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  UPDATE public.constraints c
     SET source_doc_id = NEW.id,
         source_hash = coalesce(c.source_hash, NEW.content_hash)
   WHERE c.source_repo = NEW.repo AND c.source_path = NEW.path
     AND (c.source_doc_id IS DISTINCT FROM NEW.id OR c.source_hash IS NULL);
  RETURN NULL;
END $$;
-- Fires as part of an INSERT on documents; nobody needs to call it directly.
REVOKE ALL ON FUNCTION public.hm_constraint_source_relink() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.hm_constraint_source_fill() FROM PUBLIC;

DROP TRIGGER IF EXISTS documents_relink_constraints ON documents;
CREATE TRIGGER documents_relink_constraints
  AFTER INSERT ON documents
  FOR EACH ROW EXECUTE FUNCTION public.hm_constraint_source_relink();

-- Constraints whose link an earlier re-ingest already cleared cannot be recovered from the
-- database; their seed has to set source_repo/source_path again (examples/seed_example.sql).

COMMIT;
