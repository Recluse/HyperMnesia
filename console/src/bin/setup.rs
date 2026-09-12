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
    // `hm ingest` requires DATABASE_URL in its own environment -- it connects directly, not
    // through the command configured in step 2. Shelling into a certain failure and then
    // narrating success for four more steps is worse than saying this now.
    let mut ingested: Option<String> = None;
    let have_url = std::env::var("DATABASE_URL").map(|v| !v.is_empty()).unwrap_or(false);
    if hm_path().is_none() {
        println!("Skipped: ./hm is not next to this binary. `./hm ingest <dir> <scope>` from a");
        println!("checkout does it later.");
    } else if !have_url {
        println!("Skipped: DATABASE_URL is not set in this shell, and `hm ingest` connects with");
        println!("it directly -- the command from step 2 is the console's, not the ingester's.");
        println!("Export it (./hm init prints the line) and run: ./hm ingest <dir> <scope>");
    } else if yes("Ingest a repository now? (runs ./hm ingest)", true) {
        let dir = ask("Path to the repository", "");
        let scope = ask("Scope name to store it under", "myrepo");
        let (dir, scope) = (dir.trim().to_string(), scope.trim().to_string());
        if dir.is_empty() {
            println!("Skipped: no path given.");
        } else if run_hm(&["ingest", &dir, &scope]) {
            ingested = Some(scope);
        } else {
            println!("! The repository was NOT ingested. The steps below still apply, but there");
            println!("  is nothing to find until `./hm ingest {dir} {scope}` succeeds.");
        }
    } else {
        println!("Skipped. `./hm ingest <dir> <scope>` does it later.");
    }

    step(4, "Your MCP client");
    offer_mcp_block(ingested.as_deref());

    step(5, "The hooks");
    offer_hooks_block();

    step(6, "The menu bar");
    if yes("Add the tray to autostart?", true) {
        // launchd starts the tray with a minimal environment: no login shell, none of your
        // exports. A command that reads $DATABASE_URL was verified HERE, where you have it.
        let cmd = config().get("HM_PSQL_CMD").cloned().unwrap_or_default();
        if let Some(var) = shell_variable_in(&cmd) {
            println!("! The command you configured uses ${var}, which this shell supplies and");
            println!("  launchd does not: it starts jobs with a minimal environment. In the tray");
            println!("  that command will fail where it works here. Either write the value into");
            println!("  the command (hypermnesia-setup --connect) or keep using the CLI tools.");
            if !yes("Install it anyway?", false) {
                println!("Not installed.");
                return finish(false);
            }
        }
        match jobs::install_self(jobs::DEFAULT_TRAY_LABEL) {
            Ok(msg) => println!("{msg}"),
            Err(e) => println!("! could not install it: {e}"),
        }
    }

    step(7, "Check, rather than assume");
    // `hm doctor` connects with DATABASE_URL, not with the command from step 2. Saying which
    // store it is about to look at is the difference between a check and a decoration.
    let checked = if hm_path().is_none() {
        println!("Skipped: ./hm is not next to this binary, so there is nothing to run the");
        println!("checks with. `hypermnesia-stats` is the next best look.");
        None
    } else if !have_url {
        println!("Skipped: doctor reads DATABASE_URL directly and it is not set in this shell.");
        println!("The console itself does not need it -- it uses the command from step 2.");
        None
    } else {
        println!("This checks DATABASE_URL, which is how `hm` connects -- not the command");
        println!("configured in step 2.\n");
        Some(run_hm(&["doctor"]))
    };
    finish(checked != Some(false));
}

/// The last word of the walkthrough. "Done." is for the case where nothing said otherwise.
fn finish(ok: bool) {
    if ok {
        println!("\nDone. `hypermnesia-stats` prints the same numbers the tray shows.");
    } else {
        println!("\nFinished with complaints above -- fix those first. The tray will show");
        println!("whatever the store answers, including nothing.");
    }
    println!("If something above was skipped, each step is its own command -- see --help.");
}

/// The name of an environment variable the command depends on, if it has one. Deliberately
/// simple: `$NAME` and `${NAME}`, which is what the presets and a hand-written psql line use.
fn shell_variable_in(cmd: &str) -> Option<String> {
    let i = cmd.find('$')?;
    let rest = &cmd[i + 1..];
    let rest = rest.strip_prefix('{').unwrap_or(rest);
    let name: String = rest.chars()
        .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
        .collect();
    (!name.is_empty()).then_some(name)
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

/// The repository root, or None. Resolved to an absolute path: `hm_path` can return the plain
/// relative name `hm`, and `Path::new("hm").parent()` is `Some("")`, not `None` -- so the
/// placeholder never fired and every interpolated path in the blocks below came out rooted at
/// "/", which reads like a real absolute path rather than an unfilled template.
fn repo_root() -> Option<PathBuf> {
    let p = hm_path()?;
    let abs = std::fs::canonicalize(&p).unwrap_or(p);
    abs.parent().filter(|d| !d.as_os_str().is_empty()).map(Path::to_path_buf)
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
    println!("  (A password inside the command ends up in psql's argv, where any process running");
    println!("   as you can read it. ~/.pgpass or PGPASSWORD in the command's own environment");
    println!("   keeps it out of sight.)\n");
    for (i, (name, _)) in PRESETS.iter().enumerate() {
        println!("  {}) {}", i + 1, describe(name));
    }
    println!("  {}) type the whole command yourself", PRESETS.len() + 1);

    // Asked until the answer is one of the offered numbers. Substituting preset 1 for a typo
    // discards the choice without a word -- and 0 used to be an arithmetic overflow besides.
    let idx = loop {
        let a = ask("\nNumber", "1");
        match a.trim().parse::<usize>() {
            Ok(n) if (1..=PRESETS.len() + 1).contains(&n) => break n,
            _ => println!("! pick a number between 1 and {}", PRESETS.len() + 1),
        }
    };
    let cmd = if idx == PRESETS.len() + 1 {
        ask("Command (receives SQL on stdin)", DEFAULT_PSQL_CMD)
    } else {
        fill(PRESETS[idx - 1].1)
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
        Ok(()) => {
            println!("Written to {}", config_path().display());
            // The file is not the last word: HM_PSQL_CMD in the environment beats it. --show
            // warns about this; the walkthrough, which is where a first-time user actually is,
            // did not.
            if let Some(env) = std::env::var("HM_PSQL_CMD").ok().filter(|v| !v.is_empty()) {
                if env != cfg["HM_PSQL_CMD"] {
                    println!("! HM_PSQL_CMD is exported in this shell and beats the file, so what");
                    println!("  you just tested is NOT what the next run here will use:");
                    println!("    {env}");
                    println!("  Unset it, or update your shell profile to match.");
                }
            }
        }
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
            // Quoted: the filled template is handed to `sh -c`, so a space, a `;` or a `$` in an
            // answer would change the command's word structure rather than the value.
            out = out.replace(slot, &shell_quote(ask(prompt, default).trim()));
        }
    }
    out
}

/// Wrap a value so `sh` treats it as one literal word.
fn shell_quote(v: &str) -> String {
    format!("'{}'", v.replace('\'', "'\\''"))
}

// -- the two blocks that touch someone else's files ------------------------------------------

fn offer_mcp_block(scope: Option<&str>) {
    let known_root = repo_root();
    let root = known_root.clone().unwrap_or_else(|| PathBuf::from("/path/to/hypermnesia"));
    // The scope is known if step 3 ingested one; otherwise it stays a placeholder and the text
    // below says so.
    let repo = scope.unwrap_or("myrepo");
    let block = format!(r#"{{ "mcpServers": {{ "hypermnesia": {{
  "command": "{root}/mcp-server/target/release/hypermnesia-mcp",
  "env": {{
    "HM_REPO": "{repo}",
    "DATABASE_URL": "postgresql://...",
    "HM_SEARCH":  "{root}/ingest/search.py",
    "HM_MEM_OPS": "{root}/ingest/mem_ops.py"
  }} }} }} }}"#, root = root.display(), repo = repo);
    println!("Add this to your MCP client's config (for Claude Code, the repo's .mcp.json):\n");
    println!("{block}\n");
    if scope.is_some() {
        println!("HM_REPO is filled in with the scope you just ingested under. The match is exact");
        println!("and case-sensitive: a name one character off resolves to nothing on every edit.");
    } else {
        println!("HM_REPO must be the scope you ingested under -- it is an exact, case-sensitive");
        println!("match, and a name one character off resolves to nothing on every edit.");
    }
    println!("DATABASE_URL above is still a placeholder: the MCP server connects directly, not");
    println!("through the command this wizard configured. Fill it in before you use the block.");
    if known_root.is_none() {
        println!("! The paths above are placeholders too -- this binary is not next to a checkout,");
        println!("  so the wizard does not know where the repository is.");
    }
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
        // Said every time, because what is on disk is not yet usable: the connection string is a
        // placeholder, and so are the paths when the repository could not be located.
        Ok(()) => {
            println!("Written to {target}");
            println!("! It still needs DATABASE_URL filled in{}.",
                     if known_root.is_none() { ", and the four paths" } else { "" });
        }
        Err(e) => println!("! Not written: {e}"),
    }
}

fn offer_hooks_block() {
    let root = repo_root().unwrap_or_else(|| PathBuf::from("/path/to/hypermnesia"));
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
            // Compared, not assumed: the two are usually identical right after --connect, and
            // announcing a disagreement that was never measured is its own small lie.
            match cfg.get("HM_PSQL_CMD") {
                Some(f) if f != v => println!("! the file holds a different one, and the \
                                               environment wins:\n  {f}"),
                Some(_) => println!("  the file holds the same command."),
                None => {}
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
    match io::stdin().read_line(&mut line) {
        // Ok(0) is EOF, not an empty line. Taking the default there meant that piping this
        // wizard anything -- or running it with no terminal at all -- answered every question
        // with its default, and the defaults are the ones that ACT: bring up docker, ingest a
        // repository, install a launchd job. An unattended install is not a thing to guess at.
        Ok(0) => {
            eprintln!("\nhypermnesia-setup: no one to ask (end of input). \
                       Run it in a terminal, or configure it with --connect.");
            std::process::exit(1);
        }
        Ok(_) => {}
        Err(e) => {
            eprintln!("\nhypermnesia-setup: cannot read the answer: {e}");
            std::process::exit(1);
        }
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

Connection settings go to ~/.config/hypermnesia/console.conf, created mode 600 -- the string can
carry a password. The file is REFUSED, whole, if another account can write it or it is not yours:
what it holds is a command this console runs unattended at login.

A password written into the command reaches psql's argv, which any process running as you can
read. ~/.pgpass, or PGPASSWORD set in the command's own environment, keeps it out of sight.

HM_PSQL_CMD in the environment beats the file.
";

#[cfg(test)]
mod tests {
    use super::*;

    /// The filled template is handed to `sh -c`. An answer with a space, a `;` or a `$` in it
    /// must stay one word, or the answer stops being a value and becomes part of the command.
    /// Verified by hand before this test: `my container; touch FILE` as a container name created
    /// the file.
    #[test]
    fn an_answer_cannot_become_part_of_the_command() {
        assert_eq!(shell_quote("hypermnesia-pg"), "'hypermnesia-pg'");
        assert_eq!(shell_quote("two words"), "'two words'");
        assert_eq!(shell_quote("a; touch /tmp/x"), "'a; touch /tmp/x'");
        assert_eq!(shell_quote("$HOME"), "'$HOME'");
        // The one case single quotes cannot hold on their own.
        assert_eq!(shell_quote("it's"), r#"'it'\''s'"#);
    }

    /// launchd starts the tray with a minimal environment. A command resting on a variable this
    /// shell happens to export was verified here and will fail there.
    #[test]
    fn a_command_resting_on_the_shell_is_recognised() {
        assert_eq!(shell_variable_in("psql \"$DATABASE_URL\" -tAX").as_deref(),
                   Some("DATABASE_URL"));
        assert_eq!(shell_variable_in("psql \"${DATABASE_URL}\"").as_deref(), Some("DATABASE_URL"));
        assert_eq!(shell_variable_in("docker exec -i pg psql -U hm -d hm"), None);
        assert_eq!(shell_variable_in("psql 'postgresql://hm@localhost/hm'"), None);
    }
}
