-- hypermnesia -- doc-RAG + structural (Tier 0/1/2) schema. Idempotent.
-- Apply:  psql "$DATABASE_URL" -f sql/schema.sql
--
-- FTS language: the composite tsvector below stems with 'english' and also indexes 'simple'
-- (exact tokens -- code identifiers, IDs, non-English words the stemmer would mangle). Swap
-- 'english' for your language ('russian', 'german', ...) in ingest_repo.py and the mem trigger.
CREATE EXTENSION IF NOT EXISTS vector;

-- Source documents (multi-repo: one install serves many projects, scoped by `repo`).
CREATE TABLE IF NOT EXISTS documents (
    id            BIGSERIAL PRIMARY KEY,
    repo          TEXT NOT NULL,
    path          TEXT NOT NULL,
    doc_type      TEXT NOT NULL,          -- adr | spec | runbook | readme | api | note
    title         TEXT,
    git_commit    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    token_count   INT,
    status        TEXT DEFAULT 'active',  -- active | stale | archived
    updated_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (repo, path)
);

-- Components / modules -- structural project map (Tier 0/1).
CREATE TABLE IF NOT EXISTS components (
    id              BIGSERIAL PRIMARY KEY,
    repo            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    name            TEXT NOT NULL,
    parent_id       BIGINT REFERENCES components(id),
    responsibility  TEXT NOT NULL,
    key_paths       TEXT[] NOT NULL,      -- globs; resolver = longest-match wins, `priority` breaks ties
    priority        INT NOT NULL DEFAULT 0,
    owner           TEXT,
    UNIQUE (repo, slug)
);

-- Architectural constraints & decisions (Tier 0/1).
CREATE TABLE IF NOT EXISTS constraints (
    id            BIGSERIAL PRIMARY KEY,
    repo          TEXT NOT NULL,
    kind          TEXT NOT NULL,          -- invariant | adr | convention
    scope         TEXT NOT NULL,          -- global | component
    component_id  BIGINT REFERENCES components(id),  -- NULL if global
    title         TEXT NOT NULL,
    statement     TEXT NOT NULL,
    rationale     TEXT,
    severity      TEXT DEFAULT 'must',    -- must | should | info
    status        TEXT DEFAULT 'active',  -- active | deprecated | superseded
    -- ON DELETE SET NULL: a repo reingest deletes+reinserts its documents, so a constraint's
    -- source link must survive that (the constraint stays; only its doc pointer clears).
    source_doc_id BIGINT REFERENCES documents(id) ON DELETE SET NULL
);

-- Component dependency graph (1-hop constraint propagation).
CREATE TABLE IF NOT EXISTS relationships (
    src_component_id BIGINT REFERENCES components(id),
    dst_component_id BIGINT REFERENCES components(id),
    kind             TEXT NOT NULL,       -- depends_on | calls | extends | owns | feeds | ...
    PRIMARY KEY (src_component_id, dst_component_id, kind)
);

-- Chunks for hybrid search (Tier 2).
CREATE TABLE IF NOT EXISTS chunks (
    id             BIGSERIAL PRIMARY KEY,
    document_id    BIGINT REFERENCES documents(id) ON DELETE CASCADE,
    component_id   BIGINT REFERENCES components(id),
    heading_path   TEXT,                  -- "Architecture > Auth > Tokens"
    ordinal        INT,
    content        TEXT NOT NULL,
    context_prefix TEXT,                  -- optional contextual-retrieval prefix
    embedding      vector(1024),          -- bge-m3 dense (filled by the embedder)
    -- Which model produced `embedding`. Vectors from different models are NOT comparable,
    -- and a silently-updated upstream tag would degrade cosine search with no error at all;
    -- stamping it per row makes a mismatch detectable and lets a re-embed target only the
    -- stale rows instead of the whole corpus.
    embedding_model TEXT,
    fts            tsvector,              -- composite <lang> || simple
    token_count    INT
);

CREATE INDEX IF NOT EXISTS chunks_fts_idx       ON chunks USING gin (fts);
CREATE INDEX IF NOT EXISTS chunks_component_idx ON chunks (component_id);
CREATE INDEX IF NOT EXISTS chunks_document_idx  ON chunks (document_id);
CREATE INDEX IF NOT EXISTS constraints_lookup_idx ON constraints (component_id, scope, status);
-- Build the ANN index AFTER the first bulk embed (faster than maintaining it during load):
--   CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);

-- Observability -- log searches to find recall gaps (n_results=0 = a miss).
CREATE TABLE IF NOT EXISTS query_log (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ DEFAULT now(),
    query      TEXT NOT NULL,
    n_results  INT,
    top_doc    TEXT
);
