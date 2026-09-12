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
    if let Some(v) = config().get("HM_PSQL_CMD").filter(|s| !s.is_empty()) {
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

/// Why the config file must be refused, if it must be.
///
/// The file holds the command this console hands to `sh -c` -- at every refresh, and at login
/// with nobody watching, because the tray installs itself with RunAtLoad. So it gets the guard
/// the pipeline's settings file already has in hooks/_mem_common.py, and a stricter one: that
/// file can only set allowlisted variables, this one is arbitrary code.
///
/// A file that is not there is not a fault: there is a working default. `Ok(())` means "use it".
#[cfg(unix)]
pub fn config_fault(path: &std::path::Path) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    // Not libc: one extern declaration keeps the data layer dependency-free, which is the
    // reason a console built from it compiles in seconds.
    extern "C" { fn getuid() -> u32; }
    let Ok(md) = std::fs::metadata(path) else { return Ok(()) };
    let mode = md.mode() & 0o777;
    if mode & 0o022 != 0 {
        return Err(format!("{}: mode {mode:o} -- another account can write it, and what it \
                            holds is the command this console runs. Ignoring the whole file.",
                           path.display()));
    }
    let me = unsafe { getuid() };
    if md.uid() != me {
        return Err(format!("{}: owned by uid {}, not by you ({me}). Ignoring the whole file.",
                           path.display(), md.uid()));
    }
    Ok(())
}

#[cfg(not(unix))]
pub fn config_fault(_path: &std::path::Path) -> Result<(), String> { Ok(()) }

pub fn config() -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    let path = config_path();
    if let Err(why) = config_fault(&path) {
        // Said once per process: this is read on every refresh, and a warning printed sixty
        // times a minute is a warning nobody reads.
        use std::sync::atomic::{AtomicBool, Ordering};
        static SAID: AtomicBool = AtomicBool::new(false);
        if !SAID.swap(true, Ordering::Relaxed) {
            eprintln!("hypermnesia: {why}");
        }
        return out;
    }
    let Ok(text) = std::fs::read_to_string(&path) else { return out };
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
    // Created 0600, not written and then chmodded: the connection string can carry a password,
    // and between the write and the chmod the file exists at 0644 on a default Mac. And the
    // result is checked -- a password left readable because a chmod quietly failed is exactly
    // the kind of "it looked fine" this project is against.
    #[cfg(unix)]
    {
        use std::io::Write as _;
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new()
            .write(true).create(true).truncate(true).mode(0o600)
            .open(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        f.write_all(text.as_bytes()).map_err(|e| format!("{}: {e}", path.display()))?;
        // create() leaves an existing file's mode alone, so set it too.
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("{}: could not make it private ({e}) -- it may hold a \
                                  password", path.display()))?;
    }
    #[cfg(not(unix))]
    std::fs::write(&path, text).map_err(|e| format!("{}: {e}", path.display()))?;
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
        // Some of these fail with everything on stdout and nothing on stderr (a preset whose
        // placeholders were never filled in, `kubectl` printing usage). "the query failed: "
        // with nothing after the colon is the least useful sentence a console can print.
        let mut tail = err.lines().rev().take(3).collect::<Vec<_>>().join(" | ");
        if tail.trim().is_empty() {
            tail = out.lines().rev().take(3).collect::<Vec<_>>().join(" | ");
        }
        let code = st.code().map(|c| c.to_string()).unwrap_or_else(|| "a signal".into());
        if tail.trim().is_empty() {
            return Err(format!("the command exited {code} and said nothing at all"));
        }
        return Err(format!("the query failed (exit {code}): {tail}"));
    }
    Ok(out)
}

// -- parsing -------------------------------------------------------------------------------
// A small but STRUCTURAL JSON reader. The first version searched for `"key"` as a substring and
// took the digits after it, which is shorter and wrong in four separate ways, all of them
// silent: a repo named `docs` shadowed the `docs` key that followed it and the count read 0; a
// `]` inside a name ended a list early and the list came back empty; a missing key and an
// unparsable one both became 0; and any answer that was not this query's JSON parsed into a
// complete store full of zeroes. Every one of those prints a plausible number. A hundred lines
// of scanner is the cheaper half of that trade.

#[derive(Debug, Clone, PartialEq)]
enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(kv) => kv.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Num(n) => Some(*n as i64),
            _ => None,
        }
    }
    fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }
    fn as_arr(&self) -> Option<&[Json]> {
        match self {
            Json::Arr(v) => Some(v),
            _ => None,
        }
    }
}

struct Scanner<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Scanner<'a> {
    fn new(s: &'a str) -> Self { Self { b: s.as_bytes(), i: 0 } }

    fn ws(&mut self) {
        while self.i < self.b.len() && (self.b[self.i] as char).is_ascii_whitespace() {
            self.i += 1;
        }
    }

    fn value(&mut self) -> Result<Json, String> {
        self.ws();
        match self.b.get(self.i) {
            None => Err("unexpected end of JSON".into()),
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Json::Str(self.string()?)),
            Some(b't') => self.lit("true", Json::Bool(true)),
            Some(b'f') => self.lit("false", Json::Bool(false)),
            Some(b'n') => self.lit("null", Json::Null),
            Some(_) => self.number(),
        }
    }

    fn lit(&mut self, word: &str, v: Json) -> Result<Json, String> {
        if self.b[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            Ok(v)
        } else {
            Err(format!("expected {word} at byte {}", self.i))
        }
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.i;
        while self.i < self.b.len()
            && matches!(self.b[self.i], b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9') {
            self.i += 1;
        }
        std::str::from_utf8(&self.b[start..self.i]).ok()
            .and_then(|s| s.parse().ok())
            .map(Json::Num)
            .ok_or_else(|| format!("not a number at byte {start}"))
    }

    /// Reads a string INCLUDING its escapes, which is the whole point: a `"` or `]` inside a
    /// value must not be mistaken for structure.
    fn string(&mut self) -> Result<String, String> {
        if self.b.get(self.i) != Some(&b'"') {
            return Err(format!("expected a string at byte {}", self.i));
        }
        self.i += 1;
        let mut out = String::new();
        while let Some(&c) = self.b.get(self.i) {
            self.i += 1;
            match c {
                b'"' => return Ok(out),
                b'\\' => {
                    let e = *self.b.get(self.i).ok_or("unfinished escape")?;
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'n' => out.push('\n'),
                        b't' => out.push('\t'),
                        b'r' => out.push('\r'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'u' => {
                            let hex = self.b.get(self.i..self.i + 4)
                                .and_then(|h| std::str::from_utf8(h).ok())
                                .ok_or("short \\u escape")?;
                            let n = u32::from_str_radix(hex, 16).map_err(|e| e.to_string())?;
                            self.i += 4;
                            out.push(char::from_u32(n).unwrap_or('\u{fffd}'));
                        }
                        other => out.push(other as char),
                    }
                }
                _ => {
                    // Multi-byte UTF-8 passes through byte by byte; collecting bytes and
                    // converting at the end keeps names like `Документ` intact.
                    let start = self.i - 1;
                    let mut end = self.i;
                    while end < self.b.len() && self.b[end] & 0xC0 == 0x80 { end += 1; }
                    out.push_str(&String::from_utf8_lossy(&self.b[start..end]));
                    self.i = end;
                }
            }
        }
        Err("unterminated string".into())
    }

    fn object(&mut self) -> Result<Json, String> {
        self.i += 1;                              // '{'
        let mut kv = Vec::new();
        loop {
            self.ws();
            match self.b.get(self.i) {
                Some(b'}') => { self.i += 1; return Ok(Json::Obj(kv)); }
                Some(b',') => { self.i += 1; continue; }
                None => return Err("unterminated object".into()),
                _ => {}
            }
            let k = self.string()?;
            self.ws();
            if self.b.get(self.i) != Some(&b':') {
                return Err(format!("expected ':' after {k:?}"));
            }
            self.i += 1;
            kv.push((k, self.value()?));
        }
    }

    fn array(&mut self) -> Result<Json, String> {
        self.i += 1;                              // '['
        let mut out = Vec::new();
        loop {
            self.ws();
            match self.b.get(self.i) {
                Some(b']') => { self.i += 1; return Ok(Json::Arr(out)); }
                Some(b',') => { self.i += 1; continue; }
                None => return Err("unterminated array".into()),
                _ => out.push(self.value()?),
            }
        }
    }
}

fn parse_json(raw: &str) -> Result<Json, String> {
    let mut sc = Scanner::new(raw);
    let v = sc.value()?;
    sc.ws();
    if sc.i < sc.b.len() {
        return Err(format!("trailing data after the JSON value at byte {}", sc.i));
    }
    Ok(v)
}

fn parse(raw: &str) -> Result<Stats, String> {
    let raw = raw.trim();
    if raw.is_empty() {
        // An empty answer to a query that always returns exactly one row means the query did not
        // run -- not that there is no data.
        return Err("empty answer from the database (the query did not run?)".into());
    }
    let v = parse_json(raw).map_err(|e| {
        let head: String = raw.chars().take(120).collect();
        format!("the answer is not the JSON this query returns ({e}); it began: {head:?}")
    })?;

    // Two fields are required, and their absence is an ERROR rather than a zero. Everything the
    // console prints hangs off this answer, so an answer that merely parsed -- someone's
    // `SELECT 1`, a wrapper script's status line -- must not become a complete dashboard reading
    // zero everywhere. That is indistinguishable from a healthy empty store.
    let mem = v.get("memories")
        .ok_or("the answer parsed but has no \"memories\" -- this is not the stats query")?;
    let mut s = Stats {
        memories_total: mem.get("total").and_then(Json::as_i64)
            .ok_or("\"memories.total\" is missing or not a number")?,
        memories_active: mem.get("active").and_then(Json::as_i64)
            .ok_or("\"memories.active\" is missing or not a number")?,
        ..Stats::default()
    };
    s.pages = mem.get("pages").and_then(Json::as_i64).unwrap_or(0);
    s.by_type = counts(mem.get("by_type"));
    s.by_project = mem.get("by_project").and_then(Json::as_arr).map(|rows| {
        rows.iter().filter_map(|r| {
            let n = r.get("n").and_then(Json::as_i64)?;
            Some((r.get("project").and_then(Json::as_str).unwrap_or("(none)").to_string(), n))
        }).collect()
    }).unwrap_or_default();

    if let Some(q) = v.get("review_queue") {
        s.review_pending = q.get("pending").and_then(Json::as_i64).unwrap_or(0);
        s.review_oldest_days = q.get("oldest_days").and_then(Json::as_i64).unwrap_or(0);
    }
    s.stale = v.get("stale").and_then(Json::as_i64).unwrap_or(0);
    s.db_size = v.get("db_size").and_then(Json::as_str).unwrap_or_default().to_string();
    s.embedding_models = counts(v.get("embedding_models"));
    s.components = counts(v.get("components"));
    s.corpus = v.get("corpus").and_then(Json::as_arr).map(|rows| {
        rows.iter().filter_map(|r| Some(RepoStats {
            repo: r.get("repo").and_then(Json::as_str)?.to_string(),
            docs: r.get("docs").and_then(Json::as_i64).unwrap_or(0),
            chunks: r.get("chunks").and_then(Json::as_i64).unwrap_or(0),
            embedded: r.get("embedded").and_then(Json::as_i64).unwrap_or(0),
        })).collect()
    }).unwrap_or_default();
    Ok(s)
}

fn counts(v: Option<&Json>) -> BTreeMap<String, i64> {
    let mut out = BTreeMap::new();
    if let Some(Json::Obj(kv)) = v {
        for (k, val) in kv {
            if let Some(n) = val.as_i64() {
                out.insert(k.clone(), n);
            }
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

    /// The old parser searched for `"docs"` as a substring, so a repository *named* `docs` shadowed
    /// the key and every count in that row came out wrong. Reproduced before the rewrite:
    /// `docs=0 chunks=7780` for a row that says 438 docs.
    #[test]
    fn a_repo_named_like_a_key_does_not_shadow_it() {
        let raw = r#"{"memories":{"total":1,"active":1},"corpus":[{"repo":"docs","docs":438,"chunks":7780,"embedded":7780}]}"#;
        let s = parse(raw).expect("parse");
        assert_eq!(s.corpus.len(), 1);
        assert_eq!(s.corpus[0].repo, "docs");
        assert_eq!(s.corpus[0].docs, 438);
        assert_eq!(s.corpus[0].chunks, 7780);
    }

    /// The old list reader ended the array at the first `]` byte, so a bracket inside a project
    /// name silently dropped every project after it.
    #[test]
    fn a_bracket_inside_a_name_does_not_truncate_the_list() {
        let raw = r#"{"memories":{"total":2,"active":2,"by_project":[{"project":"a[1]","n":5},{"project":"b","n":7}]}}"#;
        let s = parse(raw).expect("parse");
        assert_eq!(s.by_project.len(), 2);
        assert_eq!(s.by_project[0].0, "a[1]");
        assert_eq!(s.by_project[1].1, 7);
    }

    /// Escapes belong to the string, not to the scanner: a quote or a backslash inside a name must
    /// not end it, and `\u` must decode.
    #[test]
    fn escapes_inside_names_are_decoded_not_obeyed() {
        let raw = r#"{"memories":{"total":1,"active":1,"by_project":[{"project":"a\"b\\cA","n":3}]}}"#;
        let s = parse(raw).expect("parse");
        assert_eq!(s.by_project[0].0, "a\"b\\cA");
    }

    /// Any other answer -- a different query, a notice, half a line -- must fail loudly. The old
    /// parser returned `Ok` with every field zero, which the console drew as a real, empty store.
    #[test]
    fn a_foreign_answer_is_an_error_not_an_empty_dashboard() {
        for raw in [
            r#"{"rows":[],"note":"wrong query"}"#,
            r#"{"memories":{"active":5}}"#,
            r#"{"memories":{"total":"682","active":483}}"#,
            "NOTICE:  relation does not exist",
            r#"{"memories":{"total":1,"active":1}} trailing"#,
        ] {
            assert!(parse(raw).is_err(), "parsed into zeroes instead of failing: {raw}");
        }
    }

    /// The config file is a command this console runs unattended at login. A file another
    /// account can write is refused whole, and the refusal is the value of this test: verified by
    /// hand before it was written, a mode-0666 config containing `touch FILE; echo "{}"` created
    /// the file and printed a clean empty dashboard.
    #[cfg(unix)]
    #[test]
    fn a_config_anyone_can_write_is_refused() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join("hm-console-cfg-test");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("console.conf");
        std::fs::write(&path, "HM_PSQL_CMD=echo nope\n").unwrap();

        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(config_fault(&path).is_ok(), "a private file must be usable");

        for bad in [0o666, 0o622, 0o662] {
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(bad)).unwrap();
            let why = config_fault(&path).expect_err("mode {bad:o} must be refused");
            assert!(why.contains("write"), "the refusal must say why: {why}");
        }

        // A file that is not there is not a fault -- there is a working default.
        assert!(config_fault(&dir.join("absent.conf")).is_ok());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unconfigured_console_still_has_a_command() {
        assert!(!DEFAULT_PSQL_CMD.trim().is_empty());
        assert!(DEFAULT_PSQL_CMD.contains("DATABASE_URL"),
                "the default must rest on the standard variable, not on someone's cluster");
    }
}
