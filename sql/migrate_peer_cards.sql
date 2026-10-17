-- Materialized peer cards derived from observer/observed perspective edges.
-- Cards contain navigation metadata only; memory content remains in mem.memories under RLS.

\set ON_ERROR_STOP on

DO $$ BEGIN
  CREATE ROLE hm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

BEGIN;
LOCK TABLE mem.memories, mem.entities IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE IF NOT EXISTS mem.peer_cards (
    author              text NOT NULL,
    peer_entity_id      bigint NOT NULL,
    active_count        bigint NOT NULL DEFAULT 0,
    observer_count      bigint NOT NULL DEFAULT 0,
    observed_count      bigint NOT NULL DEFAULT 0,
    memory_types        jsonb NOT NULL DEFAULT '{}'::jsonb,
    projects            text[] NOT NULL DEFAULT '{}',
    recent_memory_ids   bigint[] NOT NULL DEFAULT '{}',
    first_seen_at       timestamptz,
    last_seen_at        timestamptz,
    refreshed_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (author, peer_entity_id),
    FOREIGN KEY (author, peer_entity_id) REFERENCES mem.entities(author, id)
      ON DELETE CASCADE
);

CREATE OR REPLACE FUNCTION mem.refresh_peer_card(p_author text, p_peer bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,mem AS $$
BEGIN
  -- Serialize refreshes for one card. The trigger visits multi-card changes in stable order,
  -- so concurrent writes cannot overwrite a newer aggregate or deadlock on swapped endpoints.
  PERFORM pg_advisory_xact_lock(hashtextextended(p_author || ':' || p_peer::text,0));
  INSERT INTO mem.peer_cards(
      author,peer_entity_id,active_count,observer_count,observed_count,memory_types,
      projects,recent_memory_ids,first_seen_at,last_seen_at,refreshed_at)
  WITH card AS (
    SELECT * FROM mem.memories
     WHERE author=p_author AND status='active'
       AND (observer_entity_id=p_peer OR observed_entity_id=p_peer)
  ), totals AS (
    SELECT count(*) active_count,
           count(*) FILTER (WHERE observer_entity_id=p_peer) observer_count,
           count(*) FILTER (WHERE observed_entity_id=p_peer) observed_count,
           coalesce(array_agg(DISTINCT project ORDER BY project)
                    FILTER (WHERE project IS NOT NULL),'{}') projects,
           min(created_at) first_seen_at,max(created_at) last_seen_at
      FROM card
  )
  SELECT p_author,p_peer,t.active_count,t.observer_count,t.observed_count,
         coalesce((SELECT jsonb_object_agg(memory_type,n)
                     FROM (SELECT memory_type,count(*) n FROM card
                           GROUP BY memory_type) typed),'{}'),
         t.projects,
         coalesce((SELECT array_agg(id ORDER BY created_at DESC,id DESC)
                     FROM (SELECT id,created_at FROM card
                           ORDER BY created_at DESC,id DESC LIMIT 12) recent),'{}'),
         t.first_seen_at,t.last_seen_at,now()
    FROM totals t WHERE t.active_count > 0
  ON CONFLICT (author,peer_entity_id) DO UPDATE SET
      active_count=EXCLUDED.active_count, observer_count=EXCLUDED.observer_count,
      observed_count=EXCLUDED.observed_count, memory_types=EXCLUDED.memory_types,
      projects=EXCLUDED.projects, recent_memory_ids=EXCLUDED.recent_memory_ids,
      first_seen_at=EXCLUDED.first_seen_at, last_seen_at=EXCLUDED.last_seen_at,
      refreshed_at=EXCLUDED.refreshed_at;
  IF NOT FOUND THEN
    DELETE FROM mem.peer_cards WHERE author=p_author AND peer_entity_id=p_peer;
  END IF;
END $$;

CREATE OR REPLACE FUNCTION mem.refresh_memory_peer_cards() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,mem AS $$
DECLARE v_author text; v_peer bigint;
BEGIN
  IF TG_OP = 'INSERT' THEN
    FOR v_author,v_peer IN
      SELECT NEW.author,peer FROM (VALUES (NEW.observer_entity_id),(NEW.observed_entity_id)) p(peer)
       GROUP BY peer ORDER BY NEW.author,peer
    LOOP PERFORM mem.refresh_peer_card(v_author,v_peer); END LOOP;
  ELSIF TG_OP = 'DELETE' THEN
    FOR v_author,v_peer IN
      SELECT OLD.author,peer FROM (VALUES (OLD.observer_entity_id),(OLD.observed_entity_id)) p(peer)
       GROUP BY peer ORDER BY OLD.author,peer
    LOOP PERFORM mem.refresh_peer_card(v_author,v_peer); END LOOP;
  ELSE
    FOR v_author,v_peer IN
      SELECT author,peer FROM (VALUES
        (OLD.author,OLD.observer_entity_id),(OLD.author,OLD.observed_entity_id),
        (NEW.author,NEW.observer_entity_id),(NEW.author,NEW.observed_entity_id)) p(author,peer)
       GROUP BY author,peer ORDER BY author,peer
    LOOP PERFORM mem.refresh_peer_card(v_author,v_peer); END LOOP;
  END IF;
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS memories_peer_card_insert ON mem.memories;
DROP TRIGGER IF EXISTS memories_peer_card_delete ON mem.memories;
DROP TRIGGER IF EXISTS memories_peer_card_update ON mem.memories;
CREATE TRIGGER memories_peer_card_insert AFTER INSERT ON mem.memories
  FOR EACH ROW EXECUTE FUNCTION mem.refresh_memory_peer_cards();
CREATE TRIGGER memories_peer_card_delete AFTER DELETE ON mem.memories
  FOR EACH ROW EXECUTE FUNCTION mem.refresh_memory_peer_cards();
CREATE TRIGGER memories_peer_card_update
  AFTER UPDATE OF author,status,project,memory_type,observer_entity_id,observed_entity_id
  ON mem.memories FOR EACH ROW EXECUTE FUNCTION mem.refresh_memory_peer_cards();

-- Initial materialization.
SELECT mem.refresh_peer_card(p.author,p.peer)
FROM (SELECT author,observer_entity_id peer FROM mem.memories
      UNION SELECT author,observed_entity_id FROM mem.memories) p;

ALTER TABLE mem.peer_cards ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS peer_cards_own ON mem.peer_cards;
CREATE POLICY peer_cards_own ON mem.peer_cards FOR SELECT TO hm_app
  USING (author=current_setting('mem.reader',true));

GRANT SELECT ON mem.peer_cards TO hm_app;
REVOKE ALL ON FUNCTION mem.refresh_peer_card(text,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION mem.refresh_memory_peer_cards() FROM PUBLIC;

COMMIT;
