//! A scripted stdio session against the real binary: a slow `memory_ask` must not hold up the
//! requests behind it, every response must be one well-formed JSON line carrying the right id,
//! and `notifications/cancelled` must kill the child's whole process group.
//!
//! No database and no Python: HM_PYTHON is `/bin/sh`, the dialectic and mem_ops "scripts" are
//! shell scripts, and `psql` is a stub on PATH that prints a fixed graph.
#![cfg(target_os = "linux")]

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::mpsc::{channel, Receiver};
use std::time::{Duration, Instant};

const GRAPH: &str = r#"{"components":[{"slug":"api","name":"API","repo":"myapp","parent":null,"responsibility":"http","key_paths":["src/api/**"],"priority":0}],"relationships":[],"constraints":[{"kind":"invariant","scope":"global","repo":"myapp","component":null,"component_repo":null,"title":"Global rule","statement":"Always.","severity":"must","source":null}]}"#;

struct Session {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: Option<ChildStdout>,
    rx: Option<Receiver<String>>,
    dir: std::path::PathBuf,
}

/// How a session is started: extra env, env to drop, and the server's working directory
/// (relative to the session dir) when it should not be the session dir itself.
#[derive(Default)]
struct Opts<'a> {
    env: &'a [(&'a str, &'a str)],
    unset: &'a [&'a str],
    cwd: Option<&'a str>,
    /// A directory (relative to the session dir) to `git init` before the server starts.
    git_repo: Option<&'a str>,
}

impl Session {
    fn start(tag: &str) -> Session { Session::start_opts(tag, Opts::default()) }

    fn start_with(tag: &str, extra_env: &[(&str, &str)]) -> Session {
        Session::start_opts(tag, Opts { env: extra_env, ..Opts::default() })
    }

    fn start_opts(tag: &str, opts: Opts) -> Session {
        let dir = std::env::temp_dir().join(format!("hm-stdio-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let write_exec = |name: &str, body: &str| {
            use std::os::unix::fs::PermissionsExt;
            let p = dir.join(name);
            std::fs::write(&p, body).unwrap();
            std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
            p
        };
        write_exec("psql", &format!("#!/bin/sh\ncat >/dev/null\ncat <<'EOF'\n{GRAPH}\nEOF\n"));
        // The dialectic: "slow" sleeps in a grandchild (so a kill of the leader alone would
        // leave it running) and records both pids; "medium" answers after 2 s; anything else
        // answers at once.
        let dialectic = write_exec("dialectic.sh", &format!(r#"payload=$(cat)
case "$payload" in
  *slow*) sleep 60 & echo "$$ $!" > {dir}/pids; wait; echo '{{"never":true}}' ;;
  *stubborn*) (trap '' ALRM; exec sleep 60) & echo "$$ $!" > {dir}/pids; wait; echo '{{"never":true}}' ;;
  *medium*) sleep 2; echo '{{"version":3,"answer":"medium"}}' ;;
  *) echo '{{"version":3,"answer":"fast"}}' ;;
esac
"#, dir = dir.display()));
        // "big" answers with a 100 KB line -- more than a pipe buffer -- and every call is
        // counted, so a test can see how many workers really started. "escape" leaves a
        // descendant in a session of its own (out of reach of the group kill) that still holds
        // stdout and stderr, and records its pid so the test can clean it up. It answers only
        // once the descendant has written its pid from inside the new session: answering
        // earlier races the group kill against setsid().
        let mem_ops = write_exec("mem_ops.sh", &format!(r#"payload=$(cat); echo x >> {dir}/memops.calls
case "$payload" in
  *big*) head -c 100000 /dev/zero | tr '\0' 'a'; echo ;;
  *escape*) f={dir}/esc.$$; setsid sh -c "echo \$\$ > $f; exec sleep 60" &
            while [ ! -s $f ]; do sleep 0.05; done; cat $f >> {dir}/escaped; echo "op $1" ;;
  *) echo "op $1" ;;
esac
"#, dir = dir.display()));
        let path = format!("{}:{}", dir.display(), std::env::var("PATH").unwrap_or_default());
        if let Some(r) = opts.git_repo {
            std::fs::create_dir_all(dir.join(r)).unwrap();
            assert!(Command::new("git").args(["init", "-q"]).arg(dir.join(r)).status().unwrap().success());
        }
        let cwd = opts.cwd.map_or(dir.clone(), |c| dir.join(c));
        std::fs::create_dir_all(&cwd).unwrap();
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_hypermnesia-mcp"));
        cmd.current_dir(&cwd)
            .env("PATH", path)
            .env("HM_PYTHON", "/bin/sh")
            .env("HM_DIALECTIC", &dialectic)
            .env("HM_MEM_OPS", &mem_ops)
            .env("HM_REPO", "myapp")
            .env("HM_ROOT", "/w/myapp")
            .env("HYPERMNESIA_ENV_FILE", dir.join("no-such.env"))
            .env_remove("MEM_AUTHOR")
            .envs(opts.env.iter().copied())
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
        for k in opts.unset { cmd.env_remove(k); }
        let mut child = cmd.spawn().expect("spawn server");
        let stdout = child.stdout.take();
        let stdin = child.stdin.take();
        Session { child, stdin, stdout, rx: None, dir }
    }

    /// Start draining the server's stdout. Until this is called nothing reads it, which is
    /// how a test plays a client that is not reading.
    fn lines(&mut self) -> &Receiver<String> {
        if self.rx.is_none() {
            let stdout = self.stdout.take().unwrap();
            let (tx, rx) = channel();
            std::thread::spawn(move || {
                for line in BufReader::new(stdout).lines() {
                    let Ok(line) = line else { break };
                    if tx.send(line).is_err() { break; }
                }
            });
            self.rx = Some(rx);
        }
        self.rx.as_ref().unwrap()
    }

    fn calls(&self) -> usize {
        std::fs::read_to_string(self.dir.join("memops.calls")).map_or(0, |s| s.lines().count())
    }

    fn send(&mut self, msg: Value) {
        let s = self.stdin.as_mut().unwrap();
        s.write_all(format!("{msg}\n").as_bytes()).unwrap();
        s.flush().unwrap();
    }

    /// Next response line, parsed. Panics on a line that is not one JSON-RPC object.
    fn next(&mut self, within: Duration) -> Option<Value> {
        let line = self.lines().recv_timeout(within).ok()?;
        let v: Value = serde_json::from_str(&line)
            .unwrap_or_else(|e| panic!("response is not one JSON line ({e}): {line:?}"));
        assert_eq!(v["jsonrpc"], "2.0", "{line}");
        assert!(v.get("id").is_some(), "response without an id: {line}");
        Some(v)
    }

    fn call(id: i64, name: &str, args: Value) -> Value {
        json!({"jsonrpc":"2.0","id":id,"method":"tools/call","params":{"name":name,"arguments":args}})
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Gone, or a zombie nobody has reaped yet -- either way, no longer running.
fn dead(pid: i32) -> bool {
    match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Err(_) => true,
        Ok(stat) => stat.rsplit(')').next().map_or(true, |rest| rest.trim_start().starts_with('Z')),
    }
}

fn wait_for_pids(dir: &std::path::Path) -> Vec<i32> {
    let until = Instant::now() + Duration::from_secs(10);
    loop {
        if let Ok(s) = std::fs::read_to_string(dir.join("pids")) {
            let v: Vec<i32> = s.split_whitespace().filter_map(|x| x.parse().ok()).collect();
            if v.len() == 2 { return v; }
        }
        assert!(Instant::now() < until, "the slow dialectic never started");
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn slow_call_does_not_block_and_cancel_kills_the_group() {
    let mut s = Session::start("cancel");
    s.send(json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}));
    let init = s.next(Duration::from_secs(10)).expect("initialize answered");
    assert_eq!(init["id"], 1);

    s.send(Session::call(2, "memory_ask", json!({"query":"slow question"})));
    let pids = wait_for_pids(&s.dir);

    // While id 2 is thinking, everything else is answered at once.
    let started = Instant::now();
    s.send(json!({"jsonrpc":"2.0","id":3,"method":"tools/list"}));
    s.send(Session::call(4, "get_constraints", json!({"paths":["/w/myapp/src/api/x.py"]})));
    s.send(Session::call(5, "memory_search", json!({"query":"anything"})));
    s.send(json!({"jsonrpc":"2.0","id":6,"method":"ping"}));
    let mut seen = std::collections::HashMap::new();
    while seen.len() < 4 {
        let v = s.next(Duration::from_secs(10)).expect("a response behind the slow call");
        let id = v["id"].as_i64().unwrap();
        assert_ne!(id, 2, "the slow call must not have answered yet: {v}");
        seen.insert(id, v);
    }
    assert!(started.elapsed() < Duration::from_secs(8),
            "requests behind a slow memory_ask waited {:?}", started.elapsed());
    assert!(seen[&3]["result"]["tools"].as_array().is_some_and(|t| !t.is_empty()));
    let gc = seen[&4]["result"]["content"][0]["text"].as_str().unwrap();
    assert!(gc.contains("src/api/x.py -> api") && gc.contains("Global rule"), "{gc}");
    assert_eq!(seen[&4]["result"]["isError"], false);
    assert!(seen[&5]["result"]["content"][0]["text"].as_str().unwrap().contains("op search"));
    assert_eq!(seen[&6]["result"], json!({}));
    assert!(pids.iter().all(|p| !dead(*p)), "the slow call should still be running");

    // Cancel it: the interpreter AND its grandchild go, and no response is sent for id 2.
    s.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled",
                  "params":{"requestId":2,"reason":"client timeout"}}));
    let until = Instant::now() + Duration::from_secs(8);
    while !pids.iter().all(|p| dead(*p)) {
        assert!(Instant::now() < until, "cancelled child group still alive: {pids:?}");
        std::thread::sleep(Duration::from_millis(50));
    }
    // The server is still serving, and stays silent about the cancelled id.
    s.send(json!({"jsonrpc":"2.0","id":7,"method":"ping"}));
    let v = s.next(Duration::from_secs(5)).expect("ping after cancel");
    assert_eq!(v["id"], 7, "expected only the ping, got {v}");
    assert!(s.next(Duration::from_millis(1500)).is_none(), "a response arrived for the cancelled call");
}

#[test]
fn concurrent_answers_are_whole_lines_with_their_own_ids() {
    let mut s = Session::start("lines");
    // Four 2-second calls at once must finish in about 2 s, not 8, and each answer is its own
    // line carrying its own id.
    let started = Instant::now();
    for id in 10..14 {
        s.send(Session::call(id, "memory_ask", json!({"query":"medium question"})));
    }
    s.send(Session::call(14, "memory_ask", json!({"query":"fast question"})));
    let mut got = std::collections::HashMap::new();
    while got.len() < 5 {
        let v = s.next(Duration::from_secs(15)).expect("all five answered");
        got.insert(v["id"].as_i64().unwrap(), v);
    }
    assert!(started.elapsed() < Duration::from_secs(6),
            "calls ran one after another: {:?}", started.elapsed());
    for id in 10..14 {
        assert_eq!(got[&id]["result"]["structuredContent"]["answer"], "medium", "id {id}");
    }
    assert_eq!(got[&14]["result"]["structuredContent"]["answer"], "fast");
}

#[test]
fn closing_stdin_still_answers_what_is_in_flight() {
    // The one-shot form docs/DEMO.md uses: one request, then EOF. The answer must still come.
    let mut s = Session::start("eof-answer");
    s.send(Session::call(1, "memory_ask", json!({"query":"medium question"})));
    drop(s.stdin.take());
    let v = s.next(Duration::from_secs(15)).expect("in-flight call answered after EOF");
    assert_eq!(v["id"], 1);
    assert_eq!(v["result"]["structuredContent"]["answer"], "medium");
    assert!(s.child.wait().unwrap().success());
}

#[test]
fn closing_stdin_stops_children_past_the_grace() {
    let mut s = Session::start_with("eof-stop", &[("HM_SHUTDOWN_GRACE_SECS", "1")]);
    s.send(Session::call(1, "memory_ask", json!({"query":"slow question"})));
    let pids = wait_for_pids(&s.dir);
    drop(s.stdin.take());
    let until = Instant::now() + Duration::from_secs(10);
    while !pids.iter().all(|p| dead(*p)) {
        assert!(Instant::now() < until, "children outlived the client: {pids:?}");
        std::thread::sleep(Duration::from_millis(50));
    }
    let st = s.child.wait().unwrap();
    assert!(st.success(), "server exit: {st:?}");
}

/// A client that stops reading must not get more workers than HM_MAX_INFLIGHT: a call stays in
/// flight until its answer is written, so the second call is refused (the refusal is queued for
/// the writer thread) instead of piling up workers blocked on the same pipe.
#[test]
fn a_non_reading_client_cannot_exceed_the_inflight_cap() {
    let mut s = Session::start_with("cap", &[("HM_MAX_INFLIGHT", "1")]);
    // Spaced out, so each later call arrives while the first answer is already stuck in a
    // full pipe -- the moment a worker that had "finished" used to leave the in-flight count.
    for id in 1..=5 {
        s.send(Session::call(id, "memory_search", json!({"query":"big"})));
        std::thread::sleep(Duration::from_millis(300));
    }
    std::thread::sleep(Duration::from_secs(2));
    assert_eq!(s.calls(), 1, "workers started while nobody read stdout");
    let mut got = 0;
    while got < 5 {
        let v = s.next(Duration::from_secs(15)).expect("every call answered once reading resumes");
        if v["result"]["content"][0]["text"].as_str().is_some_and(|t| t.len() > 100_000) { got += 1; continue; }
        assert!(v["error"]["message"].as_str().is_some_and(|m| m.contains("in flight")), "{v}");
        got += 1;
    }
}

/// EOF right after a request whose answer is larger than a pipe buffer: the answer must
/// arrive whole and newline-terminated, even when the client starts reading late.
#[test]
fn eof_never_cuts_a_large_answer_short() {
    let mut s = Session::start("eof-big");
    s.send(Session::call(9, "memory_search", json!({"query":"big"})));
    drop(s.stdin.take());
    std::thread::sleep(Duration::from_millis(1500));
    let v = s.next(Duration::from_secs(15)).expect("the large answer");
    assert_eq!(v["id"], 9);
    assert!(v["result"]["content"][0]["text"].as_str().unwrap().len() > 100_000);
    assert!(s.child.wait().unwrap().success());
}

/// The leader dies on SIGALRM, its child ignores it. Reaping the leader used to clear the
/// group from the job, so nothing could reach the survivor; now the group is SIGKILLed then.
#[test]
fn cancel_reaches_descendants_that_ignore_sigalrm() {
    let mut s = Session::start("stubborn");
    s.send(Session::call(1, "memory_ask", json!({"query":"stubborn question"})));
    let pids = wait_for_pids(&s.dir);
    s.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}));
    let until = Instant::now() + Duration::from_secs(8);
    while !pids.iter().all(|p| dead(*p)) {
        assert!(Instant::now() < until, "a descendant survived cancellation: {pids:?}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// HM_REPO and HM_ROOT unset, server started in a subdirectory of a git repo: the scope is the
/// repo root's folder name (as the hook derives it), not the subdirectory's.
#[test]
fn default_scope_comes_from_the_repo_root() {
    let mut s = Session::start_opts("scope", Opts { unset: &["HM_REPO", "HM_ROOT"],
        git_repo: Some("myapp"), cwd: Some("myapp/src"), ..Opts::default() });
    s.send(Session::call(1, "get_constraints", json!({"paths":["src/api/x.py"]})));
    let v = s.next(Duration::from_secs(10)).expect("answer");
    let text = v["result"]["content"][0]["text"].as_str().unwrap().to_string();
    assert!(text.contains("src/api/x.py -> api"), "{text}");
    assert!(text.contains("guessed from the repo root's folder name (\"myapp\")"), "{text}");
    assert!(!text.contains("nothing is mapped"), "{text}");
}

/// Threads of the server process, from /proc.
fn threads(pid: u32) -> usize {
    std::fs::read_dir(format!("/proc/{pid}/task")).map_or(0, |d| d.count())
}

/// A worker blocked writing a large answer to a client that has stopped reading holds the stdout
/// lock. The input thread used to write its own replies -- here the HM_MAX_INFLIGHT refusal --
/// under that same lock, so it blocked too, and never got to the cancellation or the EOF behind
/// them: the cancelled call ran on, and the server never exited.
#[test]
fn a_stuck_stdout_does_not_block_the_input_thread() {
    let mut s = Session::start_with("stuck-out",
        &[("HM_MAX_INFLIGHT", "2"), ("HM_SHUTDOWN_GRACE_SECS", "1")]);
    s.send(Session::call(1, "memory_search", json!({"query":"big"})));
    let until = Instant::now() + Duration::from_secs(10);
    while s.calls() < 1 {
        assert!(Instant::now() < until, "the big call never started");
        std::thread::sleep(Duration::from_millis(20));
    }
    // Let the 100 KB answer fill the pipe; nobody reads it.
    std::thread::sleep(Duration::from_millis(500));
    s.send(Session::call(2, "memory_ask", json!({"query":"slow question"})));
    let pids = wait_for_pids(&s.dir);
    s.send(Session::call(3, "memory_search", json!({"query":"anything"})));   // over the cap
    s.send(json!({"jsonrpc":"2.0","id":4,"method":"ping"}));
    s.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":2}}));
    let until = Instant::now() + Duration::from_secs(8);
    while !pids.iter().all(|p| dead(*p)) {
        assert!(Instant::now() < until,
                "the cancellation never reached the call behind a stuck stdout: {pids:?}");
        std::thread::sleep(Duration::from_millis(50));
    }
    drop(s.stdin.take());
    // 1 s grace + CANCEL_GRACE + 1 s, then exit -- with the client still not reading.
    let until = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(st) = s.child.try_wait().unwrap() {
            assert!(st.success(), "server exit: {st:?}");
            break;
        }
        assert!(Instant::now() < until, "the server never exited after EOF behind a stuck stdout");
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// A helper that leaves a descendant in a session of its own, holding stdout and stderr. The
/// group kill cannot reach it, so the pipes never close. The two reader threads per child used
/// to stay blocked on them after the call had answered and released its HM_MAX_INFLIGHT slot:
/// two threads more per call, for as long as the descendant lived.
#[test]
fn an_escaped_pipe_holder_does_not_leak_threads() {
    struct Reap(std::path::PathBuf);
    impl Drop for Reap {
        fn drop(&mut self) {
            for p in std::fs::read_to_string(&self.0).unwrap_or_default().split_whitespace() {
                let _ = Command::new("kill").arg("-9").arg(p).status();
            }
        }
    }
    let mut s = Session::start("escape");
    let _reap = Reap(s.dir.join("escaped"));
    let server = s.child.id();
    // Warm up: one ordinary call and a ping, so every thread the server keeps exists.
    s.send(Session::call(1, "memory_search", json!({"query":"plain"})));
    s.send(json!({"jsonrpc":"2.0","id":2,"method":"ping"}));
    for _ in 0..2 { s.next(Duration::from_secs(10)).expect("warm-up answered"); }
    std::thread::sleep(Duration::from_millis(500));
    let baseline = threads(server);
    assert!(baseline > 0, "cannot read /proc/{server}/task");

    for id in 10..14 {
        s.send(Session::call(id, "memory_search", json!({"query":"escape"})));
    }
    for _ in 10..14 {
        let v = s.next(Duration::from_secs(20)).expect("each escape call answered");
        let text = v["result"]["content"][0]["text"].as_str().unwrap_or("");
        assert!(v["result"]["isError"] == true && text.contains("never arrived"),
                "the held pipe must be reported, not read as output: {v}");
    }
    let escaped = std::fs::read_to_string(s.dir.join("escaped")).unwrap_or_default();
    assert_eq!(escaped.split_whitespace().count(), 4, "{escaped:?}");
    assert!(escaped.split_whitespace().all(|p| !dead(p.parse().unwrap())),
            "the escaped descendants should still be alive, holding the pipes");
    let until = Instant::now() + Duration::from_secs(3);
    loop {
        let now = threads(server);
        if now <= baseline { break; }
        assert!(Instant::now() < until,
                "threads grew from {baseline} to {now} across four calls whose pipes are still held");
        std::thread::sleep(Duration::from_millis(50));
    }
    // Still serving.
    s.send(json!({"jsonrpc":"2.0","id":20,"method":"ping"}));
    assert_eq!(s.next(Duration::from_secs(5)).expect("ping")["id"], 20);
}
