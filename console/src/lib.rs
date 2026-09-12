//! Console data: one trip to the store, one picture of its state.
//!
//! Every counter comes back from ONE query returning one JSON object. Not for tidiness: a
//! connection to the store can cost a second or more (`ssh` to a gateway, `kubectl exec` into a
//! pod), so a console made of a dozen separate queries would take fifteen seconds to open and
//! half the numbers on screen would describe different moments in time.
//!
//! How it reaches the store is ONE setting, because that is the only thing deployments differ
//! by. See `Target`.

use std::collections::BTreeMap;
use std::io::Write;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub mod jobs;
pub mod settings;

pub const STATS_SQL: &str = include_str!("stats.sql");

/// How to reach the database.
///
/// Deployments differ by exactly one thing: the command that runs psql. A direct connection,
/// `docker exec`, `kubectl exec`, ssh to a machine that has kubectl -- all of them are one
/// string with different contents, and nothing else in the console depends on which. So there
/// is one setting here rather than five; five would mean every new deployment shape needs a new
/// field and a new release.
///
/// The command receives SQL on stdin and must print unaligned, untitled rows (`-tAX`).
/// `ON_ERROR_STOP=1` is not a preference: without it psql prints the error, exits 0 and returns
/// an empty string -- indistinguishable from "nothing matched".
pub struct Target {
    /// The whole command, as `sh -c` will run it.
    pub psql_cmd: String,
    pub timeout: Duration,
}

/// Ready-made shapes for common deployments. The setup wizard fills in the placeholders.
pub const PRESETS: &[(&str, &str)] = &[
    ("direct",
     "psql \"$DATABASE_URL\" -tAX -v ON_ERROR_STOP=1"),
    ("docker",
     "docker exec -i {container} psql -U {user} -d {db} -tAX -v ON_ERROR_STOP=1"),
    ("kubectl",
     "kubectl exec -i -n {namespace} deploy/{deploy} -- psql -U {user} -d {db} -tAX -v ON_ERROR_STOP=1"),
    ("ssh+kubectl",
     "ssh {host} kubectl exec -i -n {namespace} deploy/{deploy} -- psql -U {user} -d {db} -tAX -v ON_ERROR_STOP=1"),
];

pub const DEFAULT_PSQL_CMD: &str = "psql \"$DATABASE_URL\" -tAX -v ON_ERROR_STOP=1";

impl Default for Target {
    fn default() -> Self {
        Self {
            psql_cmd: resolve_psql_cmd(),
            timeout: Duration::from_secs(
                std::env::var("HM_TIMEOUT_SECS").ok()
                    .and_then(|v| v.parse().ok()).filter(|n| *n > 0).unwrap_or(30),
            ),
        }
    }
}

/// Environment, then config file, then the default -- explicit beats recorded, as everywhere
/// else here.
pub fn resolve_psql_cmd() -> String {
    if let Some(v) = std::env::var("HM_PSQL_CMD").ok().filter(|s| !s.is_empty()) {
        return v;
    }
    if let Some(v) = config().get("HM_PSQL_CMD") {
        return v.clone();
    }
    DEFAULT_PSQL_CMD.to_string()
}

/// The console's own config. Separate from the pipeline's settings file: this one holds what the
/// console needs in order to reach anything at all.
pub fn config_path() -> std::path::PathBuf {
    if let Some(p) = std::env::var("HM_CONSOLE_CONFIG").ok().filter(|s| !s.is_empty()) {
        return std::path::PathBuf::from(p);
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    std::path::PathBuf::from(home).join(".config/hypermnesia/console.conf")
}

pub fn config() -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    let Ok(text) = std::fs::read_to_string(config_path()) else { return out };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') { continue; }
        if let Some((k, v)) = line.split_once('=') {
            out.insert(k.trim().to_string(), v.trim().to_string());
        }
    }
    out
}

pub fn save_config(map: &BTreeMap<String, String>) -> Result<(), String> {
    let path = config_path();
    if let Some(d) = path.parent() {
        std::fs::create_dir_all(d).map_err(|e| format!("{}: {e}", d.display()))?;
    }
    let mut text = String::from(
        "# HyperMnesia console settings.\n\
         # HM_PSQL_CMD is the command that receives SQL on stdin. It is the only thing that\n\
         # differs between deployments; everything else the console does is the same.\n");
    for (k, v) in map {
        text.push_str(&format!("{k}={v}\n"));
    }
    std::fs::write(&path, text).map_err(|e| format!("{}: {e}", path.display()))?;
    // The connection string can carry a password.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// One reading of the store's state. Parsed by hand: pulling in a JSON crate for seven numbers
/// would trade build time for convenience this does not need.
#[derive(Debug, Default, Clone)]
pub struct Stats {
    pub memories_total: i64,
    pub memories_active: i64,
    pub pages: i64,
    pub by_type: BTreeMap<String, i64>,
    pub by_project: Vec<(String, i64)>,
    pub review_pending: i64,
    pub review_oldest_days: i64,
    pub stale: i64,
    pub corpus: Vec<RepoStats>,
    pub embedding_models: BTreeMap<String, i64>,
    pub components: BTreeMap<String, i64>,
    pub db_size: String,
    /// How long the reading took. Shown on screen: a console that has itself become slow should
    /// say so rather than look thoughtful.
    pub took: Duration,
}

#[derive(Debug, Default, Clone)]
pub struct RepoStats {
    pub repo: String,
    pub docs: i64,
    pub chunks: i64,
    pub embedded: i64,
}

impl RepoStats {
    /// A knowledge page published as a document (`<project>~mem`), not a repo's real corpus.
    pub fn is_memory_page(&self) -> bool {
        self.repo.ends_with("~mem")
    }
}

pub fn fetch(t: &Target) -> Result<Stats, String> {
    let started = Instant::now();
    let raw = fetch_raw(t)?;
    let mut s = parse(&raw)?;
    s.took = started.elapsed();
    Ok(s)
}

/// The store's answer as it arrived. Separate from `fetch` so raw and parsed can be compared:
/// the parser here is ours, and silently losing a field is the likeliest way for it to lie.
pub fn fetch_raw(t: &Target) -> Result<String, String> {
    run_with_timeout(&t.psql_cmd, STATS_SQL, t.timeout)
}

/// Connectivity check: the cheapest query that must return a row. The setup wizard needs it --
/// a setting nobody tried is not a setting, it is a guess.
pub fn probe(t: &Target) -> Result<String, String> {
    let out = run_with_timeout(&t.psql_cmd, "SELECT current_database() || $$ @ $$ || version();",
                               t.timeout)?;
    let line = out.trim();
    if line.is_empty() {
        return Err("the command ran but returned nothing -- psql most likely never reached a \
                    database".into());
    }
    Ok(line.to_string())
}

fn run_with_timeout(cmd: &str, stdin_text: &str, timeout: Duration) -> Result<String, String> {
    use std::sync::mpsc;
    // `sh -c` deliberately: the command is written by a person and contains quotes, expansions
    // and pipelines (`ssh host kubectl exec … -- psql …`). Parsing it ourselves would mean
    // writing an incomplete shell. Its source is the config file of whoever owns the machine,
    // not the network.
    let mut child = Command::new("sh")
        .arg("-c").arg(cmd)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| format!("could not start sh: {e}"))?;
    {
        let mut si = child.stdin.take().ok_or("no stdin on the child")?;
        si.write_all(stdin_text.as_bytes()).map_err(|e| format!("writing the query: {e}"))?;
    }

    // Readers on their own threads, and channels rather than join: killing a process does not
    // close the pipe if something it spawned inherited stdout, and a join would then wait for
    // the grandchild -- far past the deadline. Measured in this project's MCP server: a 3s
    // timeout returned after 30.
    let (tx_o, rx_o) = mpsc::channel();
    let (tx_e, rx_e) = mpsc::channel();
    let mut so = child.stdout.take();
    let mut se = child.stderr.take();
    std::thread::spawn(move || {
        let mut b = Vec::new();
        if let Some(h) = so.as_mut() { use std::io::Read; let _ = h.read_to_end(&mut b); }
        let _ = tx_o.send(String::from_utf8_lossy(&b).into_owned());
    });
    std::thread::spawn(move || {
        let mut b = Vec::new();
        if let Some(h) = se.as_mut() { use std::io::Read; let _ = h.read_to_end(&mut b); }
        let _ = tx_e.send(String::from_utf8_lossy(&b).into_owned());
    });

    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Some(st),
            Ok(None) => {
                if Instant::now() >= deadline { break None; }
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(e) => return Err(format!("waiting on the child: {e}")),
        }
    };
    let Some(st) = status else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(format!("no answer within {}s", timeout.as_secs()));
    };
    let grace = Duration::from_secs(5);
    let out = rx_o.recv_timeout(grace)
        .map_err(|_| "the command exited but its output never arrived".to_string())?;
    let err = rx_e.recv_timeout(grace).unwrap_or_default();
    if !st.success() {
        let tail = err.lines().rev().take(3).collect::<Vec<_>>().join(" | ");
        return Err(format!("the query failed: {tail}"));
    }
    Ok(out)
}

// -- parsing -------------------------------------------------------------------------------
// A small parser for exactly the shape stats.sql returns. It is NOT general: the source of this
// JSON is our own query, not the network, and that is the trade being made.

fn parse(raw: &str) -> Result<Stats, String> {
    let raw = raw.trim();
    if raw.is_empty() {
        // An empty answer to a query that always returns exactly one row means the query did not
        // run -- not that there is no data.
        return Err("empty answer from the database (the query did not run?)".into());
    }
    let mut s = Stats::default();
    s.memories_total = num(raw, "\"memories\"", "\"total\"").unwrap_or(0);
    s.memories_active = num(raw, "\"memories\"", "\"active\"").unwrap_or(0);
    s.pages = num(raw, "\"memories\"", "\"pages\"").unwrap_or(0);
    s.review_pending = num(raw, "\"review_queue\"", "\"pending\"").unwrap_or(0);
    s.review_oldest_days = num(raw, "\"review_queue\"", "\"oldest_days\"").unwrap_or(0);
    s.stale = top_num(raw, "\"stale\"").unwrap_or(0);
    s.db_size = top_str(raw, "\"db_size\"").unwrap_or_default();
    s.by_type = obj_counts(section(raw, "\"by_type\""));
    s.embedding_models = obj_counts(section(raw, "\"embedding_models\""));
    s.components = obj_counts(section(raw, "\"components\""));
    s.by_project = pairs(section(raw, "\"by_project\""), "project", "n");
    s.corpus = section(raw, "\"corpus\"").map(repo_rows).unwrap_or_default();
    Ok(s)
}

fn section<'a>(raw: &'a str, key: &str) -> Option<&'a str> {
    let i = raw.find(key)? + key.len();
    let rest = &raw[i..];
    let open = rest.find(|c| c == '{' || c == '[')?;
    let opener = rest.as_bytes()[open] as char;
    let closer = if opener == '{' { '}' } else { ']' };
    let mut depth = 0usize;
    for (j, c) in rest[open..].char_indices() {
        if c == opener { depth += 1; }
        if c == closer {
            depth -= 1;
            if depth == 0 { return Some(&rest[open..open + j + 1]); }
        }
    }
    None
}

fn num(raw: &str, outer: &str, key: &str) -> Option<i64> {
    top_num(section(raw, outer)?, key)
}

fn top_num(raw: &str, key: &str) -> Option<i64> {
    let i = raw.find(key)? + key.len();
    let rest = raw[i..].trim_start().trim_start_matches(':').trim_start();
    let end = rest.find(|c: char| !c.is_ascii_digit() && c != '-').unwrap_or(rest.len());
    rest[..end].parse().ok()
}

fn top_str(raw: &str, key: &str) -> Option<String> {
    let i = raw.find(key)? + key.len();
    let rest = raw[i..].trim_start().trim_start_matches(':').trim_start();
    let rest = rest.strip_prefix('"')?;
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

fn obj_counts(sec: Option<&str>) -> BTreeMap<String, i64> {
    let mut out = BTreeMap::new();
    let Some(sec) = sec else { return out };
    let body = sec.trim_start_matches('{').trim_end_matches('}');
    let mut rest = body;
    while let Some(q) = rest.find('"') {
        let after = &rest[q + 1..];
        let Some(end) = after.find('"') else { break };
        let name = &after[..end];
        let tail = &after[end + 1..];
        let tail = tail.trim_start().trim_start_matches(':').trim_start();
        let n_end = tail.find(|c: char| !c.is_ascii_digit()).unwrap_or(tail.len());
        if let Ok(n) = tail[..n_end].parse::<i64>() {
            out.insert(name.to_string(), n);
        }
        rest = &tail[n_end..];
    }
    out
}

fn pairs(sec: Option<&str>, name_key: &str, num_key: &str) -> Vec<(String, i64)> {
    let mut out = Vec::new();
    let Some(sec) = sec else { return out };
    for item in objects(sec) {
        let name = top_str(item, &format!("\"{name_key}\"")).unwrap_or_default();
        let n = top_num(item, &format!("\"{num_key}\"")).unwrap_or(0);
        if !name.is_empty() { out.push((name, n)); }
    }
    out
}

fn repo_rows(sec: &str) -> Vec<RepoStats> {
    objects(sec).into_iter().map(|item| RepoStats {
        repo: top_str(item, "\"repo\"").unwrap_or_default(),
        docs: top_num(item, "\"docs\"").unwrap_or(0),
        chunks: top_num(item, "\"chunks\"").unwrap_or(0),
        embedded: top_num(item, "\"embedded\"").unwrap_or(0),
    }).filter(|r| !r.repo.is_empty()).collect()
}

/// Top-level `{...}` pieces inside an array.
fn objects(sec: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut depth = 0usize;
    let mut start = 0usize;
    for (i, c) in sec.char_indices() {
        if c == '{' {
            if depth == 0 { start = i; }
            depth += 1;
        } else if c == '}' && depth > 0 {
            depth -= 1;
            if depth == 0 { out.push(&sec[start..=i]); }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    // A shortened snapshot of a real answer from a live store. The parser is checked against the
    // shape stats.sql actually returns, not one invented for the test.
    const SAMPLE: &str = r#"{"memories" : {"total" : 682, "active" : 483, "by_type" : { "semantic" : 90, "preference" : 139 }, "by_project" : [{"project" : "(none)", "n" : 83}, {"project" : "myrepo", "n" : 68}], "pages" : 17}, "review_queue" : {"pending" : 0, "oldest_days" : 0}, "stale" : 0, "corpus" : [{"repo" : "myrepo", "docs" : 438, "chunks" : 7780, "embedded" : 7780}, {"repo" : "myrepo~mem", "docs" : 1, "chunks" : 1, "embedded" : 1}], "embedding_models" : { "bge-m3" : 25035 }, "components" : { "myrepo" : 20, "infra" : 38 }, "db_size" : "430 MB"}"#;

    #[test]
    fn parses_a_real_answer() {
        let s = parse(SAMPLE).expect("parse");
        assert_eq!(s.memories_total, 682);
        assert_eq!(s.memories_active, 483);
        assert_eq!(s.pages, 17);
        assert_eq!(s.by_type.get("preference"), Some(&139));
        assert_eq!(s.by_project.first().map(|p| p.0.as_str()), Some("(none)"));
        assert_eq!(s.by_project.len(), 2);
        assert_eq!(s.review_pending, 0);
        assert_eq!(s.db_size, "430 MB");
        assert_eq!(s.embedding_models.get("bge-m3"), Some(&25035));
        assert_eq!(s.components.get("infra"), Some(&38));
        assert_eq!(s.corpus.len(), 2);
        assert_eq!(s.corpus[0].docs, 438);
        assert!(!s.corpus[0].is_memory_page());
        assert!(s.corpus[1].is_memory_page());
    }

    /// An empty answer to a query that always returns a row is a failure, not "no data" -- the
    /// substitution this whole project exists to prevent.
    #[test]
    fn empty_answer_is_an_error_not_zeroes() {
        assert!(parse("   ").is_err());
    }

    /// Every preset must ask psql for raw output and for a hard stop on error. Without `-tAX`
    /// the parser gets table borders; without ON_ERROR_STOP an error comes back as an empty
    /// string with exit 0 -- indistinguishable from "nothing matched".
    #[test]
    fn every_preset_asks_psql_for_raw_output_and_hard_errors() {
        for (name, cmd) in PRESETS {
            assert!(cmd.contains("-tAX"), "{name}: no -tAX");
            assert!(cmd.contains("ON_ERROR_STOP=1"), "{name}: no ON_ERROR_STOP");
            assert!(cmd.contains("psql"), "{name}: not psql at all");
        }
        assert!(DEFAULT_PSQL_CMD.contains("ON_ERROR_STOP=1"));
    }

    #[test]
    fn an_unconfigured_console_still_has_a_command() {
        assert!(!DEFAULT_PSQL_CMD.trim().is_empty());
        assert!(DEFAULT_PSQL_CMD.contains("DATABASE_URL"),
                "the default must rest on the standard variable, not on someone's cluster");
    }
}
