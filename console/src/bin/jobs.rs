//! The pipeline's scheduled jobs: show them and run them.
//!
//!     hypermnesia-jobs                  what is configured and when it last worked
//!     hypermnesia-jobs run <short>      run now, without touching the schedule
//!     hypermnesia-jobs log <short>      the last lines of the log
//!
//! The short name is the tail of the label: extract, consolidate, reflect, freshness, rerank.

use hypermnesia_console::jobs::{self, Job};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    // Through jobs::job_prefix rather than the variable: only that path also consults the
    // config file, and the tray -- started by launchd with a minimal environment -- has
    // nothing else to read.
    let prefix = jobs::job_prefix();
    let all = match jobs::list(&prefix) {
        Ok(j) => j,
        Err(e) => fail(&e),
    };
    if all.is_empty() {
        fail(&format!("no job with the prefix {prefix} found in ~/Library/LaunchAgents"));
    }

    match args.first().map(String::as_str) {
        None => list(&all),
        Some("run") => {
            let Some(name) = args.get(1) else { fail("give the job's short name") };
            let j = find(&all, name);
            match jobs::run_now(&j.label) {
                Ok(msg) => {
                    println!("{msg}");
                    if let Some(p) = &j.log {
                        println!("log: {}", p.display());
                    }
                }
                Err(e) => fail(&e),
            }
        }
        Some("log") => {
            let Some(name) = args.get(1) else { fail("give the job's short name") };
            let n = args.get(2).and_then(|v| v.parse().ok()).unwrap_or(40);
            match jobs::tail(find(&all, name), n) {
                Ok(t) => println!("{t}"),
                Err(e) => fail(&e),
            }
        }
        Some("every") => {
            let (Some(name), Some(spec)) = (args.get(1), args.get(2)) else {
                fail("give the job's name and an interval, for example: every extract 4h");
            };
            let Some(secs) = parse_interval(spec) else {
                fail("an interval is written as 15m, 4h or 2d; less than a minute is not accepted");
            };
            apply(find(&all, name), &jobs::Schedule::Every(secs));
        }
        Some("at") => {
            let Some(name) = args.get(1) else { fail("give the job's name") };
            let (day, time) = match (args.get(2), args.get(3)) {
                (Some(d), Some(t)) => {
                    let Some(d) = parse_weekday(d) else {
                        fail("weekday: sun mon tue wed thu fri sat (or 0..6)");
                    };
                    (Some(d), t.as_str())
                }
                (Some(t), None) => (None, t.as_str()),
                _ => fail("give a time, for example: at consolidate 05:30"),
            };
            let Some((hour, minute)) = parse_hhmm(time) else { fail("a time is written as HH:MM") };
            apply(find(&all, name), &jobs::Schedule::At { hour, minute, weekday: day });
        }
        Some("-h") | Some("--help") => print!("{HELP}"),
        Some(other) => fail(&format!("unknown command: {other}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn intervals_parse_and_a_too_small_one_is_refused() {
        assert_eq!(parse_interval("4h"), Some(14400));
        assert_eq!(parse_interval("15m"), Some(900));
        assert_eq!(parse_interval("2d"), Some(172_800));
        assert_eq!(parse_interval("300"), Some(300));   // a bare number -- seconds
        // launchd treats zero unpredictably, and a job that goes out to the cluster more often
        // than once a minute is a way to take the gateway down with your own console.
        assert_eq!(parse_interval("0"), None);
        assert_eq!(parse_interval("30s"), None);
        assert_eq!(parse_interval("abc"), None);
    }

    #[test]
    fn times_and_weekdays_parse() {
        assert_eq!(parse_hhmm("05:30"), Some((5, 30)));
        assert_eq!(parse_hhmm("23:59"), Some((23, 59)));
        assert_eq!(parse_hhmm("24:00"), None);
        assert_eq!(parse_hhmm("5.30"), None);
        assert_eq!(parse_weekday("mon"), Some(1));
        assert_eq!(parse_weekday("SUN"), Some(0));
        assert_eq!(parse_weekday("tue"), Some(2));
        assert_eq!(parse_weekday("wed"), Some(3));
        assert_eq!(parse_weekday("thu"), Some(4));
        assert_eq!(parse_weekday("fri"), Some(5));
        assert_eq!(parse_weekday("sat"), Some(6));
        assert_eq!(parse_weekday("6"), Some(6));
        assert_eq!(parse_weekday("7"), None);
        assert_eq!(parse_weekday("monday"), None);
    }
}

fn apply(j: &Job, sched: &jobs::Schedule) {
    let was = j.schedule.human();
    match jobs::set_schedule(j, sched) {
        Ok(msg) => {
            println!("{msg}");
            println!("was: {was}");
            // launchd keeps its own copy of the schedule from the moment it loaded the job, so the
            // job is reloaded. That resets the run counter -- which has to be said out loud, or
            // the next look at the list will be alarming: a zero where everything is fine.
            println!("the job was reloaded into launchd; the run counter started over");
        }
        Err(e) => fail(&e),
    }
}

fn parse_interval(spec: &str) -> Option<u64> {
    let (num, mult) = match spec.chars().last()? {
        's' => (&spec[..spec.len() - 1], 1u64),
        'm' => (&spec[..spec.len() - 1], 60),
        'h' => (&spec[..spec.len() - 1], 3600),
        'd' => (&spec[..spec.len() - 1], 86_400),
        _ => (spec, 1),                       // a bare number -- seconds
    };
    let n: u64 = num.trim().parse().ok()?;
    // launchd treats zero unpredictably, and a job that goes out to the cluster more often than
    // once a minute is a way to take the gateway down with your own console.
    let total = n.checked_mul(mult)?;
    if total < 60 { return None; }
    Some(total)
}

fn parse_hhmm(s: &str) -> Option<(u32, u32)> {
    let (h, m) = s.split_once(':')?;
    let (h, m): (u32, u32) = (h.trim().parse().ok()?, m.trim().parse().ok()?);
    if h > 23 || m > 59 { return None; }
    Some((h, m))
}

fn parse_weekday(s: &str) -> Option<u32> {
    const DAYS: [&str; 7] = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
    let s = s.trim().to_lowercase();
    if let Some(i) = DAYS.iter().position(|d| *d == s) {
        return Some(i as u32);
    }
    s.parse().ok().filter(|d| *d <= 6)
}

fn find<'a>(all: &'a [Job], name: &str) -> &'a Job {
    match all.iter().find(|j| j.short() == name || j.label == name) {
        Some(j) if j.broken() => fail(&format!(
            "{name}: this plist cannot be read ({}), so launchd has not loaded it either. \
             Fix the file first.", j.fault.clone().unwrap_or_default())),
        Some(j) => j,
        None => fail(&format!("no such job: {name}. There is: {}",
                              all.iter().map(Job::short).collect::<Vec<_>>().join(", "))),
    }
}

fn fail(e: &str) -> ! {
    eprintln!("hypermnesia-jobs: {e}");
    std::process::exit(1)
}

fn list(all: &[Job]) {
    println!("{:<13} {:<22} {:<14} {}", "JOB", "SCHEDULE", "LAST OUTPUT", "STATE");
    for j in all {
        if let Some(why) = &j.fault {
            // An unreadable plist is one launchd also refused at login: the job is not running.
            // It used to be dropped from the list entirely, which is the one case the console had
            // nothing at all to say about.
            println!("{:<13} {:<22} {:<14} ! unreadable plist: {why}", j.short(), "—", "—");
            continue;
        }
        let when = match j.since_last_output() {
            Some(d) => ago(d),
            None => "—".to_string(),
        };
        let state = if j.running() {
            format!("running (pid {})", j.pid.unwrap_or(0))
        } else if j.never_ran() {
            // Not `runs == Some(0)`: that counter restarts at every login, so on its own it
            // reports every healthy job as never having run after a reboot.
            match j.since_installed() {
                Some(d) => format!("never ran (installed {})", ago(d)),
                None => "never ran".to_string(),
            }
        } else {
            match j.last_exit {
                // launchd remembers only the code of the LAST run, so this is not a history but a
                // single point. A non-zero code is shown as it is: for freshness a one means
                // "discrepancies were found", for the others it means a failure, and the console
                // has nothing to tell them apart by.
                Some(0) => format!("last run ok{}", runs_note(j)),
                Some(c) => format!("last run: code {c}{}", runs_note(j)),
                None => "not loaded into launchd".to_string(),
            }
        };
        println!("{:<13} {:<22} {:<14} {}", j.short(), j.schedule.human(), when, state);
        if j.overdue() {
            // We only shout when a period and a half has passed with nothing to show for it. A
            // job installed later than its own last slot honestly shows a zero and is not a
            // complaint: a false alarm here would teach one to scroll past this line.
            let what = if j.never_ran() {
                "the period has already passed and launchd still never ran it"
            } else {
                "it ran at some point, but the log has not moved for more than a period"
            };
            println!("{:<13} ! {what}", "");
        }
    }
    println!();
    println!("run <name> — run now; log <name> [N] — the tail of the log; \
              every/at <name> … — change the schedule (--help)");
}

fn runs_note(j: &Job) -> String {
    match j.runs {
        Some(n) if n > 0 => format!(", {n} runs"),
        _ => String::new(),
    }
}

fn ago(d: std::time::Duration) -> String {
    let s = d.as_secs();
    if s < 90 { format!("{s} s ago") }
    else if s < 5400 { format!("{} min ago", s / 60) }
    else if s < 172_800 { format!("{} h ago", s / 3600) }
    else { format!("{} d ago", s / 86400) }
}

const HELP: &str = "\
hypermnesia-jobs — the memory pipeline's scheduled jobs (launchd).

    hypermnesia-jobs                  what is configured, when it worked, the last exit code
    hypermnesia-jobs run <name>       run immediately (launchctl kickstart -k)
    hypermnesia-jobs log <name> [N]   the last N lines of the log (40 by default)
    hypermnesia-jobs every <name> 4h  an interval schedule (30s, 15m, 4h, 2d)
    hypermnesia-jobs at <name> 05:30      daily at that time
    hypermnesia-jobs at <name> mon 06:10  weekly on that day

Editing a schedule writes the plist, validates it and reloads the job into launchd: without the
reload launchd keeps working from its own copy, and the new schedule would be displayed where the
old one is in force. Before the edit a .bak is put down next to the file, and on any slip the
file is restored.

The name is the tail of the label: extract, consolidate, reflect, freshness, rerank.
The label prefix comes from HM_JOB_PREFIX, then the console config file, then the
default com.hypermnesia.
";
