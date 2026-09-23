-- One reading of the store. Every figure below that says "active" must mean the same thing, and
-- the thing it means is mem.active_memories: status='active' AND inside its validity window.
-- `count(*) FILTER (WHERE status='active')` over mem.memories is NOT that -- a fact past its
-- valid_to keeps status='active' for ever (nothing sweeps it), so the headline counted memories
-- no retrieval path in this system can return, above breakdowns that did not.
SELECT json_build_object(
 'memories', (SELECT json_build_object(
     'total', count(*),
     'active', (SELECT count(*) FROM mem.active_memories),
     'by_type', (SELECT coalesce(json_object_agg(t, n),'{}') FROM (
         SELECT memory_type::text AS t, count(*) AS n FROM mem.active_memories GROUP BY 1) x),
     'by_project', (SELECT coalesce(json_agg(json_build_object('project', coalesce(project,'(none)'), 'n', n) ORDER BY n DESC),'[]')
                    FROM (SELECT project, count(*) AS n FROM mem.active_memories GROUP BY 1) y),
     'pages', (SELECT count(*) FROM mem.active_memories WHERE metadata->>'kind'='page')
   ) FROM mem.memories),
 'review_queue', (SELECT json_build_object(
     'pending', count(*),
     'oldest_days', coalesce(max(EXTRACT(day FROM now()-created_at))::int, 0)
   ) FROM mem.review_queue WHERE status='pending'),
 -- The same definition hooks/mem_profile.py and mem_ops.py's stale list use, last_accessed_at
 -- clause included. Without it this counted memories the agent recalled yesterday, while the
 -- console printed a note promising the two surfaces differ only in the day threshold.
 'stale', (SELECT count(*) FROM mem.active_memories
            WHERE created_at < now() - interval '180 days'
              AND memory_type IN ('semantic','episodic','prospective')
              AND (metadata->>'kind') IS DISTINCT FROM 'page'
              AND (last_accessed_at IS NULL
                   OR last_accessed_at < now() - interval '180 days')),
 'corpus', (SELECT coalesce(json_agg(json_build_object(
       'repo', repo, 'docs', docs, 'chunks', chunks, 'embedded', embedded) ORDER BY repo),'[]')
     FROM (SELECT d.repo, count(DISTINCT d.id) AS docs, count(c.id) AS chunks,
                  count(c.embedding) AS embedded
             FROM documents d LEFT JOIN chunks c ON c.document_id=d.id GROUP BY d.repo) z),
 -- A list with a possibly-null 'model', not a {model: count} map. The map keyed NULL as the
 -- string '(null)', so a model literally named that would have had its chunks counted together
 -- with the ones that have no model recorded -- two different things under one healthy number.
 'embedding_models', (SELECT coalesce(json_agg(json_build_object('model', embedding_model, 'n', n) ORDER BY n DESC),'[]')
     FROM (SELECT embedding_model, count(*) AS n FROM chunks WHERE embedding IS NOT NULL GROUP BY 1) w),
 'components', (SELECT coalesce(json_object_agg(repo, n),'{}')
     FROM (SELECT repo, count(*) AS n FROM components GROUP BY 1) v),
 'db_size', pg_size_pretty(pg_database_size(current_database()))
);
