//! First-run setup: from nothing to a menu bar showing real numbers.
//!
//!     hypermnesia-setup            the whole walkthrough
//!     hypermnesia-setup --connect  only the connection step
//!     hypermnesia-setup --show     what is configured now, and where it came from
//!     hypermnesia-setup --test     try the current connection
//!
//! Two rules this file follows, both of them the reason it exists rather than a README section:
//!
//! 1. **It runs the real thing, it does not reimplement it.** Bringing the stack up and ingesting
//!    a repo is what `./hm` does, and this wizard shells to it. A second implementation of a
//!    deployment would drift from the documented one, and the drift would surface as someone
//!    else's broken install.
//! 2. **Nothing outside this repository is written without showing it first.** Steps 5 and 6
//!    touch the MCP client's config and the agent's hook settings. Each prints exactly what it
//!    would add and asks. A setup wizard that silently edits config it did not create is a
//!    wizard people stop trusting the first time they notice.

use std::collections::BTreeMap;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use hypermnesia_console::{config, config_path, jobs, probe, save_config, Target,
                          DEFAULT_PSQL_CMD, PRESETS};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--show") => show(),
        Some("--test") => test_current(),
        Some("--connect") => { connect(); }
        Some("-h") | Some("--help") => print!("{HELP}"),
        Some(other) => {
            eprintln!("hypermnesia-setup: unknown argument: {other}");
            std::process::exit(1);
        }
        None => walkthrough(),
    }
}

// -- the whole walkthrough -------------------------------------------------------------------

fn walkthrough() {
    println!("HyperMnesia setup.\n");
    println!("This takes you from nothing to a menu-bar icon showing your own numbers.");
    println!("Every step says what it will do first, and nothing outside this repository is");
    println!("written without showing you the exact text.\n");

    step(1, "What is already here");
    let have = survey();
    if !have.psql {
        // Everything downstream shells psql; saying so now beats failing at step 3.
        println!("\n! psql is not on PATH. Every step below needs it (the console, the ingester");
        println!("  and `hm` all shell to it). Install the Postgres client and run this again.");
        if !yes("Continue anyway?", false) { return; }
    }

    step(2, "The store");
    let connected = if yes("Do you already have a HyperMnesia database running?", false) {
        connect()
    } else if have.docker && hm_path().is_some() {
        if yes("Bring one up here with docker compose? (runs ./hm init)", true) {
            if run_hm(&["init"]) { connect() } else { false }
        } else {
            connect()
        }
    } else {
        println!("No docker or no ./hm next to this binary, so nothing to bring up from here.");
        println!("See docs/INSTALL.md for the manual and Kubernetes paths, then come back.");
        connect()
    };
    if !connected {
        println!("\nStopping here: without a reachable store the remaining steps have nothing");
        println!("to act on. Run `hypermnesia-setup --connect` when it is up.");
        return;
    }

    step(3, "A repository to remember");
    if hm_path().is_some() && yes("Ingest a repository now? (runs ./hm ingest)", true) {
        let dir = ask("Path to the repository", "");
        let scope = ask("Scope name to store it under", "myrepo");
        if !dir.trim().is_empty() {
            run_hm(&["ingest", dir.trim(), scope.trim()]);
        }
    } else {
        println!("Skipped. `./hm ingest <dir> <scope>` does it later.");
    }

    step(4, "Your MCP client");
    offer_mcp_block();

    step(5, "The hooks");
    offer_hooks_block();

    step(6, "The menu bar");
    if yes("Add the tray to autostart?", true) {
        match jobs::install_self(jobs::DEFAULT_TRAY_LABEL) {
            Ok(msg) => println!("{msg}"),
            Err(e) => println!("! could not install it: {e}"),
        }
    }

    step(7, "Check, rather than assume");
    if hm_path().is_some() {
        run_hm(&["doctor"]);
    }
    println!("\nDone. `hypermnesia-stats` prints the same numbers the tray shows.");
    println!("If something above was skipped, each step is its own command -- see --help.");
}

fn step(n: u8, title: &str) {
    println!("\n── {n}. {title} ───────────────────────────────────────");
}

struct Survey {
    psql: bool,
    docker: bool,
}

fn survey() -> Survey {
    let s = Survey { psql: which("psql"), docker: which("docker") };
    println!("  psql    {}", mark(s.psql));
    println!("  docker  {}", mark(s.docker));
    match hm_path() {
        Some(p) => println!("  ./hm    found at {}", p.display()),
        None => println!("  ./hm    not found next to this binary (deploy steps unavailable)"),
    }
    s
}

fn mark(b: bool) -> &'static str { if b { "found" } else { "NOT found" } }

fn which(bin: &str) -> bool {
    Command::new("sh").arg("-c").arg(format!("command -v {bin} >/dev/null 2>&1"))
        .status().map(|s| s.success()).unwrap_or(false)
}

/// `hm` lives at the repository root, and this binary at console/target/release/. Resolved from
/// the executable rather than the working directory: the wizard is run from anywhere.
fn hm_path() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    for up in [3usize, 4] {
        if let Some(root) = exe.ancestors().nth(up) {
            let p = root.join("hm");
            if p.exists() { return Some(p); }
        }
    }
    let p = Path::new("hm").to_path_buf();
    if p.exists() { Some(p) } else { None }
}

/// Run `./hm <args>` with its output going straight to the terminal, and say plainly whether it
/// worked. Inheriting stdio rather than capturing: these steps take minutes and a silent wizard
/// looks hung.
fn run_hm(args: &[&str]) -> bool {
    let Some(hm) = hm_path() else {
        println!("! ./hm not found, skipping");
        return false;
    };
    println!("Running: {} {}\n", hm.display(), args.join(" "));
    let status = Command::new("sh")
        .arg(hm).args(args)
        .stdin(Stdio::inherit()).stdout(Stdio::inherit()).stderr(Stdio::inherit())
        .status();
    match status {
        Ok(s) if s.success() => true,
        Ok(s) => { println!("\n! it exited with {s}"); false }
        Err(e) => { println!("\n! could not run it: {e}"); false }
    }
}

// -- the connection --------------------------------------------------------------------------

fn connect() -> bool {
    println!("\nDeployments differ by exactly one thing: the command that runs psql.");
    println!("Pick the shape closest to yours:\n");
    for (i, (name, _)) in PRESETS.iter().enumerate() {
        println!("  {}) {}", i + 1, describe(name));
    }
    println!("  {}) type the whole command yourself", PRESETS.len() + 1);

    let idx: usize = ask("\nNumber", "1").trim().parse().unwrap_or(1);
    let cmd = if idx == PRESETS.len() + 1 {
        ask("Command (receives SQL on stdin)", DEFAULT_PSQL_CMD)
    } else {
        fill(PRESETS.get(idx - 1).unwrap_or(&PRESETS[0]).1)
    };

    println!("\nThat gives:\n  {cmd}\n");
    // Tried BEFORE it is written. Writing an untried setting hands someone a console that shows
    // an empty screen on first open -- and an empty screen is indistinguishable from an empty
    // store.
    let t = Target { psql_cmd: cmd.clone(), ..Target::default() };
    let ok = match probe(&t) {
        Ok(info) => {
            println!("The database answered: {}", info.lines().next().unwrap_or(&info));
            true
        }
        Err(e) => {
            println!("! No answer: {e}");
            if !yes("Save it anyway?", false) {
                println!("Nothing was written.");
                return false;
            }
            false
        }
    };

    let mut cfg: BTreeMap<String, String> = config();
    cfg.insert("HM_PSQL_CMD".into(), cmd);
    match save_config(&cfg) {
        Ok(()) => println!("Written to {}", config_path().display()),
        Err(e) => {
            println!("! Not written: {e}");
            return false;
        }
    }
    ok
}

fn describe(name: &str) -> &'static str {
    match name {
        "direct" => "psql straight to DATABASE_URL (the database is reachable from here)",
        "docker" => "docker exec into a Postgres container on this machine",
        "kubectl" => "kubectl exec into a pod (kubectl is configured here)",
        "ssh+kubectl" => "ssh to a machine that has kubectl, then into the pod",
        _ => "your own shape",
    }
}

/// Fill a template's `{...}` slots from answers. Only the slots that appear are asked about.
fn fill(template: &str) -> String {
    let mut out = template.to_string();
    for (slot, prompt, default) in [
        ("{host}", "ssh host", ""),
        ("{namespace}", "namespace", "hypermnesia"),
        ("{deploy}", "deployment", "postgres"),
        ("{container}", "container name", "hypermnesia-pg"),
        ("{user}", "Postgres user", "hm"),
        ("{db}", "database", "hypermnesia"),
    ] {
        if out.contains(slot) {
            out = out.replace(slot, ask(prompt, default).trim());
        }
    }
    out
}

// -- the two blocks that touch someone else's files ------------------------------------------

fn offer_mcp_block() {
    let root = hm_path().and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("/path/to/hypermnesia"));
    let block = format!(r#"{{ "mcpServers": {{ "hypermnesia": {{
  "command": "{root}/mcp-server/target/release/hypermnesia-mcp",
  "env": {{
    "HM_REPO": "myrepo",
    "DATABASE_URL": "postgresql://...",
    "HM_SEARCH":  "{root}/ingest/search.py",
    "HM_MEM_OPS": "{root}/ingest/mem_ops.py"
  }} }} }} }}"#, root = root.display());
    println!("Add this to your MCP client's config (for Claude Code, the repo's .mcp.json):\n");
    println!("{block}\n");
    println!("HM_REPO must be the scope you ingested under -- it is an exact, case-sensitive");
    println!("match, and a name one character off resolves to nothing on every edit.");
    let target = ask("Write it to which file? (empty to skip)", "");
    let target = target.trim();
    if target.is_empty() {
        println!("Skipped -- copy it yourself when you are ready.");
        return;
    }
    if Path::new(target).exists() {
        // Merging JSON properly needs a JSON library and an opinion about conflicts. Refusing is
        // honest; overwriting someone's client config would not be.
        println!("! {target} already exists. This wizard will not merge into it -- add the block");
        println!("  above by hand, so nothing of yours is lost.");
        return;
    }
    match std::fs::write(target, format!("{block}\n")) {
        Ok(()) => println!("Written to {target}"),
        Err(e) => println!("! Not written: {e}"),
    }
}

fn offer_hooks_block() {
    let root = hm_path().and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("/path/to/hypermnesia"));
    println!("The hooks are what makes this more than a search index: they deliver without being");
    println!("asked. For Claude Code, add to its settings:\n");
    println!(r#"  "hooks": {{
    "PreToolUse": [{{ "matcher": "Edit|Write|MultiEdit", "hooks": [
      {{ "type": "command", "command": "python3 {root}/hooks/arch_invariants.py" }} ]}}],
    "UserPromptSubmit": [{{ "hooks": [
      {{ "type": "command", "command": "python3 {root}/hooks/mem_recall.py" }} ]}}],
    "SessionStart": [{{ "hooks": [
      {{ "type": "command", "command": "python3 {root}/hooks/mem_profile.py" }} ]}}]
  }}"#, root = root.display());
    println!("\nSee hooks/README.md for the rest (capture, extract, consolidate, reflect) and for");
    println!("the scheduled passes. This wizard does not edit your agent's settings file: it is");
    println!("yours, it has other things in it, and a silent merge is how people lose config.");
}

// -- the smaller commands ---------------------------------------------------------------------

fn show() {
    let cfg = config();
    println!("Config file: {}{}", config_path().display(),
             if config_path().exists() { "" } else { "  (not created yet)" });
    let from_env = std::env::var("HM_PSQL_CMD").ok().filter(|s| !s.is_empty());
    match (&from_env, cfg.get("HM_PSQL_CMD")) {
        (Some(v), _) => {
            println!("Command (from the environment, HM_PSQL_CMD):\n  {v}");
            if cfg.contains_key("HM_PSQL_CMD") {
                // Otherwise what is written in the file looks like what is in effect.
                println!("! the file holds a different one, but the environment wins");
            }
        }
        (None, Some(v)) => println!("Command (from the file):\n  {v}"),
        (None, None) => println!("Command (default):\n  {DEFAULT_PSQL_CMD}"),
    }
}

fn test_current() {
    let t = Target::default();
    println!("Trying: {}", t.psql_cmd);
    match probe(&t) {
        Ok(info) => println!("Answers: {}", info.lines().next().unwrap_or(&info)),
        Err(e) => {
            eprintln!("No answer: {e}");
            std::process::exit(1);
        }
    }
}

// -- asking ------------------------------------------------------------------------------------

fn ask(prompt: &str, default: &str) -> String {
    if default.is_empty() {
        print!("{prompt}: ");
    } else {
        print!("{prompt} [{default}]: ");
    }
    let _ = io::stdout().flush();
    let mut line = String::new();
    if io::stdin().read_line(&mut line).is_err() {
        return default.to_string();
    }
    let line = line.trim();
    if line.is_empty() { default.to_string() } else { line.to_string() }
}

fn yes(prompt: &str, default: bool) -> bool {
    let d = if default { "Y/n" } else { "y/N" };
    let a = ask(&format!("{prompt} [{d}]"), "").to_lowercase();
    match a.chars().next() {
        Some('y') => true,
        Some('n') => false,
        _ => default,
    }
}

const HELP: &str = "\
hypermnesia-setup — from nothing to a menu bar showing your own numbers.

    hypermnesia-setup            the whole walkthrough
    hypermnesia-setup --connect  only the connection step
    hypermnesia-setup --show     what is configured now, and where it came from
    hypermnesia-setup --test     try the current connection

The walkthrough shells to ./hm for the steps that deploy and ingest, rather than reimplementing
them: a second implementation of a deployment drifts from the documented one, and the drift
surfaces as someone else's broken install.

It never writes outside this repository without printing the exact text first, and it refuses to
merge into a config file you already have.

Connection settings go to ~/.config/hypermnesia/console.conf (mode 600 -- the string can carry a
password). HM_PSQL_CMD in the environment beats the file.
";
