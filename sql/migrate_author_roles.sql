-- Enforced identity: one Postgres login per author.
--
-- On the shared `hm_app` login every caller names itself through the `mem.reader` setting, so
-- row-level security is a FILTER -- it stops a query that forgot the rule from over-reading, and
-- it stops nobody who holds the password from reading as somebody else. A login listed in
-- `mem.author_roles` (sql/mem_multiuser.sql) is different: `mem.reader()` maps the AUTHENTICATED
-- login to its author and ignores the setting, so the database itself decides who is reading.
--
-- This file adds the tooling around that map; the map and `mem.reader()` live next to the rule
-- they feed, in mem_multiuser.sql, so that re-applying that file can never leave the rule
-- pointing at an identity function that does not exist yet.
--
--   SELECT mem.add_author_role('alice');          -- creates login hm_alice, maps it to alice
--   SELECT mem.add_author_role('alice', 'alice_ci'); -- a second login for the same author
--   SELECT mem.remove_author_role('hm_alice');    -- unmaps it, revokes hm_app, NOLOGIN
--   SELECT * FROM mem.whoami();                   -- any app login: who the database says I am
--
-- `./hm author add <name>` wraps the first and prints how to set the password. The new login
-- inherits hm_app's privileges (GRANT hm_app TO <role>), so the policies written for hm_app
-- bind it too. Run as the owner/maintenance role. Idempotent: safe to re-run.

\set ON_ERROR_STOP on

-- The shared login cannot also be an author login: mapping hm_app would make every holder of
-- its password that author, which is the opposite of what the map is for.
DO $$ BEGIN
  ALTER TABLE mem.author_roles ADD CONSTRAINT author_roles_not_shared_login
    CHECK (role <> 'hm_app');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS author_roles_author_idx ON mem.author_roles(author);

-- The caller's OWN mapping, and nothing else. `mode` is
--   enforced -- this login is mapped; `author` is fixed by the database;
--   filter   -- shared login (or a role that bypasses row-level security anyway); `author` is
--               whatever mem.reader was set to;
--   unmapped -- any other login, including one whose mapping was removed: it reads nothing
--               until it is mapped.
CREATE OR REPLACE FUNCTION mem.whoami(OUT login name, OUT author text, OUT mode text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, mem AS $$
BEGIN
  login := session_user;
  author := mem.reader();
  IF EXISTS (SELECT 1 FROM mem.author_roles ar WHERE ar.role = session_user) THEN
    mode := 'enforced';
  ELSIF mem.trusts_reader_setting(session_user) THEN
    mode := 'filter';
  ELSE
    mode := 'unmapped';
  END IF;
END $$;
REVOKE ALL ON FUNCTION mem.whoami() FROM PUBLIC;

-- Create (or adopt) a login for an author and map it. Returns the role name. Refuses a role
-- that would bypass row-level security -- a superuser, a BYPASSRLS role, the memory table's
-- owner, or a member of any of them -- because mapping it would announce an enforcement that
-- does not happen.
CREATE OR REPLACE FUNCTION mem.add_author_role(p_author text, p_role name DEFAULT NULL)
RETURNS name LANGUAGE plpgsql
SET search_path = pg_catalog, mem AS $$
DECLARE
  r name;
  existing text;
  tbl_owner oid;
BEGIN
  IF p_author IS NULL OR p_author !~ '^[A-Za-z0-9._-]{1,64}$' THEN
    RAISE EXCEPTION 'author % is not a plain name ([A-Za-z0-9._-], up to 64)', p_author;
  END IF;
  r := coalesce(p_role, ('hm_' || lower(regexp_replace(p_author, '[^A-Za-z0-9_]', '_', 'g')))::name);
  IF r = 'hm_app' THEN
    RAISE EXCEPTION 'hm_app is the shared login; an author login needs its own name';
  END IF;
  SELECT ar.author INTO existing FROM mem.author_roles ar WHERE ar.role = r;
  IF FOUND AND existing <> p_author THEN
    RAISE EXCEPTION 'role % is already mapped to author %', r, existing;
  END IF;
  IF to_regrole('hm_app') IS NULL THEN
    RAISE EXCEPTION 'role hm_app does not exist -- apply the migrations first (./hm migrate)';
  END IF;
  SELECT relowner INTO tbl_owner FROM pg_class WHERE oid = 'mem.memories'::regclass;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('CREATE ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE '
                   'NOREPLICATION NOBYPASSRLS', r);
  ELSE
    -- 'MEMBER', not 'USAGE': a membership granted WITH INHERIT FALSE, SET TRUE gives no
    -- inherited privileges but still lets the login SET ROLE to the owner, or to any
    -- superuser or BYPASSRLS role it can reach, and skip every policy from there.
    IF EXISTS (SELECT 1 FROM pg_roles g
                WHERE (g.rolsuper OR g.rolbypassrls OR g.oid = tbl_owner)
                  AND pg_has_role(r, g.oid, 'MEMBER')) THEN
      RAISE EXCEPTION 'role % bypasses row-level security (superuser, BYPASSRLS or table '
                      'owner) and cannot be an author login', r;
    END IF;
    EXECUTE format('ALTER ROLE %I LOGIN INHERIT', r);
  END IF;
  EXECUTE format('GRANT hm_app TO %I', r);
  INSERT INTO mem.author_roles(role, author) VALUES (r, p_author)
    ON CONFLICT (role) DO NOTHING;
  RETURN r;
END $$;
REVOKE ALL ON FUNCTION mem.add_author_role(text, name) FROM PUBLIC;

-- Unmap a login. It also loses hm_app and the right to log in: an unmapped member of hm_app
-- already reads nothing (mem.reader() returns NULL for it), and this makes that two locks.
CREATE OR REPLACE FUNCTION mem.remove_author_role(p_role name)
RETURNS boolean LANGUAGE plpgsql
SET search_path = pg_catalog, mem AS $$
BEGIN
  DELETE FROM mem.author_roles WHERE role = p_role;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = p_role) THEN
    IF to_regrole('hm_app') IS NOT NULL THEN
      EXECUTE format('REVOKE hm_app FROM %I', p_role);
    END IF;
    EXECUTE format('ALTER ROLE %I NOLOGIN', p_role);
  END IF;
  RETURN true;
END $$;
REVOKE ALL ON FUNCTION mem.remove_author_role(name) FROM PUBLIC;
