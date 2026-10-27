#!/usr/bin/env python3
"""Row-level security and identity, proved against a live Postgres -- as the roles that use it.

The memory store has two trust modes (docs/DESIGN.md, "Trust"), and this test runs both:

  * the shared `hm_app` login, where the caller names itself with the `mem.reader` setting.
    RLS is a FILTER there: two readers on one login must see different rows, a write in
    somebody else's name must be refused -- and nothing stops a caller from choosing a name;
  * per-author logins (`mem.author_roles`, `./hm author add`), where the database maps the
    authenticated login to its author and IGNORES the setting. A login that sets `mem.reader`
    to somebody else's name must still see only its own author's rows.

Every check connects as the role under test. The earlier probe ran as the table OWNER, which
bypasses every policy, so it proved what the views filter and nothing about the policies -- it
kept passing with the `mem_read` policy dropped.

It also checks the SECURITY DEFINER functions (they run as the owner, so each must re-check the
caller through mem.reader() itself), that the application roles cannot read the author
directory, and that `./hm migrate` is a no-op on a second run and refuses a changed file.

    DATABASE_URL=postgresql://<owner>@host/db        # the table owner: sets up and cleans up
    HM_APP_URL=postgresql://hm_app:<pw>@host/db      # the shared application login
    python3 tests/test_rls_live.py

Run it against a THROWAWAY database: it creates (and drops) two login roles with random names
and writes rows it deletes again. Without a database it is skipped; under CI it fails instead.
"""
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HM = os.path.join(ROOT, "hm")
OWNER_URL = os.environ.get("DATABASE_URL", "")
APP_URL = os.environ.get("HM_APP_URL", "")

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run_sql(url, sql, reader=None):
    """(ok, stdout, stderr) of one fresh session. `reader` goes in as the connection option,
    exactly the way the hooks pass it."""
    env = dict(os.environ)
    env.pop("PGOPTIONS", None)
    if reader is not None:
        env["PGOPTIONS"] = f"-c mem.reader={reader}"
    p = subprocess.run(["psql", url, "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"],
                       input=sql.encode(), capture_output=True, timeout=60, env=env)
    return p.returncode == 0, p.stdout.decode().strip(), p.stderr.decode().strip()


def must(url, sql, reader=None):
    ok, out, err = run_sql(url, sql, reader)
    if not ok:
        raise SystemExit(f"setup statement failed: {err}\n--- sql ---\n{sql}")
    return out


def as_login(url, user, password):
    u = urlsplit(url)
    host = u.hostname or "localhost"
    netloc = f"{quote(user)}:{quote(password)}@{host}" + (f":{u.port}" if u.port else "")
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


class Fixture:
    def __init__(self):
        t = secrets.token_hex(4)
        self.tag = t
        self.a, self.b, self.c = f"rlsa{t}", f"rlsb{t}", f"rlsc{t}"
        self.p = f"rlsp{t}"
        self.role_a, self.role_b, self.role_x = f"hm_rlst_a{t}", f"hm_rlst_b{t}", f"hm_rlst_x{t}"
        # g: a group holding hm_app; y: a login that reaches hm_app only through g;
        # r: an author login revoked in the middle of a session; s: a login that can SET ROLE
        # to the table owner without inheriting it.
        self.role_g, self.role_y = f"hm_rlst_g{t}", f"hm_rlst_y{t}"
        self.role_r, self.role_s = f"hm_rlst_r{t}", f"hm_rlst_s{t}"
        self.pw = {r: secrets.token_hex(12)
                   for r in (self.role_a, self.role_b, self.role_x, self.role_y, self.role_r,
                             self.role_s)}
        self.ids = {}

    def setup(self):
        meta = f"'{{\"rlstest\":\"{self.tag}\"}}'::jsonb"
        must(OWNER_URL, f"""
          INSERT INTO mem.project_members (author, project)
               VALUES ('{self.a}', '{self.p}'), ('{self.b}', '{self.p}');
          SELECT mem.add_author_role('{self.a}', '{self.role_a}');
          SELECT mem.add_author_role('{self.b}', '{self.role_b}');
          ALTER ROLE {self.role_a} PASSWORD '{self.pw[self.role_a]}';
          ALTER ROLE {self.role_b} PASSWORD '{self.pw[self.role_b]}';
          -- a member of hm_app with NO mapping: must read nothing, not fall back to the setting
          CREATE ROLE {self.role_x} LOGIN INHERIT PASSWORD '{self.pw[self.role_x]}';
          GRANT hm_app TO {self.role_x};
          -- the same, one group removed: membership through a group must not reopen the setting
          CREATE ROLE {self.role_g} NOLOGIN;
          GRANT hm_app TO {self.role_g};
          CREATE ROLE {self.role_y} LOGIN INHERIT PASSWORD '{self.pw[self.role_y]}';
          GRANT {self.role_g} TO {self.role_y};
          SELECT mem.add_author_role('{self.a}', '{self.role_r}');
          ALTER ROLE {self.role_r} PASSWORD '{self.pw[self.role_r]}';
        """)
        rows = must(OWNER_URL, f"""
          INSERT INTO mem.memories (memory_type, content, author, scope, project, metadata)
          VALUES ('semantic', 'a_priv', '{self.a}', 'private', '{self.p}', {meta}),
                 ('semantic', 'a_proj', '{self.a}', 'project', '{self.p}', {meta}),
                 ('semantic', 'b_priv', '{self.b}', 'private', '{self.p}', {meta}),
                 ('semantic', 'b_proj', '{self.b}', 'project', '{self.p}', {meta}),
                 ('semantic', 'c_priv', '{self.c}', 'private', NULL,        {meta})
          RETURNING content || ' ' || id;""")
        for line in rows.splitlines():
            name, mid = line.split()
            self.ids[name] = int(mid)
        must(OWNER_URL, f"""
          INSERT INTO mem.sources (memory_id, source_type, excerpt)
               VALUES ({self.ids['b_proj']}, 'manual', 'b_proj source');
          INSERT INTO mem.assertions (memory_id, predicate, object_text)
               VALUES ({self.ids['b_proj']}, 'is', 'b_proj assertion');""")
        self.url_a = as_login(OWNER_URL, self.role_a, self.pw[self.role_a])
        self.url_b = as_login(OWNER_URL, self.role_b, self.pw[self.role_b])
        self.url_x = as_login(OWNER_URL, self.role_x, self.pw[self.role_x])
        self.url_y = as_login(OWNER_URL, self.role_y, self.pw[self.role_y])
        self.url_r = as_login(OWNER_URL, self.role_r, self.pw[self.role_r])

    def teardown(self):
        authors = f"'{self.a}','{self.b}','{self.c}'"
        run_sql(OWNER_URL, f"""
          DELETE FROM mem.memories WHERE author IN ({authors});
          DELETE FROM mem.dream_runs WHERE author IN ({authors});
          DELETE FROM mem.review_queue WHERE author IN ({authors});
          DELETE FROM mem.entities WHERE author IN ({authors});
          DELETE FROM mem.project_members WHERE project = '{self.p}';
          SELECT mem.remove_author_role('{self.role_a}');
          SELECT mem.remove_author_role('{self.role_b}');
          DROP ROLE IF EXISTS {self.role_a};
          DROP ROLE IF EXISTS {self.role_b};
          DROP ROLE IF EXISTS {self.role_x};
          SELECT mem.remove_author_role('{self.role_r}');
          DROP ROLE IF EXISTS {self.role_r};
          DROP ROLE IF EXISTS {self.role_y};
          DROP ROLE IF EXISTS {self.role_g};
          DROP ROLE IF EXISTS {self.role_s};
        """)


def visible(fx, url, reader=None, relation="mem.memories", prefix=""):
    ok, out, err = run_sql(url, f"{prefix}SELECT content FROM {relation} "
                                f"WHERE metadata->>'rlstest' = '{fx.tag}' ORDER BY content;",
                           reader)
    return set(out.split()) if ok else {"ERROR: " + err}


def scalar(url, sql, reader=None):
    ok, out, err = run_sql(url, sql, reader)
    return out if ok else "ERROR: " + err


def refused(url, sql, reader=None, needle=""):
    ok, _, err = run_sql(url, sql, reader)
    return (not ok) and (needle.lower() in err.lower()), err


def shared_login(fx):
    print("== shared login hm_app: the setting chooses the reader (filter mode) ==")
    A = {"a_priv", "a_proj", "b_proj"}
    B = {"b_priv", "a_proj", "b_proj"}
    check("reader A sees its own rows and the project's shared rows",
          visible(fx, APP_URL, fx.a) == A, str(visible(fx, APP_URL, fx.a)))
    check("reader B sees its own rows and the project's shared rows, not A's private one",
          visible(fx, APP_URL, fx.b) == B, str(visible(fx, APP_URL, fx.b)))
    check("a non-member sees its own private row and no project rows",
          visible(fx, APP_URL, fx.c) == {"c_priv"}, str(visible(fx, APP_URL, fx.c)))
    check("no reader at all sees nothing", visible(fx, APP_URL) == set(),
          str(visible(fx, APP_URL)))
    check("the active view applies the same rule",
          visible(fx, APP_URL, fx.a, "mem.active_memories") == A)
    check("whoami reports filter mode with the chosen name",
          scalar(APP_URL, "SELECT login, author, mode FROM mem.whoami();", fx.a)
          == f"hm_app|{fx.a}|filter")

    print("== shared login hm_app: what it may not reach ==")
    for rel in ("mem.author_roles", "mem.project_members", "mem.all_active_memories"):
        ok, err = refused(APP_URL, f"SELECT count(*) FROM {rel};", fx.a, "permission denied")
        check(f"cannot read {rel}", ok, err)
    check("its own memberships come from mem.reader_projects(), and only its own",
          scalar(APP_URL, "SELECT mem.reader_projects();", fx.c) == "{}"
          and fx.p in scalar(APP_URL, "SELECT mem.reader_projects();", fx.a))
    ok, err = refused(APP_URL, f"SELECT mem.add_author_role('{fx.c}');", fx.a,
                      "permission denied")
    check("cannot create author logins", ok, err)
    ok, err = refused(APP_URL, "SELECT mem.refresh_peer_card('x', 1);", fx.a,
                      "permission denied")
    check("cannot call the peer-card refresher directly", ok, err)

    print("== shared login hm_app: writes ==")
    ok, err = refused(APP_URL, f"INSERT INTO mem.memories (memory_type, content, author, metadata) "
                               f"VALUES ('semantic', 'forged', '{fx.b}', "
                               f"'{{\"rlstest\":\"{fx.tag}\"}}');", fx.a, "row-level security")
    check("a write in another author's name is refused", ok, err)
    ok, err = refused(APP_URL, f"INSERT INTO mem.memories (memory_type, content, author, scope, "
                               f"project, metadata) VALUES ('semantic', 'outsider', '{fx.c}', "
                               f"'project', '{fx.p}', '{{\"rlstest\":\"{fx.tag}\"}}');",
                      fx.c, "row-level security")
    check("publishing into a project the writer is not in is refused", ok, err)
    n = scalar(APP_URL, f"WITH u AS (UPDATE mem.memories SET content = 'hijacked' "
                        f"WHERE id IN ({fx.ids['b_proj']}, {fx.ids['b_priv']}) RETURNING 1) "
                        f"SELECT count(*) FROM u;", fx.a)
    check("another author's rows cannot be updated, even shared ones", n == "0", n)
    n = scalar(APP_URL, f"WITH d AS (DELETE FROM mem.memories WHERE id = {fx.ids['b_proj']} "
                        f"RETURNING 1) SELECT count(*) FROM d;", fx.a)
    check("nor deleted", n == "0", n)
    ok, err = refused(APP_URL, f"INSERT INTO mem.review_queue (action, member_ids, proposal, "
                               f"confidence, author) VALUES ('merge', '{{1}}', '{{}}', 0.9, "
                               f"'{fx.b}');", fx.a, "row-level security")
    check("a review item in another author's name is refused", ok, err)


def per_author(fx):
    print("== per-author login: the database decides who is reading ==")
    A = {"a_priv", "a_proj", "b_proj"}
    B = {"b_priv", "a_proj", "b_proj"}
    check("login A with no setting reads as A", visible(fx, fx.url_a) == A,
          str(visible(fx, fx.url_a)))
    seen = visible(fx, fx.url_a, fx.b)
    check("login A claiming to be B still reads as A -- B's private row stays hidden",
          seen == A, str(seen))
    seen = visible(fx, fx.url_a, fx.c)
    check("login A claiming a non-member's name still sees A's project", seen == A, str(seen))
    seen = visible(fx, fx.url_a, prefix=f"SET mem.reader = '{fx.b}'; ")
    check("setting mem.reader inside the session changes nothing", seen == A, str(seen))
    seen = visible(fx, fx.url_a, prefix=f"SET ROLE hm_app; SET mem.reader = '{fx.b}'; ")
    check("nor does SET ROLE hm_app -- identity follows the authenticated login",
          seen == A, str(seen))
    seen = visible(fx, fx.url_b, fx.a)
    check("login B claiming to be A reads as B", seen == B, str(seen))
    check("whoami reports enforced mode and ignores the claimed name",
          scalar(fx.url_a, "SELECT login, author, mode FROM mem.whoami();", fx.b)
          == f"{fx.role_a}|{fx.a}|enforced")
    seen = visible(fx, fx.url_x, fx.a)
    check("a member of hm_app with no mapping reads nothing, whatever it claims",
          seen == set(), str(seen))
    check("and says so", scalar(fx.url_x, "SELECT mode FROM mem.whoami();", fx.a) == "unmapped")
    seen = visible(fx, fx.url_y, fx.b)
    check("a login reaching hm_app only through a group reads nothing, whatever it claims",
          seen == set(), str(seen))
    check("and is reported unmapped, not filter",
          scalar(fx.url_y, "SELECT mode FROM mem.whoami();", fx.b) == "unmapped")
    revoked_mid_session(fx)

    print("== per-author login: another author's provenance ==")
    src = (f"SELECT (SELECT count(*) FROM mem.sources WHERE memory_id = {fx.ids['b_proj']}) "
           f"|| '/' || (SELECT count(*) FROM mem.assertions WHERE memory_id = {fx.ids['b_proj']});")
    check("login A reads the sources and assertions of B's shared row",
          scalar(fx.url_a, src) == "1/1", scalar(fx.url_a, src))
    for t in ("sources", "assertions"):
        n = scalar(fx.url_a, f"WITH d AS (DELETE FROM mem.{t} WHERE memory_id = "
                             f"{fx.ids['b_proj']} RETURNING 1) SELECT count(*) FROM d;")
        check(f"but cannot delete its {t}", n == "0", n)
        n = scalar(fx.url_a, f"WITH u AS (UPDATE mem.{t} SET memory_id = {fx.ids['a_priv']} "
                             f"WHERE memory_id = {fx.ids['b_proj']} RETURNING 1) "
                             f"SELECT count(*) FROM u;")
        check(f"nor move its {t} onto a row of its own", n == "0", n)
    check("both are still there", scalar(OWNER_URL, src) == "1/1", scalar(OWNER_URL, src))

    for rel in ("mem.author_roles", "mem.project_members"):
        ok, err = refused(fx.url_a, f"SELECT count(*) FROM {rel};", None, "permission denied")
        check(f"login A cannot read {rel}", ok, err)

    print("== per-author login: writes ==")
    ok, err = refused(fx.url_a, f"INSERT INTO mem.memories (memory_type, content, author, metadata) "
                                f"VALUES ('semantic', 'forged', '{fx.b}', "
                                f"'{{\"rlstest\":\"{fx.tag}\"}}');", fx.b, "row-level security")
    check("login A writing as B, with mem.reader=B, is refused", ok, err)
    out = scalar(fx.url_a, f"INSERT INTO mem.memories (memory_type, content, author, metadata) "
                           f"VALUES ('semantic', 'a_new', '{fx.a}', "
                           f"'{{\"rlstest\":\"{fx.tag}\"}}') RETURNING author;", fx.b)
    check("login A writing as A succeeds whatever the setting says", out == fx.a, out)
    n = scalar(fx.url_a, f"WITH u AS (UPDATE mem.memories SET content = 'hijacked' "
                         f"WHERE id = {fx.ids['b_proj']} RETURNING 1) SELECT count(*) FROM u;",
               fx.b)
    check("login A cannot update B's shared row while claiming to be B", n == "0", n)
    ok, err = refused(fx.url_a, f"INSERT INTO mem.dream_runs (author, observer_entity_id, "
                                f"observed_entity_id) SELECT author, id, id FROM mem.entities "
                                f"LIMIT 1; UPDATE mem.dream_runs SET author = '{fx.b}' "
                                f"WHERE author = '{fx.a}';", fx.b, "row-level security")
    check("a Dreamer run cannot be moved into B's name", ok, err)


def revoked_mid_session(fx):
    """An author login that ran SET ROLE hm_app keeps hm_app as its current role after the
    mapping and the membership are removed; it must not fall back to naming itself."""
    env = dict(os.environ)
    env.pop("PGOPTIONS", None)
    p = subprocess.Popen(["psql", fx.url_r, "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=env)
    try:
        p.stdin.write("SET ROLE hm_app; SELECT 'ready';\n")
        p.stdin.flush()
        ready = p.stdout.readline().strip()
        must(OWNER_URL, f"SELECT mem.remove_author_role('{fx.role_r}');")
        p.stdin.write(f"SET mem.reader = '{fx.b}'; SELECT count(*) FROM mem.memories "
                      f"WHERE id = {fx.ids['b_priv']}; SELECT coalesce(mode, '') "
                      f"FROM mem.whoami();\n")
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    lines = out.split()
    check("a login revoked mid-session (after SET ROLE hm_app) cannot read as B",
          ready == "ready" and lines[:1] == ["0"], f"ready={ready!r} out={out!r} err={err!r}")
    check("and is reported unmapped", lines[1:2] == ["unmapped"], repr(out))


def definer_functions(fx):
    print("== SECURITY DEFINER functions re-check the caller through mem.reader() ==")
    ids = f"ARRAY[{fx.ids['b_priv']}, {fx.ids['a_priv']}, {fx.ids['b_proj']}]::bigint[]"
    n = scalar(fx.url_a, f"SELECT mem.touch_memory_access({ids});", fx.b)
    check("touch_memory_access as login A (claiming B) counts only what A may read", n == "2", n)
    counts = scalar(OWNER_URL, f"SELECT string_agg(content || '=' || access_count, ',' "
                               f"ORDER BY content) FROM mem.memories "
                               f"WHERE id = ANY({ids});")
    check("B's private row was not touched", "b_priv=0" in counts and "a_priv=1" in counts,
          counts)
    n = scalar(APP_URL, f"SELECT mem.touch_memory_access(ARRAY[{fx.ids['a_proj']}]::bigint[]);",
               fx.c)
    check("and on the shared login a non-member touches nothing", n == "0", n)

    ok, err = refused(fx.url_a, f"INSERT INTO mem.derivations (author, evidence_memory_id, "
                                f"derived_memory_id, relation) VALUES ('{fx.a}', "
                                f"{fx.ids['b_priv']}, {fx.ids['a_priv']}, 'supports');",
                      fx.b, "not visible")
    check("guard_derivation refuses evidence the login cannot read, even claiming its owner",
          ok, err)
    out = scalar(fx.url_a, f"INSERT INTO mem.derivations (author, evidence_memory_id, "
                           f"derived_memory_id, relation) VALUES ('{fx.a}', {fx.ids['b_proj']}, "
                           f"{fx.ids['a_priv']}, 'supports') RETURNING relation;", fx.b)
    check("and accepts shared evidence it can read", out == "supports", out)
    ok, err = refused(fx.url_a, f"INSERT INTO mem.derivations (author, evidence_memory_id, "
                                f"derived_memory_id, relation) VALUES ('{fx.b}', "
                                f"{fx.ids['a_proj']}, {fx.ids['b_priv']}, 'supports');",
                      fx.b, "row-level security")
    check("an edge in B's name is refused for login A", ok, err)
    edge = (f"SELECT count(*) FROM mem.derivations WHERE derived_memory_id = "
            f"{fx.ids['a_priv']};")
    check("the edge is visible to A", scalar(fx.url_a, edge) == "1")
    check("and hidden from B, who cannot read its derived end", scalar(fx.url_b, edge, fx.a) == "0")

    got = scalar(fx.url_a, f"SELECT string_agg(m.content, ',' ORDER BY m.content) "
                           f"FROM mem.perspective_graph g JOIN mem.memories m "
                           f"ON m.id = g.memory_id WHERE m.metadata->>'rlstest' = '{fx.tag}' "
                           f"AND m.content <> 'a_new';", fx.b)
    check("perspective_graph (owner-run view) shows login A only A's readable rows",
          got == "a_priv,a_proj,b_proj", got)
    got = scalar(fx.url_a, f"SELECT count(*) FILTER (WHERE author = '{fx.a}') || '/' || "
                           f"count(*) FROM mem.peer_cards WHERE author IN "
                           f"('{fx.a}', '{fx.b}', '{fx.c}');", fx.b)
    mine, total = (got.split("/") + ["", ""])[:2]
    check("peer_cards shows login A its own cards only", mine == total and mine not in ("", "0"),
          got)


def admin_helpers(fx):
    print("== author-login administration refuses what would not be enforced ==")
    ok, err = refused(OWNER_URL, "SELECT mem.add_author_role('someone', 'hm_app');", None,
                      "shared login")
    check("hm_app itself cannot be mapped", ok, err)
    owner = scalar(OWNER_URL, "SELECT current_user;")
    ok, err = refused(OWNER_URL, f"SELECT mem.add_author_role('someone', '{owner}');", None,
                      "bypasses row-level security")
    check("a role that bypasses RLS cannot become an author login", ok, err)
    must(OWNER_URL, f"CREATE ROLE {fx.role_s} LOGIN PASSWORD '{fx.pw[fx.role_s]}'; "
                    f"GRANT \"{owner}\" TO {fx.role_s} WITH INHERIT FALSE, SET TRUE;")
    ok, err = refused(OWNER_URL, f"SELECT mem.add_author_role('{fx.c}', '{fx.role_s}');", None,
                      "bypasses row-level security")
    check("nor a role that can only SET ROLE to the owner (no inherited privileges)", ok, err)
    ok, err = refused(OWNER_URL, f"SELECT mem.add_author_role('{fx.c}', '{fx.role_a}');", None,
                      "already mapped")
    check("a login cannot be remapped to a second author", ok, err)


def hm(*args, env_extra=None):
    env = dict(os.environ, DATABASE_URL=OWNER_URL)
    env.pop("HM_ADMIN_URL", None)
    env.update(env_extra or {})
    p = subprocess.run(["sh", HM, *args], capture_output=True, text=True, timeout=300, env=env)
    return p.returncode, p.stdout + p.stderr


def migrations():
    print("== hm migrate: recorded, idempotent, and loud about edited files ==")
    code, out = hm("migrate", "--owner", "ci")
    check("a run over an up-to-date store applies nothing", code == 0 and "nothing to apply" in out,
          out[-400:])
    before = scalar(OWNER_URL, "SELECT checksum || ' ' || applied_at FROM hm_schema_migrations "
                               "WHERE name = 'migrate_dreamer.sql';")
    with tempfile.TemporaryDirectory() as tmp:
        sql = os.path.join(tmp, "sql")
        shutil.copytree(os.path.join(ROOT, "sql"), sql)
        with open(os.path.join(sql, "migrate_dreamer.sql"), "a", encoding="utf-8") as f:
            f.write("\n-- edited after it was applied\n")
        code, out = hm("migrate", env_extra={"HM_SQL_DIR": sql})
        check("an edited, already-applied file stops the run", code != 0
              and "migrate_dreamer.sql" in out and "nothing was applied" in out, out[-400:])
        after = scalar(OWNER_URL, "SELECT checksum || ' ' || applied_at FROM hm_schema_migrations "
                                  "WHERE name = 'migrate_dreamer.sql';")
        check("and nothing was re-applied or re-recorded", before == after, f"{before} -> {after}")
        code, out = hm("migrate", "--status", env_extra={"HM_SQL_DIR": sql})
        check("--status names it as CHANGED", "migrate_dreamer.sql" in out and "CHANGED" in out,
              out[-400:])
        code, out = hm("migrate", "--reapply", "migrate_dreamer.sql", env_extra={"HM_SQL_DIR": sql})
        check("--reapply re-runs it, and the grant file after it",
              code == 0 and "applying sql/migrate_dreamer.sql" in out
              and "applying sql/mem_app_role.sql" in out, out[-400:])
    code, out = hm("migrate")
    check("the original file now reads as changed in its turn", code != 0, out[-300:])
    code, out = hm("migrate", "--reapply", "migrate_dreamer.sql")
    check("and --reapply puts it back", code == 0, out[-300:])
    code, out = hm("migrate")
    check("after which the store is current again", code == 0 and "nothing to apply" in out,
          out[-300:])
    with tempfile.TemporaryDirectory() as tmp:
        # A run that dies after re-applying an earlier file and before the grant file: the
        # retry must still owe the grant file, not find every checksum current.
        sql = os.path.join(tmp, "sql")
        shutil.copytree(os.path.join(ROOT, "sql"), sql)
        with open(os.path.join(sql, "mem_app_role.sql"), "a", encoding="utf-8") as f:
            f.write("\nSELECT 1/0;  -- the run is interrupted here\n")
        code, out = hm("migrate", "--reapply", "migrate_peer_cards.sql",
                       env_extra={"HM_SQL_DIR": sql})
        check("a run interrupted in the grant file fails", code != 0
              and "applying sql/migrate_peer_cards.sql" in out, out[-400:])
    code, out = hm("migrate")
    check("and the next run re-applies the grant file instead of reporting nothing to do",
          code == 0 and "applying sql/mem_app_role.sql" in out, out[-400:])
    code, out = hm("migrate")
    check("after which nothing is owed", code == 0 and "nothing to apply" in out, out[-300:])


def main():
    owner = scalar(OWNER_URL, "SELECT rolsuper OR rolcreaterole FROM pg_roles "
                              "WHERE rolname = current_user;")
    if owner != "t":
        raise SystemExit(f"DATABASE_URL must be the owner/maintenance role (got: {owner})")
    app_user = scalar(APP_URL, "SELECT current_user;")
    if app_user != "hm_app":
        raise SystemExit(f"HM_APP_URL must log in as hm_app (got: {app_user})")
    fx = Fixture()
    try:
        fx.setup()
        shared_login(fx)
        per_author(fx)
        definer_functions(fx)
        admin_helpers(fx)
    finally:
        fx.teardown()
    left = scalar(OWNER_URL, f"SELECT count(*) FROM mem.memories "
                             f"WHERE metadata->>'rlstest' = '{fx.tag}';")
    check("the test cleaned up its rows", left == "0", left)
    left = scalar(OWNER_URL, f"SELECT count(*) FROM pg_roles WHERE rolname LIKE 'hm_rlst_%{fx.tag}';")
    check("and its roles", left == "0", left)
    if os.environ.get("HM_RLS_SKIP_MIGRATE") != "1":
        migrations()
    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


def unavailable():
    if not shutil.which("psql"):
        return "psql is not on PATH"
    if not OWNER_URL:
        return "DATABASE_URL (the owner's connection) is not set"
    if not APP_URL:
        return "HM_APP_URL (the hm_app connection) is not set"
    for url in (OWNER_URL, APP_URL):
        try:
            p = subprocess.run(["psql", url, "-X", "-tA", "-c", "SELECT 1"],
                               capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"psql could not run ({exc})"
        if p.returncode != 0:
            return f"the database did not answer: {p.stderr.decode(errors='replace').strip()}"
    return None


if __name__ == "__main__":
    reason = unavailable()
    if reason:
        if os.environ.get("CI"):
            sys.exit(f"FAIL: {reason} -- CI provides a database for this test")
        print(f"SKIP: {reason}; this test needs a live database")
        sys.exit(0)
    sys.exit(main())
