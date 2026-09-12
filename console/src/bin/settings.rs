//! Pipeline settings: show what is in effect, change the shared ones.
//!
//!     hypermnesia-settings                    what is in effect and where it came from
//!     hypermnesia-settings set <KEY> <value>  write it into the shared file
//!     hypermnesia-settings unset <KEY>        go back to the default value

use hypermnesia_console::settings::{self, Source, KNOBS};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        None => show(),
        Some("set") => {
            let (Some(k), Some(v)) = (args.get(1), args.get(2)) else {
                fail("give a key and a value");
            };
            match settings::set(k, v) {
                Ok(()) => {
                    println!("{k}={v} written into {}", settings::env_file().display());
                    // Without this line the setting would look as though it had taken effect
                    // immediately.
                    println!("It takes effect the next time whoever reads it starts. Processes \
                              already running do not re-read their environment.");
                    if std::env::var(k).is_ok() {
                        println!("! Note: {k} is also set in this session's environment -- there \
                                  it is stronger than the file.");
                    }
                }
                Err(e) => fail(&e),
            }
        }
        Some("unset") => {
            let Some(k) = args.get(1) else { fail("give a key") };
            match settings::unset(k) {
                Ok(()) => {
                    println!("{k} removed from {}", settings::env_file().display());
                    // "the default is in effect" is false in exactly the case the set branch
                    // already warns about: the same variable set in this shell.
                    match std::env::var(k) {
                        Ok(v) if !v.is_empty() => println!(
                            "! but {k}={v} is set in this session's environment, so the default \
                             is NOT in effect here. It is in effect for what launchd starts."),
                        _ => println!("the default value is in effect"),
                    }
                }
                Err(e) => fail(&e),
            }
        }
        Some("-h") | Some("--help") => print!("{HELP}"),
        Some(other) => fail(&format!("unknown command: {other}")),
    }
}

fn fail(e: &str) -> ! {
    eprintln!("hypermnesia-settings: {e}");
    std::process::exit(1)
}

fn show() {
    let file = settings::read();
    let path = settings::env_file();
    println!("Settings file: {}{}", path.display(),
             if path.exists() { "" } else { "  (not created yet)" });
    // The loader refuses the whole file on this, so every value in it is out of force. Without
    // this line the screen showed each one as "file" while the pipeline ran on defaults.
    let ignored = settings::fault();
    if let Some(why) = &ignored {
        println!("! the hooks IGNORE this file entirely: {why}");
        println!("! so everything below marked (file) is NOT in force -- the default is.");
        println!("  fix it with: chmod 600 {}", path.display());
    }
    println!();

    for k in KNOBS {
        let (mut val, src) = settings::effective(k, &file);
        let mark = match src {
            Source::Env(_) => "this shell",
            // A file the loader refuses is not a source of anything: the value column has to
            // show what is actually in force, which is the default.
            Source::File(_) if ignored.is_some() => {
                val = format!("{} (file: {val})", k.default);
                "default"
            }
            Source::File(_) => "file",
            Source::Default => "default",
        };
        println!("  {:<22} {:<20} {:<12} {}", k.key, val, mark, k.what);
        println!("  {:<22} read by {}", "", k.read_by);
        if matches!(src, Source::Env(_)) {
            // "environment" means this shell. launchd starts its jobs with a minimal one, so for
            // the scheduled half of the pipeline the file (or the default) is what is in force.
            let for_jobs = file.get(k.key).cloned()
                .filter(|_| ignored.is_none())
                .unwrap_or_else(|| format!("{} (the default)", k.default));
            println!("  {:<22} ! only in this shell; jobs launchd starts use {for_jobs}", "");
        }
    }
    println!();
    // The one limit that is invisible otherwise, and the reason this is a line of output rather
    // than a comment in the source: the file sets the environment of processes that start on
    // THIS machine.
    println!("These reach a process only if it starts on this machine. A script you run inside a");
    println!("container or a pod reads that environment, whatever this screen says.");
    println!();
    println!("hypermnesia-settings set <KEY> <value> -- write it; unset <KEY> -- back to the default");
}

const HELP: &str = "\
hypermnesia-settings -- the shared settings of the memory pipeline.

    hypermnesia-settings                     the effective values and where each came from
    hypermnesia-settings set <KEY> <value>   write it into ~/.claude/hypermnesia.env
    hypermnesia-settings unset <KEY>         remove it, going back to the default value

Where a value came from matters more than the value itself: a variable set in this shell is
stronger than the file, the file is stronger than the default -- but this shell is only this
shell. Jobs started by launchd get a minimal environment, so for them the file's value is the
one in force.

The hooks ignore the whole file unless it is your own private file in your own private
directory. The screen says so when it is not.
";
