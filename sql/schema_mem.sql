-- hypermnesia -- personal memory schema (`mem`). Idempotent.
-- Design:
--   * content (free text) is the primary representation; assertions are an optional exact-lookup index.
--   * no numeric decay -- correctness via validity windows + supersede; volume via importance at write.
--   * bi-temporal: valid_from/valid_to = EVENT time (when the fact holds), created_at = INGESTION time.
--     Retrieval filters superseded/out-of-window facts BY DEFAULT via mem.active_memories.
-- FTS stems with 'english' + 'simple' (swap 'english' for your language in mem.update_fts).
CREATE SCHEMA IF NOT EXISTS mem;

DO $$ BEGIN
  CREATE TYPE mem.memory_type AS ENUM
    ('semantic','episodic','preference','procedural','prospective','summary');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE mem.memory_status AS ENUM
    ('active','superseded','retracted','expired');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS mem.entities (
    id              BIGSERIAL PRIMARY KEY,
    namespace       TEXT NOT NULL,              -- user | project | host | technology | ...
    entity_type     TEXT NOT NULL,
    canonical_name  TEXT NOT NULL,
    aliases         TEXT[] NOT NULL DEFAULT '{}',
    attributes      JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, entity_type, canonical_name)
);

CREATE TABLE IF NOT EXISTS mem.memories (
    id                BIGSERIAL PRIMARY KEY,
    memory_type       mem.memory_type NOT NULL,
    status            mem.memory_status NOT NULL DEFAULT 'active',
    content           TEXT NOT NULL,            -- natural-language fact/episode (primary)
    title             TEXT,
    lang              TEXT DEFAULT 'en',
    importance        REAL NOT NULL DEFAULT 0.5,
    confidence        REAL NOT NULL DEFAULT 0.8,
    subject_entity_id BIGINT REFERENCES mem.entities(id),
    project           TEXT,                     -- workspace/repo tag
    valid_from        TIMESTAMPTZ,              -- event time: fact holds since
    valid_to          TIMESTAMPTZ,              -- event time: fact stopped holding
    event_time        TIMESTAMPTZ,              -- for episodic: when it happened
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),   -- ingestion time
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed_at  TIMESTAMPTZ,
    access_count      BIGINT NOT NULL DEFAULT 0,
    supersedes_id     BIGINT REFERENCES mem.memories(id),
    metadata          JSONB NOT NULL DEFAULT '{}',
    fts               TSVECTOR,
    embedding         vector(1024)
);

-- Optional exact-lookup index over memories (subject-predicate-object).
CREATE TABLE IF NOT EXISTS mem.assertions (
    id                BIGSERIAL PRIMARY KEY,
    memory_id         BIGINT NOT NULL REFERENCES mem.memories(id) ON DELETE CASCADE,
    subject_entity_id BIGINT REFERENCES mem.entities(id),
    predicate         TEXT NOT NULL,
    object_entity_id  BIGINT REFERENCES mem.entities(id),
    object_text       TEXT,
    object_number     NUMERIC,
    unit              TEXT,
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(object_entity_id, object_text, object_number) >= 1)
);

-- Provenance: where each memory came from (user said it != agent inferred it).
CREATE TABLE IF NOT EXISTS mem.sources (
    id           BIGSERIAL PRIMARY KEY,
    memory_id    BIGINT NOT NULL REFERENCES mem.memories(id) ON DELETE CASCADE,
    source_type  TEXT NOT NULL,   -- user_message | assistant_inference | tool_result | manual | consolidation
    session_id   TEXT,
    channel      TEXT,
    excerpt      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Confidence-gated consolidation review queue: the background consolidator only auto-applies a
-- merge/supersede when the LLM is confident; below the bar it parks the proposal here for a human.
CREATE TABLE IF NOT EXISTS mem.review_queue (
    id           BIGSERIAL PRIMARY KEY,
    action       TEXT NOT NULL,                       -- merge | supersede
    member_ids   BIGINT[] NOT NULL,
    proposal     JSONB NOT NULL,
    confidence   REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS mem_review_pending_idx
    ON mem.review_queue(created_at) WHERE status = 'pending';

-- The default read surface -- retrieval must not see superseded/out-of-window facts.
CREATE OR REPLACE VIEW mem.active_memories AS
  SELECT * FROM mem.memories
  WHERE status = 'active'
    AND (valid_from IS NULL OR valid_from <= now())
    AND (valid_to   IS NULL OR valid_to   >  now());

-- NOTE: if you set HM_FTS_LANG (used by the mem search query) to something other than
-- 'english', change the stemmer below to match, or the two legs disagree on word forms.
CREATE OR REPLACE FUNCTION mem.update_fts() RETURNS trigger AS $$
BEGIN
    NEW.fts := to_tsvector('english', coalesce(NEW.title,'') || ' ' || NEW.content)
            || to_tsvector('simple',  coalesce(NEW.title,'') || ' ' || NEW.content);
    NEW.updated_at := now();
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_fts ON mem.memories;
CREATE TRIGGER memories_fts BEFORE INSERT OR UPDATE OF content, title
    ON mem.memories FOR EACH ROW EXECUTE FUNCTION mem.update_fts();

CREATE INDEX IF NOT EXISTS mem_memories_status_type_idx ON mem.memories(status, memory_type);
CREATE INDEX IF NOT EXISTS mem_memories_subject_idx     ON mem.memories(subject_entity_id);
CREATE INDEX IF NOT EXISTS mem_memories_project_idx     ON mem.memories(project);
CREATE INDEX IF NOT EXISTS mem_memories_created_idx     ON mem.memories(created_at DESC);
CREATE INDEX IF NOT EXISTS mem_memories_fts_idx         ON mem.memories USING gin(fts);
CREATE INDEX IF NOT EXISTS mem_memories_hnsw_idx        ON mem.memories
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS mem_assertions_subj_pred_idx ON mem.assertions(subject_entity_id, predicate);
CREATE INDEX IF NOT EXISTS mem_entities_aliases_idx     ON mem.entities USING gin(aliases);
