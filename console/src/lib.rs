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
pub mod view;

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

/// The longest timeout that can be asked for. Not a preference: `Instant::now() + timeout`
/// panics on a duration near `u64::MAX`, and `HM_TIMEOUT_SECS=18446744073709551615` did exactly
/// that. An hour is far past any query this console runs.
const MAX_TIMEOUT_SECS: u64 = 3600;

impl Target {
    /// Read the environment and the config, or say why neither can be trusted.
    ///
    /// There is no `Default` any more, and that is the point: it could only return a `Target`,
    /// so a refused config had to become the DEFAULT psql command -- a different database,
    /// reported as a healthy reading. A fallible constructor lets the refusal reach the screen.
    pub fn from_env() -> Result<Self, String> {
        Ok(Self { psql_cmd: resolve_psql_cmd()?, timeout: timeout_from_env() })
    }

    /// A target for a command the caller already has in hand -- the setup wizard trying one
    /// before it is written anywhere. It reads no config, so a config that is being repaired
    /// cannot stop the repair.
    pub fn with_cmd(psql_cmd: String) -> Self {
        Self { psql_cmd, timeout: timeout_from_env() }
    }
}

fn timeout_from_env() -> Duration {
    Duration::from_secs(
        std::env::var("HM_TIMEOUT_SECS").ok()
            .and_then(|v| v.parse().ok())
            .filter(|n| *n > 0 && *n <= MAX_TIMEOUT_SECS)
            .unwrap_or(30),
    )
}

/// Environment, then config file, then the default -- explicit beats recorded, as everywhere
/// else here. A config that cannot be trusted is an error, NOT a fall-through to the default:
/// the default reaches whatever `$DATABASE_URL` points at, and its numbers are indistinguishable
/// on screen from the ones the person was asking for.
pub fn resolve_psql_cmd() -> Result<String, String> {
    if let Some(v) = std::env::var("HM_PSQL_CMD").ok().filter(|s| !s.is_empty()) {
        return Ok(v);
    }
    if let Some(v) = config()?.get("HM_PSQL_CMD").filter(|s| !s.is_empty()) {
        return Ok(v.clone());
    }
    Ok(DEFAULT_PSQL_CMD.to_string())
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

/// Why the config file must be refused, if it must be -- judged on an ALREADY OPEN descriptor.
///
/// The file holds the command this console hands to `sh -c` -- at every refresh, and at login
/// with nobody watching, because the tray installs itself with RunAtLoad. So it gets the guard
/// the pipeline's settings file already has in hooks/_mem_common.py, and a stricter one: that
/// file can only set allowlisted variables, this one is arbitrary code.
///
/// Taking the metadata from the descriptor rather than from the path is the whole point. The
/// previous version called `metadata(path)` and then `read_to_string(path)` -- two separate
/// lookups, so anyone able to create entries in that directory could let the checked file be the
/// person's own and the READ file be theirs.
#[cfg(unix)]
pub fn fd_fault(path: &std::path::Path, md: &std::fs::Metadata) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    // Not libc: one extern declaration keeps the data layer dependency-free, which is the
    // reason a console built from it compiles in seconds.
    extern "C" { fn getuid() -> u32; }
    if !md.is_file() {
        // A directory, a device or a fifo here is a misconfigured HM_CONSOLE_CONFIG. Reading it
        // fails, and falling back to the default without a word is how the console ends up
        // talking to a different store than the person configured.
        return Err(format!("{} is not a file, so nothing can be read from it", path.display()));
    }
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

/// Open the config, check the descriptor, read the same descriptor.
///
/// `Ok(None)` means the file is not there, which is a working state: there is a default. Every
/// other failure is an `Err`, and the difference matters more here than anywhere else in this
/// file -- see `config`.
#[cfg(unix)]
fn read_config_file(path: &std::path::Path) -> Result<Option<String>, String> {
    use std::io::Read;
    use std::os::unix::fs::OpenOptionsExt;
    // O_NONBLOCK so that a fifo left at this path cannot hang the console at login instead of
    // being refused. The two values this project builds for; the constant is not in std.
    #[cfg(target_os = "linux")]
    const O_NONBLOCK: i32 = 0o4000;
    #[cfg(not(target_os = "linux"))]
    const O_NONBLOCK: i32 = 0x0004;

    let f = match std::fs::OpenOptions::new().read(true).custom_flags(O_NONBLOCK).open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("{}: {e}", path.display())),
    };
    let md = f.metadata().map_err(|e| format!("{}: {e}", path.display()))?;
    fd_fault(path, &md)?;
    let mut text = String::new();
    (&f).read_to_string(&mut text).map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(Some(text))
}

#[cfg(not(unix))]
fn read_config_file(path: &std::path::Path) -> Result<Option<String>, String> {
    match std::fs::read_to_string(path) {
        Ok(t) => Ok(Some(t)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

/// The console's settings -- or the reason they cannot be used.
///
/// `Ok` with an empty map means there is no config file. That is the ONLY absence this returns
/// quietly. A refused file, an unreadable one, a directory where a file should be: each used to
/// become an empty map too, and an empty map means the DEFAULT psql command -- another
/// database. Its figures then arrived in the tray as a fresh, successful reading of a store
/// nobody configured, which is this project's own defect class at its worst: not a stale number
/// but somebody else's number.
pub fn config() -> Result<BTreeMap<String, String>, String> {
    let path = config_path();
    let mut out = BTreeMap::new();
    let Some(text) = read_config_file(&path)? else { return Ok(out) };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') { continue; }
        if let Some((k, v)) = line.split_once('=') {
            out.insert(k.trim().to_string(), v.trim().to_string());
        }
    }
    Ok(out)
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
    // Written to a NEW neighbouring file and renamed over the destination. Writing in place had
    // three failure modes, all of them quiet: an existing 0644 file kept that mode while the new
    // contents -- which can carry a password -- were already on disk; the truncate happened
    // before the write, so a failure left an empty live config while setup printed "Not
    // written"; and a chmod by pathname lands on whatever is at the pathname by then.
    #[cfg(unix)]
    {
        use std::io::Write as _;
        use std::os::unix::fs::OpenOptionsExt;
        let name = path.file_name().map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "console.conf".into());
        let tmp = path.with_file_name(format!("{name}.new"));
        // A leftover from an interrupted save would otherwise block every future one. Removing
        // it first is safe in a directory only this account can write -- which is the same
        // assumption the guard above already makes about the config itself.
        let _ = std::fs::remove_file(&tmp);
        let written = (|| -> std::io::Result<()> {
            let mut f = std::fs::OpenOptions::new()
                .write(true).create_new(true).mode(0o600).open(&tmp)?;
            f.write_all(text.as_bytes())?;
            f.sync_all()?;                    // renaming over a config that is still in the
            drop(f);                          // page cache is how an empty one survives a crash
            std::fs::rename(&tmp, &path)
        })();
        if let Err(e) = written {
            let _ = std::fs::remove_file(&tmp);
            return Err(format!("{}: {e} -- the previous settings are untouched", path.display()));
        }
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
    /// Chunks whose `embedding_model` is NULL. Its own field rather than an entry in the map:
    /// the query used to name that bucket "(null)", which a model actually called that would
    /// have merged with -- and a merged count of two different things reads as one healthy one.
    pub embedding_unset: i64,
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

/// The most output an answer may be. This query returns a few kilobytes; a command that has
/// produced sixteen megabytes is not psql answering it, and reading on until memory runs out is
/// a worse failure than saying so.
const MAX_OUTPUT: usize = 16 << 20;

/// The smallest amount of time the readers get after the child exits, when the deadline itself
/// has already run out. Without it a child that finishes one millisecond before the deadline
/// would have its perfectly good output declared missing.
const COLLECT_FLOOR: Duration = Duration::from_millis(200);

/// Read one pipe to the end, bounded. Read errors are carried out rather than dropped: a
/// truncated answer that arrives as `Ok` is the silent kind of wrong this file is about.
fn drain(h: &mut Option<std::process::ChildStdout>, which: &str) -> Result<Vec<u8>, String> {
    use std::io::Read;
    let Some(h) = h.as_mut() else { return Ok(Vec::new()) };
    let mut b = Vec::new();
    // One byte past the limit, so "exactly at the limit" and "more than the limit" are
    // distinguishable rather than both looking like a complete answer.
    h.take(MAX_OUTPUT as u64 + 1).read_to_end(&mut b)
        .map_err(|e| format!("reading the command's {which}: {e}"))?;
    if b.len() > MAX_OUTPUT {
        return Err(format!("the command produced more than {} MiB on {which}",
                           MAX_OUTPUT >> 20));
    }
    Ok(b)
}

fn run_with_timeout(cmd: &str, stdin_text: &str, timeout: Duration) -> Result<String, String> {
    use std::sync::mpsc;
    // ONE deadline, taken before anything starts. It used to begin after the query had already
    // been written to the child, so the write itself was outside the timeout: a query larger
    // than the pipe's capacity blocked for ever if the child never read it, and a child that
    // filled its own output pipe while we were still writing deadlocked both ends.
    let deadline = Instant::now() + timeout;
    // `sh -c` deliberately: the command is written by a person and contains quotes, expansions
    // and pipelines (`ssh host kubectl exec … -- psql …`). Parsing it ourselves would mean
    // writing an incomplete shell. Its source is the config file of whoever owns the machine,
    // not the network.
    let mut child = Command::new("sh");
    child.arg("-c").arg(cmd)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    // Its own process group, so the timeout can kill the WHOLE tree. Measured before this:
    // killing only `sh` left the ssh and kubectl it had started running after the deadline --
    // a console that reports "no answer within 30s" while its query keeps working on the other
    // end, once a minute, forever.
    #[cfg(unix)]
    unsafe {
        use std::os::unix::process::CommandExt;
        child.pre_exec(|| {
            // setpgid(0, 0): the child leads a new group, so killpg reaches its descendants too.
            extern "C" { fn setpgid(pid: i32, pgid: i32) -> i32; }
            if setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = child.spawn().map_err(|e| format!("could not start sh: {e}"))?;

    // Readers on their own threads, and channels rather than join: killing a process does not
    // close the pipe if something it spawned inherited stdout, and a join would then wait for
    // the grandchild -- far past the deadline. Measured in this project's MCP server: a 3s
    // timeout returned after 30.
    //
    // They start BEFORE the query is written, and the write gets a thread of its own, so that
    // no phase of this can block another. All three are only ever waited for against the one
    // deadline above.
    let (tx_o, rx_o) = mpsc::channel();
    let (tx_e, rx_e) = mpsc::channel();
    let (tx_w, rx_w) = mpsc::channel();
    let mut so = child.stdout.take();
    let mut se = child.stderr.take();
    std::thread::spawn(move || { let _ = tx_o.send(drain(&mut so, "stdout")); });
    std::thread::spawn(move || {
        // stderr is read through the same bounded path; the handle types differ, so it gets its
        // own two lines rather than a generic.
        use std::io::Read;
        let r = (|| -> Result<Vec<u8>, String> {
            let Some(h) = se.as_mut() else { return Ok(Vec::new()) };
            let mut b = Vec::new();
            h.take(MAX_OUTPUT as u64 + 1).read_to_end(&mut b)
                .map_err(|e| format!("reading the command's stderr: {e}"))?;
            b.truncate(MAX_OUTPUT);
            Ok(b)
        })();
        let _ = tx_e.send(r);
    });
    let mut si = child.stdin.take().ok_or("no stdin on the child")?;
    let query = stdin_text.to_string();
    std::thread::spawn(move || {
        let r = si.write_all(query.as_bytes()).map_err(|e| e.to_string());
        drop(si);                      // the child needs to see the end of its input
        let _ = tx_w.send(r);
    });

    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Some(st),
            Ok(None) => {
                if Instant::now() >= deadline { break None; }
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(e) => { kill_tree(&mut child); return Err(format!("waiting on the child: {e}")); }
        }
    };
    let Some(st) = status else {
        kill_tree(&mut child);
        return Err(format!("no answer within {}s", timeout.as_secs()));
    };

    // What is LEFT of the deadline, not a fresh five seconds. A descendant holding stdout open
    // used to add five seconds to every such run and then be reported as "output never
    // arrived"; one holding only stderr was worse -- it added the same five seconds and then
    // succeeded, with stderr silently empty. Either way the group was left alive.
    let collect = deadline.saturating_duration_since(Instant::now()).max(COLLECT_FLOOR);
    let out = match rx_o.recv_timeout(collect) {
        Ok(r) => match r {
            Ok(bytes) => String::from_utf8(bytes)
                // The earliest point at which two different names became the same `?`: by the
                // time the parser saw them they were genuinely identical, so no amount of care
                // further in could have told them apart. A psql that returns something which is
                // not text is a failure, not a name.
                .map_err(|_| "the command's output is not valid UTF-8".to_string()),
            Err(e) => Err(e),
        },
        Err(_) => {
            kill_tree(&mut child);
            Err("the command exited but something it started is still holding its output open"
                .to_string())
        }
    };
    let err = match rx_e.recv_timeout(COLLECT_FLOOR) {
        Ok(Ok(b)) => String::from_utf8_lossy(&b).into_owned(),   // for a person to read
        Ok(Err(e)) => e,
        Err(_) => "(its error output is still held open by something it started)".to_string(),
    };
    // Checked after the exit status, never before it: psql that fails on the first line exits
    // while the rest of the query is still being written, and the EPIPE that causes is the
    // symptom. Reporting it instead of the real error is how "syntax error at or near" became
    // "writing the query: Broken pipe".
    let write_failed = matches!(rx_w.recv_timeout(COLLECT_FLOOR), Ok(Err(_)));

    // Our own reason for refusing the output comes before the exit status. When the size limit
    // is what stopped the read, the child dies of SIGPIPE BECAUSE of that -- and "the command
    // exited a signal and said nothing at all" would name a symptom this console caused rather
    // than the cause it knows.
    let out = out?;
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
    if write_failed {
        // A clean exit that never took the whole query answered a DIFFERENT question from the
        // one asked. Whatever it printed is not this query's reading.
        return Err("the command exited successfully without reading the whole query, so what \
                    it answered is not what was asked".to_string());
    }
    Ok(out)
}

/// End the child AND everything it started. `child.kill()` reaches only `sh`; a preset like
/// `ssh host kubectl exec ...` leaves both of those behind, still talking to the store.
fn kill_tree(child: &mut std::process::Child) {
    #[cfg(unix)]
    {
        extern "C" { fn killpg(pgrp: i32, sig: i32) -> i32; }
        // The group id is the child's pid: it was made a group leader before exec.
        let pgid = child.id() as i32;
        unsafe { killpg(pgid, 15); }                 // SIGTERM to the group
        std::thread::sleep(Duration::from_millis(200));
        unsafe { killpg(pgid, 9); }                  // and then SIGKILL to whatever ignored it
    }
    let _ = child.kill();
    let _ = child.wait();
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
pub(crate) enum Json {
    Null,
    Bool(bool),
    /// The number's TOKEN, exactly as it arrived. Not an `f64`: every figure in this answer is a
    /// SQL `count(*)`, and the round trip through a float changed them without a word --
    /// `9007199254740993` came out `…992`, `7.9` came out `7`, `1e999` came out `i64::MAX`, all
    /// with a successful exit. Keeping the token means an unrepresentable count is an absent
    /// number, and an absent required number is an error.
    Num(String),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub(crate) fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(kv) => kv.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub(crate) fn as_i64(&self) -> Option<i64> {
        match self {
            // A fraction, an exponent or anything past i64 fails here rather than being
            // rounded, truncated or saturated into a plausible count.
            Json::Num(tok) => tok.parse().ok(),
            _ => None,
        }
    }
    pub(crate) fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }
    pub(crate) fn as_arr(&self) -> Option<&[Json]> {
        match self {
            Json::Arr(v) => Some(v),
            _ => None,
        }
    }
    pub(crate) fn is_null(&self) -> bool { matches!(self, Json::Null) }
}

/// How deep a value may nest. This query returns three levels; a hundred thousand nested arrays
/// in a field nobody reads used to abort the whole console with a stack overflow, which is a
/// worse answer than any error message.
const MAX_DEPTH: usize = 32;

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

    fn value(&mut self, depth: usize) -> Result<Json, String> {
        if depth > MAX_DEPTH {
            return Err(format!("nested more than {MAX_DEPTH} deep at byte {}", self.i));
        }
        self.ws();
        match self.b.get(self.i) {
            None => Err("unexpected end of JSON".into()),
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
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

    fn digits(&mut self) -> usize {
        let start = self.i;
        while self.i < self.b.len() && self.b[self.i].is_ascii_digit() { self.i += 1; }
        self.i - start
    }

    /// JSON's number grammar, enforced. The previous version swallowed any run of `-+.eE0-9`
    /// and handed it to a float parser, which happily accepted `+01`, `.5` and `1e999`. A
    /// console that reads a damaged answer as a number is the thing this file exists to prevent.
    fn number(&mut self) -> Result<Json, String> {
        let start = self.i;
        let bad = |i: usize| format!("not a number at byte {i}");
        if self.b.get(self.i) == Some(&b'-') { self.i += 1; }
        match self.b.get(self.i) {
            Some(b'0') => {
                self.i += 1;
                if self.b.get(self.i).is_some_and(u8::is_ascii_digit) {
                    return Err(format!("leading zero at byte {start}"));
                }
            }
            Some(c) if c.is_ascii_digit() => { self.digits(); }
            _ => return Err(bad(start)),
        }
        if self.b.get(self.i) == Some(&b'.') {
            self.i += 1;
            if self.digits() == 0 { return Err(bad(start)); }
        }
        if matches!(self.b.get(self.i), Some(b'e') | Some(b'E')) {
            self.i += 1;
            if matches!(self.b.get(self.i), Some(b'+') | Some(b'-')) { self.i += 1; }
            if self.digits() == 0 { return Err(bad(start)); }
        }
        std::str::from_utf8(&self.b[start..self.i]).map(|s| Json::Num(s.to_string()))
            .map_err(|_| bad(start))
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let hex = self.b.get(self.i..self.i + 4)
            .and_then(|h| std::str::from_utf8(h).ok())
            .ok_or("short \\u escape")?;
        let n = u32::from_str_radix(hex, 16).map_err(|_| format!("bad \\u escape {hex:?}"))?;
        self.i += 4;
        Ok(n)
    }

    /// Reads a string INCLUDING its escapes, which is the whole point: a `"` or `]` inside a
    /// value must not be mistaken for structure.
    ///
    /// Bytes are collected and validated as UTF-8 once at the end -- the same rule the reader
    /// in `run_with_timeout` now applies to the whole answer, which is where raw bad bytes are
    /// actually stopped. Replacing bad input with `U+FFFD` as it went was not leniency but data
    /// loss: two different model names decoded to the same `?`, overwrote one another in the
    /// count map, and took the "more than one embedding model" warning down with them.
    fn string(&mut self) -> Result<String, String> {
        if self.b.get(self.i) != Some(&b'"') {
            return Err(format!("expected a string at byte {}", self.i));
        }
        self.i += 1;
        let mut out: Vec<u8> = Vec::new();
        while let Some(&c) = self.b.get(self.i) {
            self.i += 1;
            match c {
                b'"' => return String::from_utf8(out)
                    .map_err(|_| "a string in the answer is not valid UTF-8".to_string()),
                b'\\' => {
                    let e = *self.b.get(self.i).ok_or("unfinished escape")?;
                    self.i += 1;
                    let ch = match e {
                        b'"' => '"',
                        b'\\' => '\\',
                        b'/' => '/',
                        b'n' => '\n',
                        b't' => '\t',
                        b'r' => '\r',
                        b'b' => '\u{8}',
                        b'f' => '\u{c}',
                        b'u' => {
                            let n = self.hex4()?;
                            match n {
                                // A high surrogate must be followed by its low half: the pair is
                                // ONE character. Decoded separately they both became U+FFFD.
                                0xD800..=0xDBFF => {
                                    if self.b.get(self.i..self.i + 2) != Some(b"\\u") {
                                        return Err("a \\u escape is half of a surrogate pair \
                                                    with nothing after it".into());
                                    }
                                    self.i += 2;
                                    let lo = self.hex4()?;
                                    if !(0xDC00..=0xDFFF).contains(&lo) {
                                        return Err("a surrogate pair whose second half is not \
                                                    a low surrogate".into());
                                    }
                                    let c = 0x10000 + ((n - 0xD800) << 10) + (lo - 0xDC00);
                                    char::from_u32(c).ok_or("an impossible \\u escape")?
                                }
                                0xDC00..=0xDFFF => {
                                    return Err("a low surrogate with no high half before it"
                                               .into());
                                }
                                _ => char::from_u32(n).ok_or("an impossible \\u escape")?,
                            }
                        }
                        other => {
                            return Err(format!("unknown escape \\{} in a string",
                                               other as char));
                        }
                    };
                    let mut buf = [0u8; 4];
                    out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                }
                // A raw control character is not allowed in a JSON string, and a literal NUL
                // inside a model name is a sign the answer is not what it claims to be.
                0x00..=0x1F => return Err(format!("a raw control byte {c:#04x} in a string")),
                _ => out.push(c),
            }
        }
        Err("unterminated string".into())
    }

    /// `,` separates, it does not decorate. The previous version treated a comma as "skip me",
    /// which accepted `{,,,"memories":{"total":7 "active":5,},}` -- a missing comma between two
    /// fields silently became one field too.
    fn object(&mut self, depth: usize) -> Result<Json, String> {
        self.i += 1;                              // '{'
        let mut kv: Vec<(String, Json)> = Vec::new();
        self.ws();
        if self.b.get(self.i) == Some(&b'}') { self.i += 1; return Ok(Json::Obj(kv)); }
        loop {
            self.ws();
            let k = self.string()?;
            // Duplicates used to resolve two different ways in the same answer -- first wins in
            // `get`, last wins in `counts`. Neither is a reading of anything.
            if kv.iter().any(|(seen, _)| *seen == k) {
                return Err(format!("the key {k:?} appears twice"));
            }
            self.ws();
            if self.b.get(self.i) != Some(&b':') {
                return Err(format!("expected ':' after {k:?}"));
            }
            self.i += 1;
            let v = self.value(depth + 1)?;
            kv.push((k, v));
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => { self.i += 1; }
                Some(b'}') => { self.i += 1; return Ok(Json::Obj(kv)); }
                _ => return Err(format!("expected ',' or '}}' at byte {}", self.i)),
            }
        }
    }

    fn array(&mut self, depth: usize) -> Result<Json, String> {
        self.i += 1;                              // '['
        let mut out = Vec::new();
        self.ws();
        if self.b.get(self.i) == Some(&b']') { self.i += 1; return Ok(Json::Arr(out)); }
        loop {
            out.push(self.value(depth + 1)?);
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => { self.i += 1; }
                Some(b']') => { self.i += 1; return Ok(Json::Arr(out)); }
                _ => return Err(format!("expected ',' or ']' at byte {}", self.i)),
            }
        }
    }
}

pub(crate) fn parse_json(raw: &str) -> Result<Json, String> {
    let mut sc = Scanner::new(raw);
    let v = sc.value(0)?;
    sc.ws();
    if sc.i < sc.b.len() {
        return Err(format!("trailing data after the JSON value at byte {}", sc.i));
    }
    Ok(v)
}

/// A field `stats.sql` ALWAYS supplies -- including for a completely empty store, which is what
/// the `coalesce(…, '{}')` and `'[]'` in that query are for.
///
/// So a missing or mistyped one does not mean "nothing there", it means the answer did not come
/// from this query, and the console has to say so rather than print the zeroes that absence
/// would otherwise become. An empty store and somebody else's answer are the one pair this
/// console exists to keep apart: `{"memories":{"total":7,"active":5}}` used to exit 0 and report
/// an empty corpus, an empty review queue, no stale memories and no pages.
fn need<'a>(v: &'a Json, key: &str, whose: &str) -> Result<&'a Json, String> {
    v.get(key).ok_or_else(|| format!("{whose}{key:?} is missing -- this is not the stats query"))
}

fn need_i64(v: &Json, key: &str, whose: &str) -> Result<i64, String> {
    need(v, key, whose)?.as_i64()
        .ok_or_else(|| format!("{whose}{key:?} is not a whole number"))
}

fn need_str<'a>(v: &'a Json, key: &str, whose: &str) -> Result<&'a str, String> {
    need(v, key, whose)?.as_str()
        .ok_or_else(|| format!("{whose}{key:?} is not a string"))
}

fn need_arr<'a>(v: &'a Json, key: &str, whose: &str) -> Result<&'a [Json], String> {
    need(v, key, whose)?.as_arr()
        .ok_or_else(|| format!("{whose}{key:?} is not a list"))
}

/// A `{name: count}` map, with every entry required to BE a count. `filter_map` here used to
/// make a malformed entry vanish, which prints as a smaller store rather than as a problem.
fn need_counts(v: &Json, key: &str, whose: &str) -> Result<BTreeMap<String, i64>, String> {
    let mut out = BTreeMap::new();
    match need(v, key, whose)? {
        Json::Obj(kv) => {
            for (k, val) in kv {
                let n = val.as_i64()
                    .ok_or_else(|| format!("{whose}{key:?}: {k:?} is not a whole number"))?;
                out.insert(k.clone(), n);
            }
            Ok(out)
        }
        _ => Err(format!("{whose}{key:?} is not an object")),
    }
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

    let mem = need(&v, "memories", "")?;
    let mut s = Stats {
        memories_total: need_i64(mem, "total", "memories.")?,
        memories_active: need_i64(mem, "active", "memories.")?,
        pages: need_i64(mem, "pages", "memories.")?,
        by_type: need_counts(mem, "by_type", "memories.")?,
        stale: need_i64(&v, "stale", "")?,
        db_size: need_str(&v, "db_size", "")?.to_string(),
        components: need_counts(&v, "components", "")?,
        ..Stats::default()
    };
    let q = need(&v, "review_queue", "")?;
    s.review_pending = need_i64(q, "pending", "review_queue.")?;
    s.review_oldest_days = need_i64(q, "oldest_days", "review_queue.")?;

    for r in need_arr(mem, "by_project", "memories.")? {
        s.by_project.push((need_str(r, "project", "memories.by_project[].")?.to_string(),
                           need_i64(r, "n", "memories.by_project[].")?));
    }
    for r in need_arr(&v, "corpus", "")? {
        s.corpus.push(RepoStats {
            repo: need_str(r, "repo", "corpus[].")?.to_string(),
            docs: need_i64(r, "docs", "corpus[].")?,
            chunks: need_i64(r, "chunks", "corpus[].")?,
            embedded: need_i64(r, "embedded", "corpus[].")?,
        });
    }
    // A list, not a `{model: count}` map, because a chunk with NO recorded model has to stay
    // distinguishable from one whose model is literally named "(null)" -- the query used to
    // coalesce both into the same key, merging their counts. JSON null cannot collide with a
    // string.
    for r in need_arr(&v, "embedding_models", "")? {
        let n = need_i64(r, "n", "embedding_models[].")?;
        let m = need(r, "model", "embedding_models[].")?;
        if m.is_null() {
            s.embedding_unset += n;
        } else {
            let name = m.as_str()
                .ok_or("\"embedding_models[].model\" is neither a string nor null")?;
            s.embedding_models.insert(name.to_string(), n);
        }
    }
    Ok(s)
}

#[cfg(test)]
mod tests {
    use super::*;

    // A shortened snapshot of a real answer from a live store. The parser is checked against the
    // shape stats.sql actually returns, not one invented for the test.
    const SAMPLE: &str = r#"{"memories" : {"total" : 682, "active" : 483, "by_type" : { "semantic" : 90, "preference" : 139 }, "by_project" : [{"project" : "(none)", "n" : 83}, {"project" : "myrepo", "n" : 68}], "pages" : 17}, "review_queue" : {"pending" : 0, "oldest_days" : 0}, "stale" : 0, "corpus" : [{"repo" : "myrepo", "docs" : 438, "chunks" : 7780, "embedded" : 7780}, {"repo" : "myrepo~mem", "docs" : 1, "chunks" : 1, "embedded" : 1}], "embedding_models" : [{"model" : "bge-m3", "n" : 25035}], "components" : { "myrepo" : 20, "infra" : 38 }, "db_size" : "430 MB"}"#;

    /// A complete answer with the three parts a test might want to vary. Every field is
    /// required now, so a test probing ONE of them still has to supply the rest -- which is the
    /// point of the change: an answer missing the others is itself the error.
    fn answer(by_project: &str, corpus: &str, models: &str) -> String {
        format!(r#"{{"memories":{{"total":1,"active":1,"by_type":{{}},"by_project":{by_project},"pages":0}},"review_queue":{{"pending":0,"oldest_days":0}},"stale":0,"corpus":{corpus},"embedding_models":{models},"components":{{}},"db_size":"1 MB"}}"#)
    }

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
        assert_eq!(s.embedding_unset, 0);
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
        let raw = answer("[]", r#"[{"repo":"docs","docs":438,"chunks":7780,"embedded":7780}]"#, "[]");
        let s = parse(&raw).expect("parse");
        assert_eq!(s.corpus.len(), 1);
        assert_eq!(s.corpus[0].repo, "docs");
        assert_eq!(s.corpus[0].docs, 438);
        assert_eq!(s.corpus[0].chunks, 7780);
    }

    /// The old list reader ended the array at the first `]` byte, so a bracket inside a project
    /// name silently dropped every project after it.
    #[test]
    fn a_bracket_inside_a_name_does_not_truncate_the_list() {
        let raw = answer(r#"[{"project":"a[1]","n":5},{"project":"b","n":7}]"#, "[]", "[]");
        let s = parse(&raw).expect("parse");
        assert_eq!(s.by_project.len(), 2);
        assert_eq!(s.by_project[0].0, "a[1]");
        assert_eq!(s.by_project[1].1, 7);
    }

    /// Escapes belong to the string, not to the scanner: a quote or a backslash inside a name must
    /// not end it, and `\u` must decode.
    #[test]
    fn escapes_inside_names_are_decoded_not_obeyed() {
        let raw = answer(r#"[{"project":"a\"b\\cA","n":3}]"#, "[]", "[]");
        let s = parse(&raw).expect("parse");
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
    ///
    /// Exercised through `read_config_file`, which is the path the console actually takes:
    /// checking the guard alone would no longer prove anything, because the guard now judges the
    /// descriptor that gets read rather than a second lookup of the same name.
    #[cfg(unix)]
    #[test]
    fn a_config_anyone_can_write_is_refused() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join("hm-console-cfg-test");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("console.conf");
        std::fs::write(&path, "HM_PSQL_CMD=echo nope\n").unwrap();

        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(read_config_file(&path).expect("a private file must be usable"),
                   Some("HM_PSQL_CMD=echo nope\n".to_string()));

        for bad in [0o666, 0o622, 0o662] {
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(bad)).unwrap();
            let why = read_config_file(&path).expect_err("mode {bad:o} must be refused");
            assert!(why.contains("write"), "the refusal must say why: {why}");
        }

        // A file that is not there is not a fault -- there is a working default. Anything else
        // IS a fault: both used to come back as an empty map, and an empty map means the
        // default command, which is a different database.
        assert_eq!(read_config_file(&dir.join("absent.conf")).expect("absence is not a fault"),
                   None);
        assert!(read_config_file(&dir).is_err(), "a directory is not a config");
        // `/dev/null/not-a-file` was the audit's case: a path UNDER something that is not a
        // directory. It came back as an absence and the default database was chosen silently.
        assert!(read_config_file(&path.join("deeper")).is_err(),
                "a path under a plain file is an error, not an absence");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Saving must not widen the file, and must not leave the previous one destroyed. Verified
    /// against the failure it replaces: writing in place left an existing 0644 file at 0644
    /// while the password was already in it.
    #[cfg(unix)]
    #[test]
    fn saving_leaves_a_private_file_and_no_leftovers() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join("hm-console-save-test");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("console.conf");
        // The wizard writes wherever HM_CONSOLE_CONFIG points, so the test drives the real one.
        std::env::set_var("HM_CONSOLE_CONFIG", &path);

        std::fs::write(&path, "HM_PSQL_CMD=old\n").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();

        let mut cfg = BTreeMap::new();
        cfg.insert("HM_PSQL_CMD".to_string(), "psql \"$DATABASE_URL\" -tAX".to_string());
        save_config(&cfg).expect("save");

        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "a file that held a password must not stay group-readable");
        assert!(std::fs::read_to_string(&path).unwrap().contains("DATABASE_URL"));
        assert!(!dir.join("console.conf.new").exists(), "the temporary file must be gone");

        std::env::remove_var("HM_CONSOLE_CONFIG");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Every field this query always supplies is required. `{"memories":{"total":7,"active":5}}`
    /// used to exit 0 and draw a complete dashboard: empty corpus, empty review queue, no stale
    /// memories, no pages -- which is exactly what a healthy empty store looks like. An answer
    /// that is not this query's must not be able to impersonate one.
    #[test]
    fn a_partial_answer_is_not_an_empty_store() {
        assert!(parse(r#"{"memories":{"total":7,"active":5}}"#).is_err());
        let full = answer("[]", "[]", "[]");
        parse(&full).expect("the complete shape still parses");
        for drop in ["\"stale\":0,", "\"components\":{},", ",\"pages\":0", "\"by_type\":{},",
                     "\"review_queue\":{\"pending\":0,\"oldest_days\":0},"] {
            let damaged = full.replace(drop, "");
            assert_ne!(damaged, full, "the fixture must actually contain {drop}");
            assert!(parse(&damaged).is_err(), "a missing {drop} became a zero");
        }
        // A field of the wrong type is the same lie by another route.
        assert!(parse(&full.replace("\"stale\":0", "\"stale\":\"none\"")).is_err());
        assert!(parse(&full.replace("\"components\":{}", "\"components\":[]")).is_err());
        // ...and so is a row inside a collection that is missing a column: `filter_map` used to
        // make such a row vanish, which prints as a smaller store rather than as a problem.
        assert!(parse(&answer("[]", r#"[{"repo":"x","docs":1,"chunks":2}]"#, "[]")).is_err());
        assert!(parse(&answer(r#"[{"n":5}]"#, "[]", "[]")).is_err());
    }

    /// Counts come from `count(*)` and must arrive unchanged. Every one of these used to pass
    /// through an f64 and a saturating cast, and every one exited 0: `9007199254740993` printed
    /// as `…992`, `7.9` as `7`, `1e999` as `i64::MAX`.
    #[test]
    fn counts_are_not_rounded_through_a_float() {
        let big = answer("[]", "[]", "[]").replace("\"total\":1", "\"total\":9007199254740993");
        assert_eq!(parse(&big).expect("parse").memories_total, 9007199254740993);
        for wrong in ["7.9", "1e999", "-0.5", "1e30"] {
            let raw = answer("[]", "[]", "[]").replace("\"total\":1", &format!("\"total\":{wrong}"));
            assert!(parse(&raw).is_err(), "{wrong} was accepted as a count");
        }
    }

    /// Two names that differ must stay two names. Decoding bad input to U+FFFD as it went was
    /// not leniency but a merge: these two emoji escapes both became `?`, one count overwrote
    /// the other, and the "more than one embedding model" warning went quiet with them.
    #[test]
    fn two_names_that_differ_must_not_decode_to_one() {
        let pair = r#"[{"model":"😀","n":11},{"model":"😁","n":22}]"#;
        let s = parse(&answer("[]", "[]", pair)).expect("a surrogate pair is one character");
        assert_eq!(s.embedding_models.len(), 2, "two models, and the warning depends on it");
        assert_eq!(s.embedding_models.get("\u{1f600}"), Some(&11));
        // Half a pair is not a character. Accepting it is how the two above became one.
        for half in [r#"[{"model":"\ud83d","n":1}]"#, r#"[{"model":"\ude00","n":1}]"#,
                     r#"[{"model":"\ud83dA","n":1}]"#] {
            assert!(parse(&answer("[]", "[]", half)).is_err(), "accepted a lone surrogate: {half}");
        }
    }

    /// The deadline covers the WHOLE operation, descendants included. Each of these was measured
    /// against the previous version, which established its deadline only after the query had
    /// been written and then gave the readers a fresh five seconds on top:
    ///
    /// - a descendant holding stdout: error after 5.03 s, and the group left running;
    /// - a descendant holding only stderr: SUCCESS after 5.03 s, stderr silently empty;
    /// - a query larger than the pipe, to a command that never reads: blocked for ever.
    #[cfg(unix)]
    #[test]
    fn the_deadline_covers_the_whole_operation() {
        let one = Duration::from_secs(1);

        // A child that exits at once but leaves a descendant holding stdout open. The answer
        // must come back inside the deadline, not five seconds after it.
        let began = Instant::now();
        let r = run_with_timeout("sleep 30 & exit 0", "", one);
        assert!(began.elapsed() < one * 3, "waited {:?} on a held pipe", began.elapsed());
        assert!(r.is_err(), "a held-open pipe is not a complete answer: {r:?}");

        // The same, holding only stderr. This one used to SUCCEED -- with stderr empty, which
        // is how a failing command came back as "said nothing at all".
        let began = Instant::now();
        let _ = run_with_timeout("sleep 30 2>/dev/null & echo hi", "", one);
        assert!(began.elapsed() < one * 3, "waited {:?} on a held stderr", began.elapsed());

        // A query far larger than a pipe's capacity, handed to a command that never reads it.
        // The write used to happen before the deadline existed, on the calling thread.
        let began = Instant::now();
        let huge = "x".repeat(4 << 20);
        let r = run_with_timeout("sleep 30", &huge, one);
        assert!(began.elapsed() < one * 3, "waited {:?} on a full pipe", began.elapsed());
        assert!(r.is_err(), "{r:?}");
    }

    /// Output has a ceiling. Reading on until memory runs out is a worse failure than saying so,
    /// and a command producing megabytes is not psql answering this query.
    #[cfg(unix)]
    #[test]
    fn output_larger_than_the_limit_is_refused() {
        let ok = run_with_timeout("printf 'small'", "", Duration::from_secs(20));
        assert_eq!(ok.as_deref(), Ok("small"));
        // `yes` fills the pipe far faster than the limit, so this settles quickly.
        let r = run_with_timeout("yes 0123456789abcdef", "", Duration::from_secs(20))
            .expect_err("an endless stream must be refused");
        assert!(r.contains("MiB") || r.contains("no answer"), "unexpected refusal: {r}");
    }

    /// A command that exits 0 without reading the query answered a different question. It used
    /// to come back as a successful reading of whatever it happened to print -- and the EPIPE
    /// from the unread half used to be reported INSTEAD of a real error, turning psql's "syntax
    /// error at or near" into "writing the query: Broken pipe".
    #[cfg(unix)]
    #[test]
    fn a_command_that_ignores_the_query_is_not_an_answer() {
        let big = "x".repeat(4 << 20);
        let r = run_with_timeout("exit 0", &big, Duration::from_secs(20))
            .expect_err("a command that never read the query has not answered it");
        assert!(r.contains("whole query"), "{r}");

        // ...but a command that FAILS reports its own error, not the broken pipe.
        let r = run_with_timeout("echo 'syntax error at or near' >&2; exit 3", &big,
                                 Duration::from_secs(20))
            .expect_err("a failing command is an error");
        assert!(r.contains("syntax error"), "the real error must survive the EPIPE: {r}");
    }

    /// Bad bytes from the command are a failure, not a name. They used to be replaced with
    /// U+FFFD before anything could look at them, so two different model names arrived at the
    /// parser genuinely identical -- one count overwrote the other and the "more than one
    /// embedding model" warning went quiet.
    #[cfg(unix)]
    #[test]
    fn output_that_is_not_text_is_a_failure_not_a_name() {
        let ok = run_with_timeout("printf 'hello'", "", Duration::from_secs(10));
        assert_eq!(ok.as_deref(), Ok("hello"), "ordinary output still works");
        let bad = run_with_timeout(r"printf 'a\377b'", "", Duration::from_secs(10))
            .expect_err("invalid UTF-8 must not become a name");
        assert!(bad.contains("UTF-8"), "the refusal must say why: {bad}");
    }

    /// A chunk with no recorded model is not a model named "(null)". The query used to coalesce
    /// them into one key, so their counts merged into a single healthy-looking number.
    #[test]
    fn a_chunk_with_no_model_is_not_a_model_named_null() {
        let both = r#"[{"model":null,"n":9},{"model":"(null)","n":4}]"#;
        let s = parse(&answer("[]", "[]", both)).expect("parse");
        assert_eq!(s.embedding_unset, 9);
        assert_eq!(s.embedding_models.get("(null)"), Some(&4));
        assert_eq!(s.embedding_models.len(), 1, "the unnamed bucket is not a model");
    }

    /// Damaged JSON is not a reading. Every one of these used to parse successfully.
    #[test]
    fn damaged_json_is_refused() {
        let full = answer("[]", "[]", "[]");
        for (what, raw) in [
            ("a missing comma between fields", full.replace("\"total\":1,", "\"total\":1 ")),
            ("a leading comma", full.replace("{\"memories\"", "{,\"memories\"")),
            ("a trailing comma", full.replace("\"pages\":0}", "\"pages\":0,}")),
            ("a repeated key", full.replace("\"stale\":0", "\"stale\":0,\"stale\":99")),
            ("a leading plus", full.replace("\"stale\":0", "\"stale\":+01")),
            ("a bare fraction", full.replace("\"stale\":0", "\"stale\":.5")),
            ("an unknown escape", full.replace("\"1 MB\"", "\"1\\qMB\"")),
            ("a raw NUL in a string", full.replace("\"1 MB\"", "\"1\0MB\"")),
            ("a repeated comma in a list", full.replace("\"corpus\":[]", "\"corpus\":[,]")),
        ] {
            assert!(parse(&raw).is_err(), "{what} was accepted");
        }
    }

    /// Deep nesting must be an error, not a crash. Verified before the limit existed: a hundred
    /// thousand nested arrays in a field nobody reads aborted the release binary with a stack
    /// overflow -- the one answer worse than a wrong number.
    #[test]
    fn deep_nesting_is_an_error_not_a_crash() {
        let deep = format!("{}{}", "[".repeat(5000), "]".repeat(5000));
        let raw = answer("[]", "[]", "[]").replace("\"corpus\":[]", &format!("\"corpus\":{deep}"));
        assert!(parse(&raw).is_err(), "deep nesting must be refused");
    }

    #[test]
    fn an_unconfigured_console_still_has_a_command() {
        assert!(!DEFAULT_PSQL_CMD.trim().is_empty());
        assert!(DEFAULT_PSQL_CMD.contains("DATABASE_URL"),
                "the default must rest on the standard variable, not on someone's cluster");
    }
}
