"""Pure Tier-0/1 resolution for the arch_invariants PreToolUse hook.

No DB -- operates on an already-loaded graph dict (the JSON that GRAPH_SQL below returns).
The only I/O is path resolution (lstat/readlink, see `resolve_fs`), so that a symlinked
directory and its `..` resolve physically, the way the filesystem would. This mirrors the Rust MCP server's resolve / get_constraints so the
hook injects exactly what the `get_constraints` tool would return. Keep GRAPH_SQL and
the selection logic in sync with mcp-server/src/main.rs; tests/fixtures/tier1_cases.json is
the shared contract both implementations are tested against.

Glob semantics (gitignore-ish, path-aware):
  **  matches anything incl. '/'
  *   matches anything except '/'
  ?   matches one char except '/'

Matching is ADDITIVE. Every component whose key_paths match a file applies to it, and so do
its rules: editing `src/api/x.py` under both `src/**` and `src/api/**` delivers both sets.
That is the fail-safe reading for `must` rules -- a broader rule is never silently switched
off because a narrower glob also matched. Specificity only ORDERS the matches (most specific
first); it never removes one. Specificity is (exact path, number of literal characters in the
best matching glob, priority, slug), so `**/migrations/*.sql` (16 literal characters)
outranks `src/**` (4) for a migration file.

The 1-hop expansion follows relationships of the CURRENT repo only (both endpoints in it), in
both directions and regardless of `kind`. Undirected because `kind` is free text with no
declared direction -- `api depends_on db` and `db owns api` would need opposite readings --
and the store has no direction column beyond src/dst order; for `must` rules, pulling the
neighbour's rules is the fail-safe error.
"""
import os
import re

# Same query the Rust MCP server uses (mcp-server/src/main.rs GRAPH_SQL). Single source of
# the graph shape; if you change one, change both (ci/check_graph_sql_parity.py enforces it).
# Relationships carry the repo of BOTH endpoints (joined by id, not by slug), so an edge from
# another repo that happens to use the same slugs can never be mistaken for one of this repo's.
GRAPH_SQL = (
    "SELECT json_build_object("
    "'components',(SELECT coalesce(json_agg(json_build_object("
    "'slug',slug,'name',name,'repo',repo,"
    "'parent',(SELECT slug FROM components p WHERE p.id=c.parent_id),"
    "'responsibility',responsibility,'key_paths',key_paths,'priority',priority) ORDER BY c.id),'[]') "
    "FROM components c),"
    "'relationships',(SELECT coalesce(json_agg(json_build_object("
    "'src',s.slug,'dst',d.slug,'kind',r.kind,'repo',s.repo,'dst_repo',d.repo) "
    "ORDER BY s.repo,s.slug,d.slug,r.kind),'[]') "
    "FROM relationships r JOIN components s ON s.id=r.src_component_id "
    "JOIN components d ON d.id=r.dst_component_id),"
    "'constraints',(SELECT coalesce(json_agg(json_build_object("
    "'kind',kind,'scope',scope,'repo',repo,"
    "'component',(SELECT slug FROM components WHERE id=component_id),"
    "'component_repo',(SELECT repo FROM components WHERE id=component_id),"
    "'title',title,'statement',statement,'severity',severity,"
    "'source',(SELECT repo||':'||path FROM documents WHERE id=source_doc_id)) ORDER BY x.id),'[]') "
    "FROM constraints x WHERE status='active'))"
)

_SEV = {"must": 0, "should": 1, "info": 2}


def _glob_to_regex(glob):
    out, i, n = ["^"], 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob[i + 1:i + 2] == "*":
                # `**/` matches zero OR more path segments, so `src/**/*.py` also matches
                # `src/main.py`. Mapping `**` to `.*` and leaving the `/` literal required at
                # least one directory, which disagreed with the Rust resolver in main.rs and
                # therefore with what get_constraints returns -- the hook silently withheld
                # invariants the tool would have shown, which is the exact Tier-1 failure this
                # file exists to prevent. Keep the two in step.
                if glob[i + 2:i + 3] == "/":
                    out.append("(?:.*/)?"); i += 3; continue
                out.append(".*"); i += 2
            else:
                out.append("[^/]*"); i += 1
        elif c == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(c)); i += 1
    out.append("$")
    return "".join(out)


def _glob_matches(glob, path):
    return re.match(_glob_to_regex(glob), path) is not None


def _has_wildcard(glob):
    return bool(re.search(r"[*?]", glob))


def _literal_chars(glob):
    """How much of the glob is literal text -- the specificity measure for ordering."""
    return sum(1 for ch in glob if ch not in "*?")


def _ascii_lower(s):
    # ASCII-only, so the Rust side (to_ascii_lowercase) agrees byte for byte and the length of
    # the prefix being stripped never changes.
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in s)


_DRIVE = re.compile(r"^[A-Za-z]:/")


def _split_prefix(p):
    """('/' | 'X:/' | '', rest) of a '/'-separated path. A drive-qualified path is absolute on
    every platform, so `C:/work/x.py` is never mistaken for a relative one; the drive letter is
    upper-cased because drive letters are case-insensitive everywhere."""
    if _DRIVE.match(p):
        return p[0].upper() + ":/", p[3:]
    if p.startswith("/"):
        return "/", p[1:]
    return "", p


def is_abs(path):
    return _split_prefix((path or "").replace("\\", "/"))[0] != ""


def _norm(p):
    """Lexical normalisation shared with Rust: '\\' -> '/', drop '' and '.' segments, resolve
    '..' against what precedes it (never above an absolute prefix). No filesystem access --
    used only where nothing exists on disk to resolve against."""
    prefix, rest = _split_prefix((p or "").replace("\\", "/"))
    segs = []
    for seg in rest.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segs and segs[-1] != "..":
                segs.pop()
            elif not prefix:
                segs.append(seg)
            continue
        segs.append(seg)
    return prefix + "/".join(segs)


def resolve_fs(path):
    """Physical resolution of an absolute POSIX path, identical to main.rs `resolve_fs`.

    Walks the path one segment at a time. While the prefix exists, a symlink is replaced by its
    target and `..` steps to the parent of what has been RESOLVED so far -- so
    /repo/alias/../x.py is where the filesystem would put it, not where the text suggests.
    From the first segment that does not exist (a file about to be created) the rest is joined
    lexically. Drive-qualified paths cannot be examined here and are normalised lexically."""
    prefix, rest = _split_prefix((path or "").replace("\\", "/"))
    if prefix != "/":
        return _norm(path)
    out, pending, exists, hops = [], [x for x in rest.split("/") if x not in ("", ".")], True, 0
    while pending:
        seg = pending.pop(0)
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
        if not exists:
            continue
        cur = "/" + "/".join(out)
        try:
            st_link = os.path.islink(cur) if os.path.lexists(cur) else None
        except OSError:
            st_link = None
        if st_link is None:
            exists = False
        elif st_link:
            hops += 1
            if hops > 40:                 # a loop: stop resolving, keep the text
                exists = False
                continue
            target = os.readlink(cur).replace("\\", "/")
            out.pop()
            if target.startswith("/"):
                out = []
            pending[:0] = [x for x in target.split("/") if x not in ("", ".")]
    return "/" + "/".join(out)


def _same_dir(a, b):
    """True when `a` and `b` are one directory on disk (same device and inode)."""
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def rel_to_root(path, root):
    """(repo-relative path, True) for a path inside the repo root, (resolved path, False) for
    one outside it -- the same rule as main.rs `rel_to_root`.

    * A relative path is relative to the root; an absolute one (POSIX or drive-qualified) is
      taken as given. Both are resolved physically (`resolve_fs`), and so is the root.
    * Containment is CASE-SENSITIVE. `/work/App` and `/work/app` are two directories on a
      case-sensitive filesystem, so a case-insensitive match would hand one repo's rules to the
      other. The only exception is a prefix that differs in case yet is the very same directory
      on disk (same device and inode) -- which is what a case-insensitive filesystem (macOS,
      Windows) reports, and exactly the case where folding is right.
    * Outside the root -- an absolute path elsewhere, `../b/x.py` -- is reported as outside and
      matches no component: no glob of this repo may claim a file of another tree.
    * Without a root, a relative path is taken as already repo-relative and anything absolute or
      escaping upward is outside.
    """
    p = (path or "").replace("\\", "/")
    if not root:
        n = _norm(p)
        if is_abs(n) or n == ".." or n.startswith("../"):
            return n, False
        return n, True
    r = root.replace("\\", "/")
    if not is_abs(p):
        p = r.rstrip("/") + "/" + p
    P = resolve_fs(p)
    R = resolve_fs(r).rstrip("/") or "/"
    if R == "/":
        return P.lstrip("/"), P.startswith("/")
    if P == R:
        return "", True
    if P.startswith(R + "/"):
        return P[len(R) + 1:], True
    if (_ascii_lower(P).startswith(_ascii_lower(R) + "/")
            and _same_dir(P[:len(R)], R)):
        return P[len(R) + 1:], True
    return P, False


def _specificity(path, comp):
    """(exact, literal chars) of the component's best matching glob, or None if none match."""
    best = None
    for g in comp.get("key_paths") or []:
        if g and _glob_matches(g, path):
            cand = (0 if _has_wildcard(g) else 1, _literal_chars(g))
            if best is None or cand > best:
                best = cand
    return best


def resolve(path, components):
    """Every component whose key_paths match `path`, most-specific first -> [slug]."""
    # NB: lstrip() takes a CHARACTER SET, so lstrip("./") ate the leading dot of every
    # dot-prefixed path (".gitlab-ci.yml" -> "gitlab-ci.yml", ".claude/x" -> "claude/x"),
    # which no glob can then match -> Tier 1 silently reported "no component" for mapped
    # files. _norm drops only whole "." segments, matching the Rust resolver.
    path = _norm(path)
    hits = []
    for comp in components:
        best = _specificity(path, comp)
        if best is not None:
            hits.append((-best[0], -best[1], -int(comp.get("priority") or 0), comp["slug"]))
    hits.sort()  # exact, then more literal characters, then priority, then slug
    return [h[3] for h in hits]


def _edges(graph, repo):
    """Relationships with BOTH endpoints in `repo`. Joined by id in GRAPH_SQL, so a same-slug
    edge from another repo carries that repo's name and is excluded here."""
    return [e for e in graph.get("relationships", [])
            if e.get("repo") == repo and e.get("dst_repo") == repo]


def _expand_1hop(slugs, rels):
    out = set(slugs)
    for r in rels:
        if r.get("src") in slugs:
            out.add(r.get("dst"))
        if r.get("dst") in slugs:
            out.add(r.get("src"))
    return out


def scope_note(graph, repo):
    """The `(!) SCOPE` line for a scope with no components at all, or None. Same text as the
    Rust server's get_constraints / locate."""
    known = sorted({c.get("repo") for c in graph.get("components", []) if c.get("repo")})
    if repo in known:
        return None
    return (f"(!) SCOPE: nothing is mapped under the scope '{repo}', so no constraint can ever "
            f"resolve here. Ingested scopes: {', '.join(known) or '(none)'}. Set HM_REPO to the "
            f"right one, or seed a map for this repo.")


def _sort_key(c):
    return (_SEV.get(c.get("severity"), 3), 0 if c.get("scope") == "global" else 1,
            0 if c["_via"] == "direct" else 1, c.get("title") or "", c.get("statement") or "")


def get_constraints(paths, graph, repo, root=None):
    """Tier 1 for one repo: touched components' + globals' + 1-hop-graph constraints."""
    comps = [c for c in graph.get("components", []) if c.get("repo") == repo]
    rels = _edges(graph, repo)
    cons = [c for c in graph.get("constraints", []) if c.get("repo") == repo]

    direct, touched, unmatched, outside = set(), [], [], []
    for p in paths:
        rp, inside = rel_to_root(p, root)
        if not inside:
            outside.append(rp)          # another tree: no glob of this repo may claim it
            continue
        if not rp:
            continue
        ms = resolve(rp, comps)
        if ms:
            touched.append((rp, ms)); direct |= set(ms)
        else:
            unmatched.append(rp)
    expanded = _expand_1hop(direct, rels)
    # This repo's globals govern this repo's files: none applies when every path is outside.
    any_inside = bool(touched or unmatched)

    selected, undeliverable = [], []
    for c in cons:
        comp = c.get("component") or ""
        # A component constraint is reachable only through a component of THIS repo. Older
        # graphs carry no component_repo; treat a missing one as this repo.
        own = c.get("component_repo", repo) == repo
        if c.get("scope") == "global":
            if not any_inside:
                continue
            via = "global"
        elif own and comp in direct:
            via = "direct"
        elif own and comp in expanded:
            via = "graph"
        else:
            if c.get("scope") != "component" or not comp or not own:
                undeliverable.append(c)
            continue
        selected.append({**c, "_via": via})
    selected.sort(key=_sort_key)
    return {"touched": touched, "unmatched": unmatched, "outside": outside, "direct": direct,
            "graph_only": expanded - direct, "constraints": selected,
            "undeliverable": undeliverable, "scope_note": scope_note(graph, repo)}


def _selfcheck():
    g = {
        "components": [
            {"slug": "api", "repo": "r", "key_paths": ["src/api/**"], "priority": 0},
            {"slug": "api-routes", "repo": "r", "key_paths": ["src/api/routes.py"], "priority": 0},
            {"slug": "data", "repo": "r", "key_paths": ["src/data/**"], "priority": 0},
            {"slug": "other", "repo": "OTHER", "key_paths": ["src/api/**"], "priority": 9},
        ],
        "relationships": [{"src": "api", "dst": "data", "kind": "depends_on",
                           "repo": "r", "dst_repo": "r"}],
        "constraints": [
            {"scope": "global", "component": None, "title": "G", "statement": "s", "severity": "must", "repo": "r"},
            {"scope": "component", "component": "api", "title": "A", "statement": "s", "severity": "must", "repo": "r"},
            {"scope": "component", "component": "data", "title": "D", "statement": "s", "severity": "should", "repo": "r"},
            {"scope": "component", "component": "other", "title": "X", "statement": "s", "severity": "must", "repo": "OTHER"},
        ],
    }
    # exact path wins over the glob -> api-routes first, api second
    assert resolve("src/api/routes.py", [c for c in g["components"] if c["repo"] == "r"]) \
        == ["api-routes", "api"], "specificity ordering"
    r = get_constraints(["src/api/routes.py"], g, "r")
    titles = [c["title"] for c in r["constraints"]]
    assert "G" in titles and "A" in titles, "global + direct present"
    assert "D" in titles, "1-hop graph (api->data) pulls data's constraint"
    assert "X" not in titles, "other repo's constraint must not leak"
    # global must sorts before component-should
    assert titles.index("G") < titles.index("D"), "severity+scope ordering"
    r2 = get_constraints(["nowhere/x.py"], g, "r")
    assert r2["unmatched"] == ["nowhere/x.py"] and not r2["direct"], "unmatched flagged"
    print("ok")


if __name__ == "__main__":
    _selfcheck()
