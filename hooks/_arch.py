"""Pure Tier-0/1 resolution for the arch_invariants PreToolUse hook.

No DB, no I/O -- operates on an already-loaded graph dict (the JSON that GRAPH_SQL
below returns). This mirrors the Rust MCP server's resolve / get_constraints so the
hook injects exactly what the `get_constraints` tool would return. Keep GRAPH_SQL and
the selection logic in sync with mcp-server/src/main.rs.

Glob semantics (gitignore-ish, path-aware):
  **  matches anything incl. '/'
  *   matches anything except '/'
  ?   matches one char except '/'
A file may resolve to >=1 component; matches rank exact > longer-literal-prefix > priority.
"""
import re

# Same query the Rust MCP server uses (mcp-server/src/main.rs GRAPH_SQL). Single source of
# the graph shape; if you change one, change both.
GRAPH_SQL = (
    "SELECT json_build_object("
    "'components',(SELECT coalesce(json_agg(json_build_object("
    "'slug',slug,'name',name,'repo',repo,"
    "'parent',(SELECT slug FROM components p WHERE p.id=c.parent_id),"
    "'responsibility',responsibility,'key_paths',key_paths,'priority',priority)),'[]') FROM components c),"
    "'relationships',(SELECT coalesce(json_agg(json_build_object("
    "'src',(SELECT slug FROM components WHERE id=src_component_id),"
    "'dst',(SELECT slug FROM components WHERE id=dst_component_id),"
    "'kind',kind)),'[]') FROM relationships),"
    "'constraints',(SELECT coalesce(json_agg(json_build_object("
    "'kind',kind,'scope',scope,'repo',repo,"
    "'component',(SELECT slug FROM components WHERE id=component_id),"
    "'title',title,'statement',statement,'severity',severity,"
    "'source',(SELECT repo||':'||path FROM documents WHERE id=source_doc_id))),'[]') "
    "FROM constraints WHERE status='active'))"
)

_SEV = {"must": 0, "should": 1, "info": 2}


def _glob_to_regex(glob):
    out, i, n = ["^"], 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob[i + 1:i + 2] == "*":
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


def _literal_prefix_len(glob):
    m = re.search(r"[*?]", glob)
    return len(glob) if m is None else m.start()


def resolve(path, components):
    """Every component whose key_paths match `path`, most-specific first -> [slug]."""
    # NB: lstrip() takes a CHARACTER SET, so lstrip("./") ate the leading dot of every
    # dot-prefixed path (".gitlab-ci.yml" -> "gitlab-ci.yml", ".claude/x" -> "claude/x"),
    # which no glob can then match -> Tier 1 silently reported "no component" for mapped
    # files. Strip only a literal leading "./" (repeated), matching the Rust resolver.
    path = re.sub(r"^(?:\./)+", "", path.replace("\\", "/"))
    hits = []
    for comp in components:
        best = None
        for g in comp.get("key_paths") or []:
            if g and _glob_matches(g, path):
                cand = (0 if _has_wildcard(g) else 1, _literal_prefix_len(g))
                if best is None or cand > best[0]:
                    best = (cand, comp)
        if best is not None:
            hits.append((best[0][0], best[0][1], comp.get("priority", 0), comp["slug"]))
    hits.sort(reverse=True)  # exact, then longer prefix, then priority
    return [h[3] for h in hits]


def _expand_1hop(slugs, rels):
    out = set(slugs)
    for r in rels:
        if r.get("src") in slugs:
            out.add(r.get("dst"))
        if r.get("dst") in slugs:
            out.add(r.get("src"))
    return out


def get_constraints(paths, graph, repo):
    """Tier 1 for one repo: touched components' + globals' + 1-hop-graph constraints."""
    comps = [c for c in graph.get("components", []) if c.get("repo") == repo]
    repo_slugs = {c["slug"] for c in comps}
    # relationships carry only slugs -- keep edges wholly inside this repo so a same-named
    # slug elsewhere can't bleed a 1-hop constraint across repos.
    rels = [e for e in graph.get("relationships", [])
            if e.get("src") in repo_slugs and e.get("dst") in repo_slugs]
    cons = [c for c in graph.get("constraints", []) if c.get("repo") == repo]

    direct, touched, unmatched = set(), [], []
    for p in paths:
        ms = resolve(p, comps)
        if ms:
            touched.append((p, ms)); direct |= set(ms)
        else:
            unmatched.append(p)
    expanded = _expand_1hop(direct, rels)

    selected = []
    for c in cons:
        if c.get("scope") == "global":
            via = "global"
        elif c.get("component") in direct:
            via = "direct"
        elif c.get("component") in expanded:
            via = "graph"
        else:
            continue
        selected.append({**c, "_via": via})
    selected.sort(key=lambda c: (_SEV.get(c.get("severity"), 3),
                                 0 if c.get("scope") == "global" else 1,
                                 c["_via"] != "direct"))
    return {"touched": touched, "unmatched": unmatched, "direct": direct,
            "graph_only": expanded - direct, "constraints": selected}


def _selfcheck():
    g = {
        "components": [
            {"slug": "api", "repo": "r", "key_paths": ["src/api/**"], "priority": 0},
            {"slug": "api-routes", "repo": "r", "key_paths": ["src/api/routes.py"], "priority": 0},
            {"slug": "data", "repo": "r", "key_paths": ["src/data/**"], "priority": 0},
            {"slug": "other", "repo": "OTHER", "key_paths": ["src/api/**"], "priority": 9},
        ],
        "relationships": [{"src": "api", "dst": "data", "kind": "depends_on"}],
        "constraints": [
            {"scope": "global", "component": None, "title": "G", "statement": "s", "severity": "must", "repo": "r"},
            {"scope": "component", "component": "api", "title": "A", "statement": "s", "severity": "must", "repo": "r"},
            {"scope": "component", "component": "data", "title": "D", "statement": "s", "severity": "should", "repo": "r"},
            {"scope": "component", "component": "other", "title": "X", "statement": "s", "severity": "must", "repo": "OTHER"},
        ],
    }
    # exact path wins over the glob -> api-routes first, api second
    assert resolve("src/api/routes.py", [c for c in g["components"] if c["repo"] == "r"]) \
        == ["api-routes", "api"], "longest-match ordering"
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
