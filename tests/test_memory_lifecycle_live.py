#!/usr/bin/env python3
"""The memory lifecycle against a real Postgres: one merge path, two time axes, live premises.

Unit tests with fake cursors pin which SQL is sent; they cannot show what it DOES. Each claim
below was a finding of the architecture review, and each is checked here by running mem_ops'
own functions against a migrated store, as the unprivileged `hm_app` role where it matters:

  * a merge of a private and a project row comes out private (scope from the group, never from
    the model), records merged_from / merged_created_range / summarizes edges, and retires its
    members in BELIEF time (retired_at) without touching their EVENT time (valid_to);
  * a group spanning two authors is refused and nothing changes;
  * supersede closes the old row's event window only at a stated valid_from, refuses a future
    valid_from and one before the old row's start; no write may end before it begins;
  * `as_of` returns the belief state at that instant;
  * a derived conclusion is served only while every premise is active AND readable by the
    caller -- including a premise another reader can no longer see -- and comes back when the
    premise does (decided at query time, not by cascading);
  * the novelty gate's probe (track_access=false) does not count as recall.

No embedder: mem_ops.embed is replaced by a deterministic topic vector, the same way the other
mem_ops tests bypass it. Needs psycopg2 and a database migrated in the documented order:

    DATABASE_URL=postgresql://... python3 tests/test_memory_lifecycle_live.py
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DIM = 1024
TAG = "lifecycle-live"          # every row this test writes carries it in a project name
P1, P2 = f"{TAG}-one", f"{TAG}-two"

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


TOPICS = ["editor", "deploy", "coffee", "backup", "printer", "vacation", "kernel", "invoice"]


def topic_vector(text):
    """One axis per topic word present: same topic -> distance ~0, different -> 1."""
    v = [0.0] * DIM
    low = (text or "").lower()
    hits = [i for i, t in enumerate(TOPICS) if t in low] or [DIM - 1]
    for i in hits:
        v[i] = 1.0
    return "[" + ",".join(f"{x:.1f}" for x in v) + "]"


def load_mem_ops():
    sys.path.insert(0, str(ROOT / "ingest"))
    spec = importlib.util.spec_from_file_location("mem_ops_live", ROOT / "ingest/mem_ops.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.embed = topic_vector
    return mod


def main():
    import psycopg2
    m = load_mem_ops()

    def connect(who, app_role=True):
        conn = psycopg2.connect(DATABASE_URL)
        if app_role:
            # The policy-bound role, as production connects. SET ROLE in autocommit so a later
            # rollback in the code under test cannot undo it.
            conn.autocommit = True
            conn.cursor().execute("SET ROLE hm_app")
            conn.autocommit = False
        cur = conn.cursor()
        m.set_reader(cur, {"_identity": who})
        conn.commit()
        return conn, cur

    owner = psycopg2.connect(DATABASE_URL)
    oc = owner.cursor()

    def owner_sql(sql, params=None):
        oc.execute(sql, params)
        rows = oc.fetchall() if oc.description else None
        owner.commit()
        return rows

    def cleanup():
        ids = [r[0] for r in owner_sql("SELECT id FROM mem.memories WHERE project LIKE %s",
                                       (f"{TAG}%",))]
        if ids:
            owner_sql("DELETE FROM mem.derivations WHERE evidence_memory_id = ANY(%s) "
                      "OR derived_memory_id = ANY(%s)", (ids, ids))
            owner_sql("UPDATE mem.memories SET supersedes_id = NULL WHERE id = ANY(%s)", (ids,))
            owner_sql("DELETE FROM mem.memories WHERE id = ANY(%s)", (ids,))
        owner_sql("DELETE FROM mem.review_queue WHERE proposal::text LIKE %s", (f"%{TAG}%",))
        owner_sql("DELETE FROM mem.project_members WHERE project LIKE %s", (f"{TAG}%",))

    def row(mid):
        return owner_sql("SELECT status::text, scope::text, project, valid_from, valid_to, "
                         "retired_at, metadata, author FROM mem.memories WHERE id=%s",
                         (mid,))[0]

    cleanup()
    for author in ("alice", "bob"):
        for project in (P1, P2):
            owner_sql("INSERT INTO mem.project_members (author, project) VALUES (%s,%s) "
                      "ON CONFLICT DO NOTHING", (author, project))

    alice, ac = connect("alice")
    bob, bc = connect("bob")

    def write(cur, conn, **payload):
        payload.setdefault("_identity", "alice" if cur is ac else "bob")
        payload.setdefault("project", P1)
        mid = m.do_write(cur, payload)
        conn.commit()
        return mid

    try:
        print("== R1: one merge path -- scope from the group, members retired in belief time ==")
        priv = write(ac, alice, type="preference", content="prefers the zed editor",
                     scope="private")
        shared = write(ac, alice, type="semantic", content="the team editor is zed")
        res = m.apply_proposal_result(ac, "merge", [priv, shared],
                                      {"content": "zed is the editor", "type": "semantic",
                                       "project": P2, "scope": "project"}, who="alice",
                                      channel="consolidator")
        alice.commit()
        check("the merge is applied", res["applied"], res["message"])
        merged = row(res["id"])
        check("a private + project merge comes out PRIVATE (group, not model, decides)",
              merged[1] == "private", merged[1])
        check("and keeps the group's project, not the one the model named",
              merged[2] == P1, merged[2])
        check("merged_from and merged_created_range are recorded",
              merged[6].get("merged_from") == [priv, shared]
              and len(merged[6].get("merged_created_range") or []) == 2, str(merged[6]))
        edges = owner_sql("SELECT evidence_memory_id FROM mem.derivations "
                          "WHERE derived_memory_id=%s AND relation='summarizes' ORDER BY 1",
                          (res["id"],))
        check("summarizes edges point at every member",
              [e[0] for e in edges] == sorted([priv, shared]), str(edges))
        for mid in (priv, shared):
            st = row(mid)
            check(f"member #{mid} retired: status superseded, retired_at set",
                  st[0] == "superseded" and st[5] is not None, str(st[:6]))
            check(f"member #{mid} keeps its event time (valid_to untouched)",
                  st[4] is None, str(st[4]))
            check(f"member #{mid} names its replacement",
                  st[6].get("superseded_by") == res["id"], str(st[6]))
        src = owner_sql("SELECT channel FROM mem.sources WHERE memory_id=%s", (res["id"],))
        check("provenance names the consolidator", src == [("consolidator",)], str(src))

        print("\n== R1: a cross-author group is refused, nothing changes ==")
        mine = write(ac, alice, type="semantic", content="backup runs nightly")
        theirs = write(bc, bob, type="semantic", content="backup runs at night")
        before = owner_sql("SELECT count(*) FROM mem.memories WHERE project LIKE %s",
                           (f"{TAG}%",))[0][0]
        res = m.apply_proposal_result(ac, "merge", [mine, theirs],
                                      {"content": "backup runs nightly"}, who="alice")
        alice.commit()
        after = owner_sql("SELECT count(*) FROM mem.memories WHERE project LIKE %s",
                          (f"{TAG}%",))[0][0]
        check("refused", not res["applied"] and "alice, bob" in res["message"], res["message"])
        check("no row written", before == after, f"{before} -> {after}")
        check("both members still active",
              row(mine)[0] == "active" and row(theirs)[0] == "active")

        print("\n== R1: apply_verdict through main(), JSON in and out ==")
        a1 = write(ac, alice, type="semantic", content="printer is on floor two")
        a2 = write(ac, alice, type="semantic", content="the printer sits on floor two")

        def cli(cmd, payload, who="alice"):
            saved = (sys.argv, sys.stdin, m.connect)
            out = io.StringIO()
            try:
                sys.argv = ["mem_ops.py", cmd]
                sys.stdin = io.StringIO(json.dumps({**payload, "_identity": who}))
                m.connect = lambda: connect(who)[0]
                with contextlib.redirect_stdout(out):
                    m.main()
            finally:
                sys.argv, sys.stdin, m.connect = saved
            return out.getvalue().strip()

        out = json.loads(cli("apply_verdict", {"action": "supersede", "member_ids": [a1, a2],
                                               "proposal": {"winner_id": a2},
                                               "format": "json"}))
        check("apply_verdict supersede applies", out["applied"] and out["id"] == a2, str(out))
        check("the loser is retired, the winner untouched",
              row(a1)[0] == "superseded" and row(a2)[0] == "active" and row(a2)[5] is None)
        out = json.loads(cli("apply_verdict", {"action": "supersede", "member_ids": [a1, a2],
                                               "proposal": {"winner_id": a2},
                                               "format": "json"}))
        check("a stale group is refused, not re-applied", out["applied"] is False, str(out))
        wrote = json.loads(cli("write", {"type": "semantic", "content": "invoice due friday",
                                         "project": P1, "format": "json"}))
        check("write answers {id} in JSON", isinstance(wrote.get("id"), int), str(wrote))
        found = json.loads(cli("search", {"query": "invoice", "project": P1, "format": "json"}))
        check("search answers rows in JSON with their layer",
              any(r["id"] == wrote["id"] and r["reasoning_level"] == "explicit" for r in found),
              str(found)[:300])

        try:
            cli("apply_verdict", {"action": "derive", "member_ids": [a2], "proposal": {}})
            check("apply_verdict will not approve a Dreamer conclusion", False)
        except SystemExit as exc:
            check("apply_verdict will not approve a Dreamer conclusion",
                  "merge or supersede" in str(exc), str(exc))

        print("\n== R1: review approval runs the same code, refusals keep the claim ==")
        r1 = write(ac, alice, type="semantic", content=f"kernel {TAG} pinned")
        r2 = write(ac, alice, type="semantic", content=f"kernel {TAG} is pinned")
        queued = cli("review_add", {"action": "merge", "member_ids": [r1, theirs],
                                    "proposal": {"content": f"kernel {TAG} pinned"},
                                    "confidence": 0.5})
        rid = int(queued.split("[#", 1)[1].split("]", 1)[0])
        out = cli("review_resolve", {"id": rid, "decision": "approved"})
        note = owner_sql("SELECT status, note FROM mem.review_queue WHERE id=%s", (rid,))[0]
        check("an approved cross-author merge is recorded as not applied",
              note[0] == "approved" and "not applied" in note[1], str(note))
        check("and changed nothing", row(r1)[0] == "active" and row(theirs)[0] == "active")
        queued = cli("review_add", {"action": "merge", "member_ids": [r1, r2],
                                    "proposal": {"content": f"kernel {TAG} pinned",
                                                 "scope": "private", "project": P2},
                                    "confidence": 0.5})
        rid = int(queued.split("[#", 1)[1].split("]", 1)[0])
        out = cli("review_resolve", {"id": rid, "decision": "approved"})
        new_id = int(out.split("-> [#", 1)[1].split("]", 1)[0])
        check("an approved merge applies through the same path",
              row(r1)[0] == "superseded" and row(r2)[0] == "superseded"
              and row(new_id)[1] == "project" and row(new_id)[2] == P1, out)

        print("\n== R1: concurrent verdicts on one group cannot both land ==")
        ca, cb = write(ac, alice, type="semantic", content="invoice terms are net 30"), \
            write(ac, alice, type="semantic", content="invoice terms are net 60")
        first_conn, first = connect("alice")
        second_conn, second = connect("alice")
        r_first = m.apply_proposal_result(first, "supersede", [ca, cb], {"winner_id": ca},
                                          who="alice")          # holds its locks, uncommitted
        r_second = {}

        def run_second():
            r_second.update(m.apply_proposal_result(second, "supersede", [ca, cb],
                                                    {"winner_id": cb}, who="alice"))
            second_conn.commit()
        worker = threading.Thread(target=run_second)
        worker.start()
        blocked = False
        for _ in range(100):
            waiting = owner_sql("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                                (second_conn.get_backend_pid(),))
            if waiting and waiting[0][0] == "Lock":
                blocked = True
                break
            time.sleep(0.05)
        first_conn.commit()
        worker.join(30)
        check("the second verdict waits for the first one's lock", blocked)
        check("the first verdict lands", r_first["applied"], r_first["message"])
        check("the second is refused once it sees the first one's work",
              r_second.get("applied") is False and "changed while" in r_second.get("message", ""),
              str(r_second))
        check("exactly one of the two rows is still active",
              (row(ca)[0], row(cb)[0]) == ("active", "superseded"), str((row(ca)[0], row(cb)[0])))
        first_conn.close()
        second_conn.close()

        print("\n== R1: a merge holding its member locks does not deadlock evidence writes ==")
        # The interleaving that deadlocked with FOR UPDATE member locks: the merge has locked
        # its members and not yet written its edges; meanwhile another write adds an evidence
        # edge pointing at a member (taking the derivations lock in the evidence guard, then a
        # KEY SHARE on the member for the foreign key). NO KEY UPDATE does not conflict with
        # KEY SHARE, so the evidence write finishes while the merge still holds its locks.
        d1 = write(ac, alice, type="semantic", content="backup target is the nas")
        d2 = write(ac, alice, type="semantic", content="backup target is the nas box")
        mconn, mcur = connect("alice")
        econn, ecur = connect("alice")
        locked_evt, go_evt = threading.Event(), threading.Event()
        real_write = m.do_write

        def paused_write(cur, payload, supersedes_id=None):
            if cur is mcur and not locked_evt.is_set():
                locked_evt.set()          # members are locked; edges not yet written
                go_evt.wait(15)
            return real_write(cur, payload, supersedes_id=supersedes_id)
        merge_res, ev_res, errors = {}, {}, []

        def run_merge():
            try:
                merge_res.update(m.apply_proposal_result(
                    mcur, "merge", [d1, d2], {"content": "backup target is the nas"},
                    who="alice"))
                mconn.commit()
            except Exception as exc:
                errors.append(f"merge: {exc}")
                mconn.rollback()

        def run_evidence():
            try:
                ev_res["id"] = real_write(ecur, {
                    "_identity": "alice", "type": "semantic", "project": P1,
                    "content": "backup restore verified weekly",
                    "evidence": [{"memory_id": d1, "relation": "supports"}]})
                econn.commit()
            except Exception as exc:
                errors.append(f"evidence: {exc}")
                econn.rollback()
        m.do_write = paused_write
        try:
            merger = threading.Thread(target=run_merge)
            merger.start()
            locked_evt.wait(15)
            evidencer = threading.Thread(target=run_evidence)
            evidencer.start()
            evidencer.join(5)
            finished_while_locked = not evidencer.is_alive()
            go_evt.set()
            merger.join(30)
            evidencer.join(30)
        finally:
            m.do_write = real_write
        check("the evidence write completes while the merge holds its member locks",
              finished_while_locked)
        check("no deadlock, no error on either side", errors == [], str(errors))
        check("both land", merge_res.get("applied") is True and ev_res.get("id"),
              str((merge_res, ev_res)))
        mconn.close()
        econn.close()

        print("\n== R5: two time axes ==")
        old = write(ac, alice, type="semantic", content="kernel is version five",
                    valid_from="2026-01-01T00:00:00Z")
        try:
            m.do_write(ac, {"_identity": "alice", "content": "kernel is version six",
                            "project": P1, "valid_from": "2099-01-01T00:00:00Z"},
                       supersedes_id=old)
            check("a supersede with a future valid_from is refused", False)
        except SystemExit as exc:
            alice.rollback()
            check("a supersede with a future valid_from is refused", "future" in str(exc),
                  str(exc))
        try:
            m.do_write(ac, {"_identity": "alice", "content": "kernel is version six",
                            "project": P1, "valid_from": "2025-06-01T00:00:00Z"},
                       supersedes_id=old)
            check("a supersede starting before the old row began is refused", False)
        except SystemExit as exc:
            alice.rollback()
            check("a supersede starting before the old row began is refused",
                  "before that memory's own valid_from" in str(exc), str(exc))
        check("the target is still active after both refusals", row(old)[0] == "active")
        new = m.do_write(ac, {"_identity": "alice", "content": "kernel is version six",
                              "project": P1, "valid_from": "2026-03-01T00:00:00Z"},
                         supersedes_id=old)
        alice.commit()
        st = row(old)
        check("an explicit valid_from closes the old window THERE",
              st[4] is not None and st[4].isoformat().startswith("2026-03-01"), str(st[4]))
        check("and the old row is retired now", st[0] == "superseded" and st[5] is not None)
        plain_old = write(ac, alice, type="semantic", content="coffee machine is broken")
        m.do_write(ac, {"_identity": "alice", "content": "coffee machine works again",
                        "project": P1}, supersedes_id=plain_old)
        alice.commit()
        st = row(plain_old)
        check("a correction WITHOUT valid_from does not claim the fact ended today",
              st[4] is None and st[5] is not None, str(st[3:6]))
        try:
            write(ac, alice, type="semantic", content="vacation week",
                  valid_from="2026-05-10T00:00:00Z", valid_to="2026-05-01T00:00:00Z")
            check("a write ending before it begins is refused", False)
        except SystemExit as exc:
            alice.rollback()
            check("a write ending before it begins is refused", "before" in str(exc), str(exc))
        try:
            owner_sql("INSERT INTO mem.memories (memory_type, content, author, scope, project, "
                      "valid_from, valid_to) VALUES ('semantic','x','alice','private',%s,"
                      "'2026-05-10','2026-05-01')", (P1,))
            check("the database refuses it too", False)
        except psycopg2.Error as exc:
            owner.rollback()
            check("the database refuses it too", "mem_valid_window_ordered" in str(exc), str(exc))
        mk = write(ac, alice, type="semantic", content="deploy freeze on fridays")
        out = json.loads(cli("mark", {"id": mk, "status": "retracted", "format": "json"}))
        st = row(mk)
        check("mark retracts in belief time only",
              out["marked"] and st[0] == "retracted" and st[5] is not None and st[4] is None,
              str(st[:6]))
        cli("mark", {"id": mk, "status": "active", "format": "json"})
        check("mark status=active clears retired_at", row(mk)[0] == "active"
              and row(mk)[5] is None, str(row(mk)[:6]))

        print("\n== R5: as_of returns the belief state at that time ==")
        then = write(ac, alice, type="semantic", content="vacation is in july")
        now_ = m.do_write(ac, {"_identity": "alice", "content": "vacation is in august",
                               "project": P1}, supersedes_id=then)
        alice.commit()
        # History, written into the past by the owner: learned 10 days ago, replaced 5 days ago.
        owner_sql("UPDATE mem.memories SET created_at = now() - interval '10 days', "
                  "retired_at = now() - interval '5 days' WHERE id=%s", (then,))
        owner_sql("UPDATE mem.memories SET created_at = now() - interval '5 days' WHERE id=%s",
                  (now_,))

        def ids_for(**extra):
            rows = m.do_search(ac, {"query": "vacation", "project": P1, **extra})
            alice.commit()
            return {r[0] for r in rows}
        week_ago = owner_sql("SELECT (now() - interval '7 days')::text")[0][0]
        two_days_ago = owner_sql("SELECT (now() - interval '2 days')::text")[0][0]
        long_ago = owner_sql("SELECT (now() - interval '30 days')::text")[0][0]
        check("as_of a week ago: what I believed then", ids_for(as_of=week_ago) == {then},
              str(ids_for(as_of=week_ago)))
        check("as_of two days ago: the replacement", ids_for(as_of=two_days_ago) == {now_},
              str(ids_for(as_of=two_days_ago)))
        check("as_of before either was learned: nothing", ids_for(as_of=long_ago) == set())
        check("no as_of: the current belief only", ids_for() == {now_})
        check("include_inactive: both", ids_for(include_inactive=True) == {then, now_})
        try:
            m.do_search(ac, {"query": "vacation", "as_of": "not a time"})
            check("a malformed as_of is refused", False)
        except SystemExit:
            alice.rollback()
            check("a malformed as_of is refused", True)
        # Retired, then reactivated: the closed interval is history, not erased by reactivation.
        back = write(ac, alice, type="semantic", content="vacation planning uses the wiki")
        owner_sql("UPDATE mem.memories SET created_at = now() - interval '10 days' WHERE id=%s",
                  (back,))
        cli("mark", {"id": back, "status": "retracted", "format": "json"})
        owner_sql("UPDATE mem.memories SET retired_at = now() - interval '6 days' WHERE id=%s",
                  (back,))
        cli("mark", {"id": back, "status": "active", "format": "json"})
        intervals = row(back)[6].get("retired_intervals") or []
        check("reactivation records the interval it closes",
              row(back)[5] is None and len(intervals) == 1
              and intervals[0].get("reactivated_at"), str(intervals))
        owner_sql("UPDATE mem.memories SET metadata = jsonb_set(metadata, "
                  "'{retired_intervals,0,reactivated_at}', to_jsonb(now() - interval '3 days')) "
                  "WHERE id=%s", (back,))
        at = {d: owner_sql(f"SELECT (now() - interval '{d} days')::text")[0][0]
              for d in (8, 5, 1)}
        check("as_of before the retirement: believed", back in ids_for(as_of=at[8]))
        check("as_of inside the retired interval: NOT believed", back not in ids_for(as_of=at[5]),
              str(ids_for(as_of=at[5])))
        check("as_of after the reactivation: believed again", back in ids_for(as_of=at[1]))
        check("and now", back in ids_for())
        # A malformed interval is ignored, never fatal to the search.
        owner_sql("""UPDATE mem.memories SET metadata = jsonb_set(metadata, '{retired_intervals}',
                     metadata->'retired_intervals' || '[{"retired_at":"garbage",
                     "reactivated_at":"2026-13-45"}, 5, "x", {"retired_at":null}]'::jsonb)
                     WHERE id=%s""", (back,))
        check("malformed retired_intervals entries are ignored, the valid one still counts",
              back not in ids_for(as_of=at[5]) and back in ids_for(as_of=at[1]))
        # A reactivation that waited behind a retirement committed after it BEGAN must not
        # record a reversed interval (now() is the transaction's start).
        late = write(ac, alice, type="semantic", content="vacation days carry over")
        waiting_conn, waiting_cur = connect("alice")
        waiting_cur.execute("SELECT now()")                 # this transaction has begun
        cli("mark", {"id": late, "status": "retracted", "format": "json"})
        saved_connect = m.connect
        saved_io = (sys.argv, sys.stdin)
        try:
            sys.argv = ["mem_ops.py", "mark"]
            sys.stdin = io.StringIO(json.dumps({"id": late, "status": "active",
                                                "_identity": "alice", "format": "json"}))
            m.connect = lambda: waiting_conn
            with contextlib.redirect_stdout(io.StringIO()):
                m.main()
        finally:
            m.connect = saved_connect
            sys.argv, sys.stdin = saved_io
        iv = (row(late)[6].get("retired_intervals") or [{}])[-1]
        check("the reactivation interval is never reversed",
              bool(iv) and iv["reactivated_at"] > iv["retired_at"], str(iv))

        print("\n== R4: derived rows do not outlive their premises (query time) ==")
        p1 = write(ac, alice, type="semantic", content="deploy happens on tuesday")
        p2 = write(ac, alice, type="semantic", content="deploy needs a second reviewer")
        res = m.apply_proposal_result(ac, "derive", [p1, p2], {
            "reasoning_level": "inductive", "confidence": 0.8, "importance": 0.5,
            "content": "deploy is a planned, reviewed tuesday event"}, who="alice")
        alice.commit()
        check("the conclusion is approved", res["applied"], res["message"])
        concl = res["id"]
        check("premise ids are stamped on it", row(concl)[6].get("premise_ids") == [p1, p2])

        def served(cur, conn):
            rows = m.do_search(cur, {"query": "deploy", "project": P1, "k": 20})
            ctx = m.do_context(cur, {"query": "deploy", "project": P1, "depth": "medium",
                                     "token_budget": 8192})
            conn.commit()
            in_search = concl in {r[0] for r in rows}
            derived = [x["id"] for s in ctx["sections"] if s["name"] == "derived"
                       for x in s["items"]]
            return in_search, concl in derived, ctx["scope"].get("stale_premises", [])
        s, d, stale = served(ac, alice)
        check("with live premises it is served by search and context",
              s and d and concl not in stale, f"search={s} derived={d} stale={stale}")
        text = m.fmt_row(next(r for r in m.do_search(ac, {"query": "deploy", "project": P1,
                                                           "k": 20}) if r[0] == concl))
        alice.commit()
        check("search text marks it as derived", "derived:inductive" in text, text)
        s, d, stale = served(bc, bob)
        check("a project member sees it too while every premise is readable", s and d)

        m.do_write(ac, {"_identity": "alice", "content": "deploy happens on wednesday",
                        "project": P1}, supersedes_id=p1)
        alice.commit()
        s, d, stale = served(ac, alice)
        check("a superseded premise makes it stale: not searched, not in context",
              not s and not d, f"search={s} derived={d}")
        check("and the context names it in stale_premises", concl in stale, str(stale))
        owner_sql("UPDATE mem.memories SET status='active', retired_at=NULL WHERE id=%s", (p1,))
        s, d, stale = served(ac, alice)
        check("a premise that comes back makes it servable again (no cascade happened)",
              s and d and row(concl)[0] == "active", f"search={s} derived={d}")

        owner_sql("UPDATE mem.memories SET scope='private' WHERE id=%s", (p2,))
        s_alice, _, _ = served(ac, alice)
        s_bob, d_bob, stale_bob = served(bc, bob)
        check("a premise another reader can no longer READ makes it stale for them",
              not s_bob and not d_bob and concl in stale_bob,
              f"search={s_bob} derived={d_bob} stale={stale_bob}")
        check("while its author, who can read every premise, is still served", s_alice)
        owner_sql("UPDATE mem.memories SET scope='project' WHERE id=%s", (p2,))
        owner_sql("DELETE FROM mem.memories WHERE id=%s", (p2,))   # edges cascade with it
        s, d, _ = served(ac, alice)
        check("a premise deleted outright (its edge cascaded away) still counts as missing",
              not s and not d, f"search={s} derived={d}")

        print("\n== R4b: a stale conclusion does not arrive as evidence either ==")
        e1 = write(ac, alice, type="semantic", content="orchard trees are pruned in march")
        e2 = write(ac, alice, type="semantic", content="orchard pruning needs dry weather")
        inner = m.apply_proposal_result(ac, "derive", [e1, e2], {
            "reasoning_level": "inductive", "confidence": 0.8, "importance": 0.5,
            "content": "orchard pruning is a dry-march job"}, who="alice")
        f1 = write(ac, alice, type="semantic", content="orchard ladders are stored in the barn")
        f2 = write(ac, alice, type="semantic", content="orchard ladders are checked yearly")
        outer = m.apply_proposal_result(ac, "derive", [f1, f2], {
            "reasoning_level": "inductive", "confidence": 0.8, "importance": 0.5,
            "content": "orchard ladders are barn-kept and yearly checked"}, who="alice")
        alice.commit()
        check("(both conclusions approved)", inner["applied"] and outer["applied"])
        # The inner conclusion supports the outer one: it is reachable as EVIDENCE.
        owner_sql("INSERT INTO mem.derivations (author, evidence_memory_id, derived_memory_id, "
                  "relation) VALUES ('alice', %s, %s, 'supports')", (inner["id"], outer["id"]))
        m.do_write(ac, {"_identity": "alice", "content": "orchard trees are pruned in april",
                        "project": P1}, supersedes_id=e1)
        alice.commit()
        ctx = m.do_context(ac, {"query": "orchard ladders", "project": P1, "depth": "max",
                                "token_budget": 8192})
        alice.commit()
        ev = [x["id"] for sec in ctx["sections"] if sec["name"] == "evidence"
              for x in sec["items"]]
        check("a conclusion whose premise was retracted is not served as evidence",
              inner["id"] not in ev, str(ev))
        check("and is named in stale_premises",
              inner["id"] in ctx["scope"].get("stale_premises", []), str(ctx["scope"]))
        served_ids = {x["id"] for sec in ctx["sections"] for x in sec["items"]}
        stale_now = ctx["scope"].get("stale_premises", [])
        check("while the explicit facts the live conclusion rests on are still served",
              {f1, f2} <= served_ids and not ({f1, f2} & set(stale_now)),
              f"served={sorted(served_ids)} stale={stale_now}")
        # The same explicit facts reached ONLY through the evidence walk: a query that does not
        # match them, so they cannot ride in through the relevant section.
        ctx = m.do_context(ac, {"query": "barn-kept yearly checked", "project": P1,
                                "depth": "max", "token_budget": 8192})
        alice.commit()
        ev = [x["id"] for sec in ctx["sections"] if sec["name"] == "evidence"
              for x in sec["items"]]
        rel = [x["id"] for sec in ctx["sections"] if sec["name"] == "relevant"
               for x in sec["items"]]
        check("an explicit premise reached only as evidence is kept, not called stale",
              all(f in ev or f in rel for f in (f1, f2))
              and not ({f1, f2} & set(ctx["scope"].get("stale_premises", []))),
              f"evidence={ev} relevant={rel} stale={ctx['scope'].get('stale_premises')}")

        print("\n== R4c: retirement is stamped when it happens, not when the transaction began ==")
        old_row = write(ac, alice, type="semantic", content="lamp bulbs are warm white")
        ac.execute("SELECT now()")
        began = ac.fetchone()[0]
        ac.execute("SELECT pg_sleep(0.3)")
        m.do_write(ac, {"_identity": "alice", "content": "lamp bulbs are cool white",
                        "project": P1}, supersedes_id=old_row)
        alice.commit()
        lag = (row(old_row)[5] - began).total_seconds()
        check("supersede stamps retired_at after the transaction's own start", lag >= 0.3,
              f"{lag:.3f}s")
        handover = owner_sql("SELECT o.retired_at = n.created_at FROM mem.memories o "
                             "JOIN mem.memories n ON n.supersedes_id = o.id WHERE o.id=%s",
                             (old_row,))
        check("and exactly when its replacement was learned: no overlap, no gap",
              handover and handover[0][0] is True, str(handover))
        g1 = write(ac, alice, type="semantic", content="kettle descaling is monthly")
        g2 = write(ac, alice, type="semantic", content="kettle descaling happens each month")
        ac.execute("SELECT now()")
        began = ac.fetchone()[0]
        ac.execute("SELECT pg_sleep(0.3)")
        res = m.apply_proposal_result(ac, "merge", [g1, g2],
                                      {"content": "kettle descaling is monthly",
                                       "type": "semantic"}, who="alice", channel="consolidator")
        alice.commit()
        lag = min((row(g)[5] - began).total_seconds() for g in (g1, g2)) if res["applied"] else -1
        check("and so does a merge", lag >= 0.3, f"{lag:.3f}s {res['message']}")

        # A supersede VERDICT names a winner that already existed, possibly long before the
        # loser: retiring the loser at the winner's created_at would end it before it began.
        w = write(ac, alice, type="semantic", content="router firmware is pinned to 7.1")
        lo = write(ac, alice, type="semantic", content="router firmware pinned at 7.1 now")
        owner_sql("UPDATE mem.memories SET created_at = now() - interval '5 days' WHERE id=%s",
                  (w,))
        res = m.apply_proposal_result(ac, "supersede", [w, lo], {"winner_id": w}, who="alice",
                                      channel="consolidator")
        alice.commit()
        span = owner_sql("SELECT retired_at >= created_at FROM mem.memories WHERE id=%s", (lo,))
        check("a supersede verdict with an older winner retires its loser after it began",
              res["applied"] and span and span[0][0] is True, f"{res['message']} {span}")

        print("\n== the pinned profile holds explicit rows only ==")
        q1 = write(ac, alice, type="semantic", content="printer jams on thick paper")
        q2 = write(ac, alice, type="semantic", content="printer jams when the tray is full")
        live = m.apply_proposal_result(ac, "derive", [q1, q2], {
            "reasoning_level": "inductive", "confidence": 0.9, "importance": 0.7,
            "content": "printer jams follow from how the paper is loaded"}, who="alice")
        alice.commit()
        concl = live["id"]
        owner_sql("UPDATE mem.memories SET importance=0.95 WHERE id=%s", (concl,))
        check("(the conclusion's premises are live)",
              owner_sql("SELECT set_config('mem.reader','alice',false), "
                        "mem.premises_live(%s)", (concl,))[0][1] is True)
        pinned = write(ac, alice, type="semantic", content="kernel builds use clang",
                       importance=0.9)
        sys.path.insert(0, str(ROOT / "hooks"))
        import mem_profile
        prof = subprocess.run(["psql", DATABASE_URL, "-tAX", "-v", "ON_ERROR_STOP=1"],
                              input=mem_profile.SQL.encode(), capture_output=True, timeout=60,
                              env={**os.environ, "PGOPTIONS": "-c mem.reader=alice"})
        lines = prof.stdout.decode().splitlines()
        check("an explicit fact is pinned", any(l.startswith(f"#FACT# {pinned}|") for l in lines),
              prof.stderr.decode()[-300:])
        check("a conclusion is not, whatever its importance",
              not any(l.split("|")[0].endswith(f" {concl}") for l in lines), str(lines[:12]))
        page = write(ac, alice, type="semantic", content="kernel page: everything about builds",
                     importance=0.95)
        # `kind` is reserved metadata that only reflect's in-process path may set.
        owner_sql("UPDATE mem.memories SET metadata = metadata || '{\"kind\":\"page\"}' "
                  "WHERE id=%s", (page,))
        prof = subprocess.run(["psql", DATABASE_URL, "-tAX", "-v", "ON_ERROR_STOP=1"],
                              input=mem_profile.SQL.encode(), capture_output=True, timeout=60,
                              env={**os.environ, "PGOPTIONS": "-c mem.reader=alice"})
        lines = prof.stdout.decode().splitlines()
        check("nor is a knowledge page (a synthesis, possibly of conclusions)",
              not any(l.split("|")[0].endswith(f" {page}") for l in lines), str(lines[:12]))

        print("\n== R7: the novelty gate looks without recalling ==")
        probe = write(ac, alice, type="semantic", content="editor theme is dark")
        m.do_search(ac, {"query": "editor theme", "project": P1}, track_access=False)
        alice.commit()
        n0 = owner_sql("SELECT access_count FROM mem.memories WHERE id=%s", (probe,))[0][0]
        cli("search", {"query": "editor theme", "project": P1, "track_access": False,
                       "format": "json"})
        n1 = owner_sql("SELECT access_count FROM mem.memories WHERE id=%s", (probe,))[0][0]
        cli("search", {"query": "editor theme", "project": P1, "format": "json"})
        n2 = owner_sql("SELECT access_count FROM mem.memories WHERE id=%s", (probe,))[0][0]
        check("track_access=false leaves the counter alone", n0 == 0 and n1 == 0, f"{n0},{n1}")
        check("an ordinary search still counts as recall", n2 == 1, str(n2))

        print("\n== R8: reflect input is fully ordered ==")
        for text in ("backup a", "backup b", "backup c"):
            write(ac, alice, type="semantic", content=text, importance=0.7)
        grp = m.do_reflect_group(ac, {"project": P1})
        alice.commit()
        same = [g["id"] for g in grp if g["content"].startswith("backup ")]
        check("equal importance falls back to newest first, then id",
              same == sorted(same, reverse=True), str(same))
    finally:
        for conn in (alice, bob):
            conn.rollback()
            conn.close()
        cleanup()
        owner.close()

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


def unavailable():
    """Why this run cannot reach a migrated database, or None (same rule as test_memory_sql)."""
    if not DATABASE_URL:
        return "DATABASE_URL is not set"
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return "psycopg2 is not installed"
    if not shutil.which("psql"):
        return "psql is not on PATH"
    try:
        p = subprocess.run(["psql", DATABASE_URL, "-tAX", "-c",
                            "SELECT to_regprocedure('mem.premises_live(bigint)') IS NOT NULL"],
                           capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"psql could not run ({exc})"
    if p.returncode != 0:
        return f"the database did not answer: {p.stderr.decode(errors='replace').strip()}"
    if p.stdout.decode().strip() != "t":
        return "sql/migrate_memory_lifecycle.sql has not been applied"
    return None


if __name__ == "__main__":
    reason = unavailable()
    if reason:
        if os.environ.get("CI"):
            sys.exit(f"FAIL: {reason} -- CI provides a migrated database for this test")
        print(f"SKIP: {reason}; this test needs a live, migrated database (set DATABASE_URL)")
        sys.exit(0)
    sys.exit(main())
