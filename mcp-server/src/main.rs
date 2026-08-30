//! HyperMnesia MCP server (stdio, JSON-RPC 2.0).
//!
//! Exposes the store to an MCP client (e.g. Claude Code):
//!   get_project_map * get_constraints(paths) * locate(query) * get_document(path)
//!   * search_docs(query) * memory_search/write/supersede/get
//!
//! Lean by design: speaks MCP stdio directly (newline-delimited JSON-RPC) -- no async
//! runtime. Postgres is reached via `psql "$DATABASE_URL"`; doc/memory search shells to
//! the bundled python scripts. The structural graph is fetched once and cached.

use regex::Regex;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, Write};
use std::sync::Mutex;

const GRAPH_SQL: &str = r#"
SELECT json_build_object(
 'components', (SELECT coalesce(json_agg(json_build_object(
     'slug',slug,'name',name,'repo',repo,
     'parent',(SELECT slug FROM components p WHERE p.id=c.parent_id),
     'responsibility',responsibility,'key_paths',key_paths,'priority',priority)),'[]')
   FROM components c),
 'relationships', (SELECT coalesce(json_agg(json_build_object(
     'src',(SELECT slug FROM components WHERE id=src_component_id),
     'dst',(SELECT slug FROM components WHERE id=dst_component_id),
     'kind',kind)),'[]') FROM relationships),
 'constraints', (SELECT coalesce(json_agg(json_build_object(
     'kind',kind,'scope',scope,'repo',repo,
     'component',(SELECT slug FROM components WHERE id=component_id),
     'title',title,'statement',statement,'severity',severity,
     'source',(SELECT repo||':'||path FROM documents WHERE id=source_doc_id))),'[]')
   FROM constraints WHERE status='active')
);
"#;

static GRAPH: Mutex<Option<Value>> = Mutex::new(None);

// -- local process / DB access ----------------------------------------------
// Everything runs locally: db_query shells to `psql "$DATABASE_URL"`, the search/memory
// tools shell to the bundled python scripts (paths from HM_SEARCH / HM_MEM_OPS / HM_RERANK).
fn database_url() -> String {
    std::env::var("DATABASE_URL")
        .unwrap_or_else(|_| "postgresql://hm@localhost:5432/hypermnesia".to_string())
}
fn py() -> String { std::env::var("HM_PYTHON").unwrap_or_else(|_| "python3".to_string()) }

/// Path to a bundled script: explicit env, else resolved from the binary's own location
/// (`<repo>/mcp-server/target/release/hypermnesia-mcp` -> `<repo>/<rel>`), NOT the client's cwd.
fn script_path(env_key: &str, rel: &str) -> String {
    if let Ok(p) = std::env::var(env_key) {
        if !p.is_empty() { return p; }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(root) = exe.ancestors().nth(4) {   // exe/release/target/mcp-server/<root>
            let p = root.join(rel);
            if p.exists() { return p.to_string_lossy().into_owned(); }
        }
    }
    rel.to_string()
}

/// This workspace's repo name -- explicit env, else the cwd's folder name (matches how
/// each workspace was ingested). Scopes search_docs / get_document to the agent's own repo.
/// The result is interpolated into both a shell command (search_docs) and SQL (get_document),
/// so it MUST be a bare identifier -- anything outside [A-Za-z0-9._-] is dropped, closing shell
/// injection (and the split_whitespace argv split on a space) and SQL injection at the source.
fn repo() -> String {
    let raw = std::env::var("HM_REPO").ok().filter(|s| !s.is_empty())
        .or_else(|| std::env::current_dir().ok()
            .and_then(|p| p.file_name().map(|f| f.to_string_lossy().into_owned())))
        .unwrap_or_default();
    let safe: String = raw.chars().filter(|c| c.is_ascii_alphanumeric() || "._-".contains(*c)).collect();
    if safe.is_empty() { "default".to_string() } else { safe }
}

fn db_query(sql: &str) -> Result<String, String> {
    use std::process::{Command, Stdio};
    let mut child = Command::new("psql")
        .arg(database_url()).args(["-tAX"])
        .env("PGCLIENTENCODING", "UTF8")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| format!("spawn psql: {e}"))?;
    {
        let mut stdin = child.stdin.take().ok_or("no stdin")?;
        stdin.write_all(sql.as_bytes()).map_err(|e| format!("write sql: {e}"))?;
    }
    let out = child.wait_with_output().map_err(|e| format!("wait: {e}"))?;
    if !out.status.success() {
        return Err(format!("psql failed: {}", String::from_utf8_lossy(&out.stderr)));
    }
    Ok(String::from_utf8_lossy(&out.stdout).to_string())
}

fn graph() -> Result<Value, String> {
    let mut g = GRAPH.lock().unwrap();
    if g.is_none() {
        let raw = db_query(GRAPH_SQL)?;
        let v: Value = serde_json::from_str(raw.trim()).map_err(|e| format!("parse graph: {e}"))?;
        *g = Some(v);
    }
    Ok(g.clone().unwrap())
}

// -- Resolver: path -> component (longest-match glob) ------------------------
fn glob_to_regex(glob: &str) -> String {
    let mut out = String::from("^");
    let chars: Vec<char> = glob.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        match chars[i] {
            '*' if i + 1 < chars.len() && chars[i + 1] == '*' => {
                // `**/` matches zero-or-more path segments INCLUDING zero, so `**/x.md` matches
                // a root-level `x.md` too (standard globstar). Bare `**` stays `.*`.
                if i + 2 < chars.len() && chars[i + 2] == '/' { out.push_str("(?:.*/)?"); i += 3; }
                else { out.push_str(".*"); i += 2; }
            }
            '*' => { out.push_str("[^/]*"); i += 1; }
            '?' => { out.push_str("[^/]"); i += 1; }
            c => {
                if ".+()|[]{}^$\\".contains(c) { out.push('\\'); }
                out.push(c); i += 1;
            }
        }
    }
    out.push('$');
    out
}

fn has_wildcard(g: &str) -> bool { g.contains('*') || g.contains('?') }
fn prefix_len(g: &str) -> usize { g.find(['*', '?']).unwrap_or(g.len()) }

/// Returns matching slugs, most specific first.
fn resolve(path: &str, comps: &[Value]) -> Vec<String> {
    let path = path.replace('\\', "/");
    let path = path.trim_start_matches("./");
    // (exact, prefix_len, priority, slug)
    let mut hits: Vec<(i32, usize, i64, String)> = Vec::new();
    for c in comps {
        let slug = c["slug"].as_str().unwrap_or("");
        let prio = c["priority"].as_i64().unwrap_or(0);
        let mut best: Option<(i32, usize)> = None;
        if let Some(kps) = c["key_paths"].as_array() {
            for kp in kps {
                let g = kp.as_str().unwrap_or("");
                if g.is_empty() { continue; }
                if let Ok(re) = Regex::new(&glob_to_regex(g)) {
                    if re.is_match(path) {
                        let cand = (if has_wildcard(g) { 0 } else { 1 }, prefix_len(g));
                        if best.map_or(true, |b| cand > b) { best = Some(cand); }
                    }
                }
            }
        }
        if let Some((ex, pl)) = best {
            hits.push((ex, pl, prio, slug.to_string()));
        }
    }
    hits.sort_by(|a, b| (b.0, b.1, b.2).cmp(&(a.0, a.1, a.2)));
    hits.into_iter().map(|h| h.3).collect()
}

fn expand_1hop(slugs: &HashSet<String>, rels: &[Value]) -> HashSet<String> {
    let mut out = slugs.clone();
    for r in rels {
        let (s, d) = (r["src"].as_str().unwrap_or(""), r["dst"].as_str().unwrap_or(""));
        if slugs.contains(s) { out.insert(d.to_string()); }
        if slugs.contains(d) { out.insert(s.to_string()); }
    }
    out
}

fn sev_rank(s: &str) -> i32 { match s { "must" => 0, "should" => 1, _ => 2 } }

/// Components/constraints belonging to a single repo (the structural tier is now multi-repo).
fn repo_filter(arr: &Value, repo: &str) -> Vec<Value> {
    arr.as_array().map(|a| a.iter()
        .filter(|c| c["repo"].as_str() == Some(repo))
        .cloned().collect()).unwrap_or_default()
}

// -- Tools -------------------------------------------------------------------
fn tool_project_map(g: &Value) -> String {
    let r = repo();
    let comps = repo_filter(&g["components"], &r);
    let cons = repo_filter(&g["constraints"], &r);
    if comps.is_empty() {
        return format!("[no structural map for repo '{r}' yet -- its docs are searchable via search_docs / get_document]\n");
    }
    let mut children: HashMap<String, Vec<&Value>> = HashMap::new();
    let mut roots: Vec<&Value> = Vec::new();
    for c in &comps {
        match c["parent"].as_str() {
            Some(p) => children.entry(p.to_string()).or_default().push(c),
            None => roots.push(c),
        }
    }
    let mut out = String::from("# GLOBAL INVARIANTS\n");
    let mut globals: Vec<&Value> = cons.iter().filter(|c| c["scope"] == "global").collect();
    globals.sort_by_key(|c| sev_rank(c["severity"].as_str().unwrap_or("")));
    for gg in globals {
        out.push_str(&format!("  [{}] {}: {}\n",
            gg["severity"].as_str().unwrap_or(""), gg["title"].as_str().unwrap_or(""),
            gg["statement"].as_str().unwrap_or("")));
    }
    out.push_str("\n# PROJECT MAP\n");
    roots.sort_by_key(|c| c["slug"].as_str().unwrap_or("").to_string());
    for r in &roots {
        render_component(r, &children, 1, &mut out);
    }
    out
}

/// Render a component and, recursively, its descendants (so grandchildren aren't dropped).
fn render_component(c: &Value, children: &HashMap<String, Vec<&Value>>, depth: usize, out: &mut String) {
    let slug = c["slug"].as_str().unwrap_or("");
    let kps: Vec<&str> = c["key_paths"].as_array().map(|a|
        a.iter().filter_map(|x| x.as_str()).collect()).unwrap_or_default();
    let kp = if kps.is_empty() { String::new() }
             else {
                 let mut s = format!("  [{}", kps.iter().take(3).cloned().collect::<Vec<_>>().join(", "));
                 if kps.len() > 3 { s.push_str(", ..."); }
                 s.push(']'); s
             };
    out.push_str(&format!("{}{} -- {}{}\n", "  ".repeat(depth), slug,
        c["responsibility"].as_str().unwrap_or(""), kp));
    if let Some(ch) = children.get(slug) {
        let mut ch = ch.clone();
        ch.sort_by_key(|x| -x["priority"].as_i64().unwrap_or(0));
        for x in ch { render_component(x, children, depth + 1, out); }
    }
}

fn tool_get_constraints(g: &Value, paths: &[String]) -> String {
    let r = repo();
    let comps = repo_filter(&g["components"], &r);
    // relationships carry only slugs (no repo); restrict to edges wholly within THIS repo's
    // components so a same-named slug in another repo can't bleed a 1-hop constraint across repos.
    let repo_slugs: HashSet<String> = comps.iter()
        .filter_map(|c| c["slug"].as_str().map(str::to_string)).collect();
    let rels: Vec<Value> = g["relationships"].as_array().map(|a| a.iter()
        .filter(|e| repo_slugs.contains(e["src"].as_str().unwrap_or(""))
                 && repo_slugs.contains(e["dst"].as_str().unwrap_or("")))
        .cloned().collect()).unwrap_or_default();
    let cons = repo_filter(&g["constraints"], &r);

    let mut direct: HashSet<String> = HashSet::new();
    let mut touched: Vec<(String, Vec<String>)> = Vec::new();
    let mut unmatched: Vec<String> = Vec::new();
    for p in paths {
        let ms = resolve(p, &comps);
        if ms.is_empty() { unmatched.push(p.clone()); }
        else { for m in &ms { direct.insert(m.clone()); } touched.push((p.clone(), ms)); }
    }
    let expanded = expand_1hop(&direct, &rels);

    let mut selected: Vec<(&Value, &str)> = Vec::new();
    for c in &cons {
        let scope = c["scope"].as_str().unwrap_or("");
        let comp = c["component"].as_str().unwrap_or("");
        if scope == "global" { selected.push((c, "global")); }
        else if direct.contains(comp) { selected.push((c, "direct")); }
        else if expanded.contains(comp) { selected.push((c, "graph")); }
    }
    selected.sort_by(|a, b| {
        let ka = (sev_rank(a.0["severity"].as_str().unwrap_or("")),
                  if a.0["scope"] == "global" { 0 } else { 1 }, if a.1 == "direct" { 0 } else { 1 });
        let kb = (sev_rank(b.0["severity"].as_str().unwrap_or("")),
                  if b.0["scope"] == "global" { 0 } else { 1 }, if b.1 == "direct" { 0 } else { 1 });
        ka.cmp(&kb)
    });

    let mut out = String::new();
    for (p, ms) in &touched { out.push_str(&format!("{} -> {}\n", p, ms.join(", "))); }
    if !unmatched.is_empty() {
        out.push_str(&format!("(!) FLAGGED (no component -- extend the map): {}\n", unmatched.join(", ")));
    }
    let graph_only: Vec<String> = expanded.difference(&direct).cloned().collect();
    if !graph_only.is_empty() {
        out.push_str(&format!("(1-hop graph also pulled: {})\n", graph_only.join(", ")));
    }
    out.push_str("\nApplicable constraints:\n");
    if selected.is_empty() { out.push_str("  (none)\n"); }
    for (c, via) in &selected {
        let tag = if c["scope"] == "global" { "global".to_string() }
                  else if *via == "direct" { c["component"].as_str().unwrap_or("").to_string() }
                  else { format!("via {}", c["component"].as_str().unwrap_or("")) };
        let src = c["source"].as_str().map(|s| format!("  [src: {s}]")).unwrap_or_default();
        out.push_str(&format!("  [{}] {} ({}){}\n      {}\n",
            c["severity"].as_str().unwrap_or(""), c["title"].as_str().unwrap_or(""),
            tag, src, c["statement"].as_str().unwrap_or("")));
    }
    out
}

fn tool_locate(g: &Value, query: &str) -> String {
    let q = query.to_lowercase();
    let comps = repo_filter(&g["components"], &repo());
    let mut out = String::new();
    for c in &comps {
        let hay = format!("{} {} {} {}",
            c["slug"].as_str().unwrap_or(""), c["name"].as_str().unwrap_or(""),
            c["responsibility"].as_str().unwrap_or(""),
            c["key_paths"].as_array().map(|a| a.iter().filter_map(|x| x.as_str())
                .collect::<Vec<_>>().join(" ")).unwrap_or_default()).to_lowercase();
        if hay.contains(&q) {
            let kps = c["key_paths"].as_array().map(|a| a.iter().filter_map(|x| x.as_str())
                .collect::<Vec<_>>().join(", ")).unwrap_or_default();
            out.push_str(&format!("* {} ({}) -- {}\n    paths: {}\n",
                c["slug"].as_str().unwrap_or(""), c["name"].as_str().unwrap_or(""),
                c["responsibility"].as_str().unwrap_or(""),
                if kps.is_empty() { "--" } else { &kps }));
        }
    }
    if out.is_empty() { format!("No component matches \"{query}\". Try get_project_map.") } else { out }
}

fn tool_get_document(path: &str) -> Result<String, String> {
    let esc = path.replace('\'', "''");
    let r = repo().replace('\'', "''");
    let sql = format!(
        "SELECT coalesce(string_agg(content, E'\\n\\n' ORDER BY ordinal), '(not found)') \
         FROM chunks c JOIN documents d ON d.id=c.document_id \
         WHERE d.repo='{r}' AND d.path='{esc}';");
    db_query(&sql).map(|s| s.trim_end().to_string())
}

fn tool_search_docs(query: &str, k: u32) -> Result<String, String> {
    // Tier 2: hybrid RRF search via the bundled search.py (which connects to the DB directly).
    // Query goes via stdin so no shell-quoting of arbitrary text is needed. repo() is sanitized.
    use std::process::{Command, Stdio};
    let kk = k.clamp(1, 20);
    // HM_RERANK -> the rerank orchestrator (fail-open to RRF); else the plain search.py (HM_SEARCH).
    let script = match std::env::var("HM_RERANK") {
        Ok(p) if !p.is_empty() => p,
        _ => script_path("HM_SEARCH", "ingest/search.py"),
    };
    let mut child = Command::new(py()).arg(script).arg(kk.to_string()).arg(repo())
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| format!("spawn search: {e}"))?;
    {
        let mut si = child.stdin.take().ok_or("no stdin")?;
        si.write_all(query.as_bytes()).map_err(|e| format!("write: {e}"))?;
        si.write_all(b"\n").ok();
    }
    let out = child.wait_with_output().map_err(|e| format!("wait: {e}"))?;
    if !out.status.success() {
        return Err(format!("search failed: {}", String::from_utf8_lossy(&out.stderr)));
    }
    let s = String::from_utf8_lossy(&out.stdout).trim_end().to_string();
    Ok(if s.is_empty() { "(no results -- embeddings may still be indexing)".to_string() } else { s })
}

fn tool_memory(cmd: &str, payload: &Value) -> Result<String, String> {
    // Personal memory: the bundled mem_ops.py (connects to the DB directly).
    // Payload goes via stdin as JSON -- no shell-quoting of arbitrary text.
    use std::process::{Command, Stdio};
    let script = script_path("HM_MEM_OPS", "ingest/mem_ops.py");
    let mut child = Command::new(py()).arg(script).arg(cmd)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| format!("spawn mem_ops: {e}"))?;
    {
        let mut si = child.stdin.take().ok_or("no stdin")?;
        si.write_all(payload.to_string().as_bytes()).map_err(|e| format!("write: {e}"))?;
    }
    let out = child.wait_with_output().map_err(|e| format!("wait: {e}"))?;
    if !out.status.success() {
        return Err(format!("mem_ops {cmd} failed: {}", String::from_utf8_lossy(&out.stderr)));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim_end().to_string())
}

// -- MCP JSON-RPC over stdio -------------------------------------------------
fn tool_defs() -> Value {
    json!([
        {"name":"get_project_map","description":"Tier 0: this workspace repo's component map + global invariants. Call first when orienting in the repo.","inputSchema":{"type":"object","properties":{}}},
        {"name":"get_constraints","description":"Tier 1: deterministic must/should constraints for the given changed file paths (touched components + globals + 1-hop graph). Call before editing config.","inputSchema":{"type":"object","properties":{"paths":{"type":"array","items":{"type":"string"},"description":"repo-relative file paths"}},"required":["paths"]}},
        {"name":"locate","description":"Where does X live? Substring search over component slugs/names/responsibilities/key_paths.","inputSchema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
        {"name":"get_document","description":"Fetch the full text of a doc in this workspace's repo by repo-relative path.","inputSchema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
        {"name":"search_docs","description":"Tier 2 hybrid search (bge-m3 vector + lexical fts, RRF, <=2 chunks/doc) over this workspace repo's doc corpus; returns top-k lines '[score] (sem#/lex#) repo:path # heading' + snippet. Both keyword/exact-symbol AND paraphrase queries work (lexical + semantic legs). Use for 'how/why/where' questions not answered by the map/constraints; fetch a promising doc in full via get_document.","inputSchema":{"type":"object","properties":{"query":{"type":"string"},"k":{"type":"integer","description":"results to return, 1-20 (default 8)"}},"required":["query"]}},
        {"name":"memory_search","description":"PERSONAL long-term memory of the owner (facts, preferences, decisions, episodes, plans -- cross-workspace; distinct from search_docs which is the doc corpus). Hybrid search, superseded/expired facts hidden by default. Search here before assuming owner preferences or past decisions.","inputSchema":{"type":"object","properties":{"query":{"type":"string"},"k":{"type":"integer","description":"default 8"},"types":{"type":"array","items":{"type":"string","enum":["semantic","episodic","preference","procedural","prospective","summary"]}},"project":{"type":"string","description":"also include memories tagged with this project"},"include_inactive":{"type":"boolean","description":"include superseded/retracted/expired history"}},"required":["query"]}},
        {"name":"memory_write","description":"Save a durable personal memory (fact/preference/decision/episode/plan). Only save what is NOT derivable from code/docs/git; prefer one self-contained natural-language sentence in content. Use valid_from/valid_to (event time) for facts with a validity window; assertions are an optional exact-lookup index (predicate + object).","inputSchema":{"type":"object","properties":{"type":{"type":"string","enum":["semantic","episodic","preference","procedural","prospective","summary"]},"content":{"type":"string"},"title":{"type":"string"},"importance":{"type":"number","description":"0-1, default 0.5"},"confidence":{"type":"number","description":"0-1, default 0.8"},"project":{"type":"string"},"subject":{"type":"object","properties":{"namespace":{"type":"string"},"entity_type":{"type":"string"},"name":{"type":"string"},"aliases":{"type":"array","items":{"type":"string"}}},"required":["namespace","entity_type","name"]},"valid_from":{"type":"string"},"valid_to":{"type":"string"},"event_time":{"type":"string"},"assertions":{"type":"array","items":{"type":"object","properties":{"predicate":{"type":"string"},"object_text":{"type":"string"},"object_number":{"type":"number"},"unit":{"type":"string"}},"required":["predicate"]}},"source":{"type":"object","properties":{"source_type":{"type":"string","enum":["user_message","assistant_inference","tool_result","manual","consolidation"]},"session_id":{"type":"string"},"channel":{"type":"string"},"excerpt":{"type":"string"}}}},"required":["type","content"]}},
        {"name":"memory_supersede","description":"Replace an outdated memory with a corrected/current version (old one is kept as history with its validity window closed -- never silently overwrite; use this instead of memory_write when a fact CHANGED).","inputSchema":{"type":"object","properties":{"old_id":{"type":"integer"},"content":{"type":"string"},"importance":{"type":"number"},"confidence":{"type":"number"},"valid_from":{"type":"string","description":"when the NEW fact became true (event time)"},"source":{"type":"object","properties":{"source_type":{"type":"string"},"excerpt":{"type":"string"}}}},"required":["old_id","content"]}},
        {"name":"memory_get","description":"Fetch one personal memory by id with its assertions, sources and supersede chain.","inputSchema":{"type":"object","properties":{"id":{"type":"integer"}},"required":["id"]}}
    ])
}

fn call_tool(name: &str, args: &Value) -> Result<String, String> {
    match name {
        "get_project_map" => Ok(tool_project_map(&graph()?)),
        "get_constraints" => {
            let paths: Vec<String> = args["paths"].as_array().map(|a|
                a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
            if paths.is_empty() { return Err("paths is required".into()); }
            Ok(tool_get_constraints(&graph()?, &paths))
        }
        "locate" => Ok(tool_locate(&graph()?, args["query"].as_str().unwrap_or(""))),
        "get_document" => tool_get_document(args["path"].as_str().unwrap_or("")),
        "search_docs" => {
            let q = args["query"].as_str().unwrap_or("");
            if q.is_empty() { return Err("query is required".into()); }
            let k = args["k"].as_u64().unwrap_or(8) as u32;
            tool_search_docs(q, k)
        }
        "memory_search" => tool_memory("search", args),
        "memory_write" => {
            let mut payload = args.clone();
            if payload.get("source").is_none() {
                payload["source"] = json!({"source_type": "assistant_inference", "channel": "mcp"});
            }
            tool_memory("write", &payload)
        }
        "memory_supersede" => tool_memory("supersede", args),
        "memory_get" => tool_memory("get", args),
        other => Err(format!("unknown tool: {other}")),
    }
}

fn reply(id: &Value, result: Value) {
    let msg = json!({"jsonrpc":"2.0","id":id,"result":result});
    println!("{msg}");
    let _ = std::io::stdout().flush();
}

fn reply_err(id: &Value, code: i64, message: &str) {
    let msg = json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":message}});
    println!("{msg}");
    let _ = std::io::stdout().flush();
}

fn main() {
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let line = match line { Ok(l) => l, Err(_) => break };
        if line.trim().is_empty() { continue; }
        let req: Value = match serde_json::from_str(&line) {
            Ok(v) => v, Err(e) => { eprintln!("hypermnesia-mcp: bad json: {e}"); continue; }
        };
        let method = req["method"].as_str().unwrap_or("");
        let id = req.get("id").cloned();

        // notifications carry no id and expect no response
        if id.is_none() { continue; }
        let id = id.unwrap();

        match method {
            "initialize" => {
                let pv = req["params"]["protocolVersion"].as_str().unwrap_or("2025-06-18");
                reply(&id, json!({
                    "protocolVersion": pv,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "hypermnesia", "version": "0.1.0"}
                }));
            }
            "tools/list" => reply(&id, json!({"tools": tool_defs()})),
            "tools/call" => {
                let name = req["params"]["name"].as_str().unwrap_or("");
                let args = req["params"].get("arguments").cloned().unwrap_or(json!({}));
                match call_tool(name, &args) {
                    Ok(text) => reply(&id, json!({"content":[{"type":"text","text":text}],"isError":false})),
                    Err(e) => reply(&id, json!({"content":[{"type":"text","text":format!("error: {e}")}],"isError":true})),
                }
            }
            "ping" => reply(&id, json!({})),
            _ => reply_err(&id, -32601, "method not found"),
        }
    }
}
