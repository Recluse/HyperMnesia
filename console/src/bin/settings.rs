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
                Ok(()) => println!("{k} removed; the default value is in effect"),
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
    println!();

    for k in KNOBS {
        let (val, src) = settings::effective(k, &file);
        let mark = match src {
            Source::Env(_) => "environment",
            Source::File(_) => "file",
            Source::Default => "default",
        };
        println!("  {:<22} {:<12} {:<12} {}", k.key, val, mark, k.what);
        println!("  {:<22} read by {}", "", k.read_by);
        if matches!(src, Source::Env(_)) && file.contains_key(k.key) {
            // Otherwise the value in the file looks like the one in effect, when it has been
            // overridden.
            println!("  {:<22} ! the file says {:?}, but the environment is stronger", "",
                     file.get(k.key).cloned().unwrap_or_default());
        }
    }
    println!();
    // The one limit that is invisible otherwise, and the reason this is a line of output rather
    // than a comment in the source: the file sets the environment of processes that start on
    // THIS machine. If you deployed ingest/mem_ops.py into a container or a pod, its own
    // environment wins there and nothing written here reaches it.
    println!("These reach a process only if it starts on this machine. If you run");
    println!("ingest/mem_ops.py in a container or a pod, that environment wins for the two knobs");
    println!("it reads, whatever this screen says.");
    println!();
    println!("hypermnesia-settings set <KEY> <value> -- write it; unset <KEY> -- back to the default");
}

const HELP: &str = "\
hypermnesia-settings -- the shared settings of the memory pipeline.

    hypermnesia-settings                     the effective values and where each came from
    hypermnesia-settings set <KEY> <value>   write it into ~/.claude/hypermnesia.env
    hypermnesia-settings unset <KEY>         remove it, going back to the default value

Where a value came from matters more than the value itself: an environment variable is
stronger than the file, the file is stronger than the default. Knobs that are read inside
a cluster pod are shown read-only -- the local file does not reach them.
";
