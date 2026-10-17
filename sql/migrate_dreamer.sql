-- Observable, author-scoped runs for the background deductive/inductive Dreamer.
-- Candidate text stays in the existing review queue until a human approves it.

\set ON_ERROR_STOP on

DO $$ BEGIN
  CREATE ROLE hm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

BEGIN;

CREATE TABLE IF NOT EXISTS mem.dream_runs (
    id                  bigserial PRIMARY KEY,
    author              text NOT NULL,
    observer_entity_id  bigint NOT NULL,
    observed_entity_id  bigint NOT NULL,
    status              text NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','succeeded','failed')),
    input_count         integer NOT NULL DEFAULT 0 CHECK (input_count >= 0),
    candidate_count     integer NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    error               text,
    config              jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    FOREIGN KEY (author,observer_entity_id) REFERENCES mem.entities(author,id),
    FOREIGN KEY (author,observed_entity_id) REFERENCES mem.entities(author,id)
);

CREATE INDEX IF NOT EXISTS dream_runs_author_started_idx
  ON mem.dream_runs(author,started_at DESC);

ALTER TABLE mem.dream_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dream_runs_own ON mem.dream_runs;
CREATE POLICY dream_runs_own ON mem.dream_runs FOR ALL TO hm_app
  USING (author=current_setting('mem.reader',true))
  WITH CHECK (author=current_setting('mem.reader',true));

GRANT SELECT,INSERT,UPDATE ON mem.dream_runs TO hm_app;
GRANT USAGE,SELECT ON SEQUENCE mem.dream_runs_id_seq TO hm_app;

COMMENT ON TABLE mem.dream_runs IS
  'Auditable Dreamer attempts. Candidate conclusions remain human-gated in mem.review_queue.';
COMMENT ON COLUMN mem.review_queue.action IS
  'merge | supersede | derive; every derive candidate remains pending until explicit review';

COMMIT;
