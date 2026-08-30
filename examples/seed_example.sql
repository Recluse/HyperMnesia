-- Example structural-tier seed (Tier 0/1) for a fictional repo "myapp".
-- Copy this, replace with YOUR components/constraints, and load:
--   psql "$DATABASE_URL" -f examples/seed_example.sql
--
-- The value: `key_paths` globs let the path->component resolver answer "what rules apply to
-- this file?" deterministically, and `relationships` propagate constraints one hop along the
-- dependency graph. Constraints are your invariants ("must") and conventions ("should"); link
-- each to the doc that states it (source_doc_id) so freshness checks can flag drift.
-- Repo-scoped + idempotent: resets only repo='myapp'.

BEGIN;
DELETE FROM constraints   WHERE repo='myapp';
DELETE FROM relationships  WHERE src_component_id IN (SELECT id FROM components WHERE repo='myapp');
DELETE FROM components      WHERE repo='myapp';

-- Parent (organizational; empty key_paths -> not path-resolved, just groups the map).
INSERT INTO components (repo,slug,name,parent_id,responsibility,key_paths,priority,owner) VALUES
('myapp','myapp-root','myapp',NULL,'Top-level grouping for the example service.','{}'::text[],10,'you');

-- Leaves (path-resolved via key_paths globs; higher priority wins on glob overlap).
INSERT INTO components (repo,slug,name,parent_id,responsibility,key_paths,priority,owner) VALUES
('myapp','myapp-api','HTTP API',(SELECT id FROM components WHERE slug='myapp-root' AND repo='myapp'),
 'Request handlers, routing, auth middleware. The only public entrypoint.',
 ARRAY['src/api/**','src/routes/**'],30,'you'),
('myapp','myapp-db','Data layer',(SELECT id FROM components WHERE slug='myapp-root' AND repo='myapp'),
 'Models, migrations, the single place that talks to Postgres.',
 ARRAY['src/db/**','migrations/**'],30,'you'),
('myapp','myapp-docs','Docs',(SELECT id FROM components WHERE slug='myapp-root' AND repo='myapp'),
 'Architecture and API documentation.',ARRAY['docs/**'],15,'you');

-- Relationships (dependency graph; kind is free text: depends_on | calls | owns | ...).
INSERT INTO relationships (src_component_id,dst_component_id,kind)
SELECT (SELECT id FROM components WHERE slug=s AND repo='myapp'),
       (SELECT id FROM components WHERE slug=d AND repo='myapp'), k
FROM (VALUES ('myapp-api','myapp-db','depends_on')) AS t(s,d,k);

-- Constraints (invariant | convention; severity must | should | info; global or a component).
INSERT INTO constraints (repo,kind,scope,component_id,title,statement,rationale,severity,source_doc_id) VALUES
('myapp','invariant','global',NULL,'All DB access goes through the data layer',
 'No component other than myapp-db may import the Postgres driver or run raw SQL.',
 'One place to reason about queries, migrations, and connection pooling.','must',
 (SELECT id FROM documents WHERE repo='myapp' AND path='docs/architecture.md')),
('myapp','convention','component',(SELECT id FROM components WHERE slug='myapp-api' AND repo='myapp'),
 'Handlers return the response envelope',
 'API handlers must return the shared {data, error} envelope, never a bare value.',
 'Consistent client-side error handling.','should',
 (SELECT id FROM documents WHERE repo='myapp' AND path='docs/architecture.md'));
COMMIT;
