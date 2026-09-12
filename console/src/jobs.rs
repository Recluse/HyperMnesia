//! The pipeline's scheduled jobs: what is configured, when it last worked, and how to run it now.
//!
//! All of it lives in launchd, so this is the macOS-specific part of the console. We read the
//! `*.plist` files through `plutil -convert json` and take their state from `launchctl list`. No
//! plist parser of our own: the format is binary about as often as it is text, and a homegrown
//! implementation would break silently on the very first binary file.
//!
//! The important part about "when it last worked": launchd does not keep that. It knows the exit
//! code of the LAST run and nothing else. So the time comes from the mtime of the log file -- and
//! a missing log means "it has not run once since the path was set", not "it is working quietly".
//! This is not a nicety: our reflect job is set to run weekly, and there is no log file at all.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, SystemTime};

/// Reverse-DNS prefix of the launchd labels this console manages.
pub const DEFAULT_JOB_PREFIX: &str = "com.hypermnesia";

/// Label of the tray's own autostart job, passed to `install_self`.
pub const DEFAULT_TRAY_LABEL: &str = "com.hypermnesia.tray";

/// The label prefix to look for. Environment, then the default -- an installation that names its
/// jobs differently can say so without a rebuild.
pub fn job_prefix() -> String {
    std::env::var("HM_JOB_PREFIX").ok().filter(|s| !s.is_empty())
        .unwrap_or_else(|| DEFAULT_JOB_PREFIX.to_string())
}

#[derive(Debug, Clone, PartialEq)]
pub enum Schedule {
    /// Every N seconds.
    Every(u64),
    /// At the given hour/minute, optionally on a specific weekday (0 = Sunday).
    At { hour: u32, minute: u32, weekday: Option<u32> },
    /// No schedule: a service that is simply kept running.
    None,
}

impl Schedule {
    pub fn human(&self) -> String {
        match self {
            Schedule::Every(s) if *s % 3600 == 0 => format!("every {} h", s / 3600),
            Schedule::Every(s) if *s % 60 == 0 => format!("every {} min", s / 60),
            Schedule::Every(s) => format!("every {s} s"),
            Schedule::At { hour, minute, weekday: None } => format!("daily {hour:02}:{minute:02}"),
            Schedule::At { hour, minute, weekday: Some(d) } => {
                const DAYS: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
                format!("weekly {} {hour:02}:{minute:02}", DAYS[(*d as usize) % 7])
            }
            Schedule::None => "on demand".into(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Job {
    pub label: String,
    pub plist: PathBuf,
    pub schedule: Schedule,
    pub program: Vec<String>,
    pub log: Option<PathBuf>,
    /// Exit code of the last run, as launchd remembers it. `None` -- the job is not loaded.
    pub last_exit: Option<i32>,
    /// When the plist last changed. Needed so as not to raise a false alarm: a job installed
    /// later than its own last slot simply has not come due yet.
    pub installed: Option<SystemTime>,
    /// How many times launchd has started it. Zero -- not once.
    ///
    /// Without this number the status column lies: `launchctl list` prints `0` in the status
    /// column both for "finished successfully" and for "never finished at all", so a job that has
    /// never started looks like one that ran without errors. Seen on reflect: the list showed
    /// "last run ok" with `runs = 0` and `last exit code = (never exited)`.
    pub runs: Option<u64>,
    /// PID, if the job is running right now.
    pub pid: Option<u32>,
    /// When something was last written to the log. The only available sign of "when it worked";
    /// `None` means there is no log -- that is, it has not run once since the path was
    /// configured.
    pub last_output: Option<SystemTime>,
}

impl Job {
    /// Short name without the reverse-DNS prefix: on the console's screen `extract`, not
    /// `com.hypermnesia.extract`.
    pub fn short(&self) -> &str {
        self.label.rsplit(['.', '-']).next().unwrap_or(&self.label)
    }

    pub fn running(&self) -> bool {
        self.pid.is_some()
    }

    /// How long until the job repeats. For calendar schedules -- a day or a week; the exact date
    /// of the next run is not needed, only the order of magnitude.
    pub fn period(&self) -> Option<Duration> {
        match self.schedule {
            Schedule::Every(s) => Some(Duration::from_secs(s)),
            Schedule::At { weekday: None, .. } => Some(Duration::from_secs(86_400)),
            Schedule::At { weekday: Some(_), .. } => Some(Duration::from_secs(7 * 86_400)),
            Schedule::None => None,
        }
    }

    /// launchd has never started it.
    pub fn never_ran(&self) -> bool {
        if self.schedule == Schedule::None {
            return false;                     // a service without a schedule -- not a complaint
        }
        match self.runs {
            Some(n) => n == 0,
            // The counter is unavailable (the job is not loaded): fall back to the sign "a log is
            // configured but not there" -- weaker, but better than silence.
            None => self.log.is_some() && self.last_output.is_none(),
        }
    }

    /// Has never run AND would have had time to, if it were working. THAT is the complaint;
    /// `never_ran` on its own is NOT.
    ///
    /// The difference is not cosmetic. A weekly job installed on Monday afternoon honestly shows
    /// zero runs until the next Monday, and shouting at it would be a lie. The signal must fire
    /// on "a whole period has passed and there are no runs", otherwise people learn to scroll
    /// past it and it stops working on the day it is needed.
    pub fn overdue(&self) -> bool {
        if !self.never_ran() {
            return false;
        }
        let (Some(period), Some(installed)) = (self.period(), self.installed) else {
            return true;                      // nothing to compare against -- better to speak up
        };
        SystemTime::now().duration_since(installed).map(|age| age > period).unwrap_or(false)
    }

    /// How long ago it was installed (by the plist's modification time).
    pub fn since_installed(&self) -> Option<Duration> {
        self.installed.and_then(|t| SystemTime::now().duration_since(t).ok())
    }

    pub fn since_last_output(&self) -> Option<Duration> {
        self.last_output.and_then(|t| SystemTime::now().duration_since(t).ok())
    }
}

fn agents_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    PathBuf::from(home).join("Library/LaunchAgents")
}

/// Every job with the given label prefix, in name order.
pub fn list(prefix: &str) -> Result<Vec<Job>, String> {
    let dir = agents_dir();
    let states = launchctl_states();
    let mut out = Vec::new();
    let entries = std::fs::read_dir(&dir)
        .map_err(|e| format!("cannot read {}: {e}", dir.display()))?;
    for e in entries.flatten() {
        let p = e.path();
        if p.extension().and_then(|x| x.to_str()) != Some("plist") {
            continue;
        }
        let name = p.file_stem().and_then(|x| x.to_str()).unwrap_or("");
        if !name.starts_with(prefix) {
            continue;
        }
        if let Some(job) = read_plist(&p, &states) {
            out.push(job);
        }
    }
    out.sort_by(|a, b| a.label.cmp(&b.label));
    Ok(out)
}

fn read_plist(path: &PathBuf, states: &BTreeMap<String, (Option<u32>, Option<i32>)>) -> Option<Job> {
    let out = Command::new("plutil")
        .args(["-convert", "json", "-o", "-"])
        .arg(path)
        .output().ok()?;
    if !out.status.success() {
        return None;
    }
    let json = String::from_utf8_lossy(&out.stdout);
    let label = str_field(&json, "Label")?;
    let schedule = if let Some(n) = int_field(&json, "StartInterval") {
        Schedule::Every(n as u64)
    } else if let Some(cal) = obj_field(&json, "StartCalendarInterval") {
        Schedule::At {
            hour: int_field(&cal, "Hour").unwrap_or(0) as u32,
            minute: int_field(&cal, "Minute").unwrap_or(0) as u32,
            weekday: int_field(&cal, "Weekday").map(|d| d as u32),
        }
    } else {
        Schedule::None
    };
    let log = str_field(&json, "StandardOutPath").map(PathBuf::from);
    let last_output = log.as_ref()
        .and_then(|p| std::fs::metadata(p).ok())
        .and_then(|m| m.modified().ok());
    let (pid, last_exit) = states.get(&label).cloned().unwrap_or((None, None));
    let runs = runs_of(&label);
    let installed = std::fs::metadata(path).ok().and_then(|m| m.modified().ok());
    Some(Job {
        program: array_field(&json, "ProgramArguments"),
        label, plist: path.clone(), schedule, log, last_exit, pid, last_output, runs, installed,
    })
}

/// How many times launchd has started the job. A separate `launchctl print` per job: it costs
/// about five milliseconds, and `launchctl list` does not print this number at all.
fn runs_of(label: &str) -> Option<u64> {
    let uid = users_uid();
    let o = Command::new("launchctl").args(["print", &format!("gui/{uid}/{label}")]).output().ok()?;
    if !o.status.success() {
        return None;                          // not loaded -- nothing to know
    }
    for line in String::from_utf8_lossy(&o.stdout).lines() {
        let t = line.trim();
        if let Some(v) = t.strip_prefix("runs = ") {
            return v.trim().parse().ok();
        }
    }
    None
}

/// `launchctl list` -> label => (pid, exit code of the last run).
fn launchctl_states() -> BTreeMap<String, (Option<u32>, Option<i32>)> {
    let mut out = BTreeMap::new();
    let Ok(o) = Command::new("launchctl").arg("list").output() else { return out };
    for line in String::from_utf8_lossy(&o.stdout).lines().skip(1) {
        let mut f = line.split('\t');
        let (pid, st, label) = (f.next(), f.next(), f.next());
        let (Some(pid), Some(st), Some(label)) = (pid, st, label) else { continue };
        out.insert(label.trim().to_string(), (pid.trim().parse().ok(), st.trim().parse().ok()));
    }
    out
}

/// Run the job immediately, without touching its schedule.
///
/// `kickstart -k` restarts even one that is already running. Returns launchctl's output: the
/// console must show what launchd answered, not silently shade a button.
pub fn run_now(label: &str) -> Result<String, String> {
    let uid = users_uid();
    let target = format!("gui/{uid}/{label}");
    let o = Command::new("launchctl").args(["kickstart", "-k", &target]).output()
        .map_err(|e| format!("could not call launchctl: {e}"))?;
    let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
    if !o.status.success() {
        return Err(if err.is_empty() {
            format!("launchctl kickstart {target} failed")
        } else {
            err
        });
    }
    Ok(if err.is_empty() { format!("started: {label}") } else { err })
}

fn users_uid() -> u32 {
    Command::new("id").arg("-u").output().ok()
        .and_then(|o| String::from_utf8_lossy(&o.stdout).trim().parse().ok())
        .unwrap_or(501)
}

/// Change a job's schedule: edit the plist and reload it into launchd.
///
/// The order is deliberate -- the file first, then bootout/bootstrap. Writing the file and NOT
/// reloading means showing a new schedule where the old one is in force: launchd holds its own
/// copy from the moment of loading and never re-reads the file itself. That is exactly the quiet
/// substitution this console is written for.
///
/// The file is first copied alongside with a `.bak` suffix: this is autostart configuration, and
/// leaving it broken halfway through an edit means losing the job altogether.
pub fn set_schedule(job: &Job, sched: &Schedule) -> Result<String, String> {
    if matches!(sched, Schedule::None) {
        return Err("the schedule cannot be removed this way: a job without a schedule does not \
                    run at all".into());
    }
    let path = job.plist.to_string_lossy().to_string();
    let backup = format!("{path}.bak");
    std::fs::copy(&path, &backup).map_err(|e| format!("cannot make a backup copy {backup}: {e}"))?;

    // The two keys are mutually exclusive: leaving the old one next to the new one means two
    // schedules, and which of them wins is up to launchd, not to us.
    let _ = plutil(&["-remove", "StartInterval", &path]);
    let _ = plutil(&["-remove", "StartCalendarInterval", &path]);
    let applied = match sched {
        Schedule::Every(secs) => {
            plutil(&["-insert", "StartInterval", "-integer", &secs.to_string(), &path])?;
            format!("every {secs} s")
        }
        Schedule::At { hour, minute, weekday } => {
            plutil(&["-insert", "StartCalendarInterval", "-dictionary", &path])?;
            plutil(&["-insert", "StartCalendarInterval.Hour", "-integer", &hour.to_string(), &path])?;
            plutil(&["-insert", "StartCalendarInterval.Minute", "-integer", &minute.to_string(), &path])?;
            if let Some(d) = weekday {
                plutil(&["-insert", "StartCalendarInterval.Weekday", "-integer", &d.to_string(), &path])?;
            }
            sched.human()
        }
        Schedule::None => unreachable!(),
    };

    // Check the file BEFORE reloading: a broken plist is simply not accepted by launchd, and the
    // job would stay unloaded -- that is, would quietly stop working.
    if let Err(e) = plutil(&["-lint", &path]) {
        let _ = std::fs::copy(&backup, &path);
        return Err(format!("the plist does not pass the check after the edit ({e}); the file was \
                            restored from the backup copy, the schedule was not changed"));
    }
    match reload(&job.label, &path) {
        Ok(note) => Ok(format!("{}: {applied}{note}", job.short())),
        Err(e) => {
            let _ = std::fs::copy(&backup, &path);
            let _ = reload(&job.label, &path);
            Err(format!("launchd did not accept the new schedule ({e}); the file was restored"))
        }
    }
}

/// Unload and load the job again, so that launchd sees the new file.
fn reload(label: &str, path: &str) -> Result<String, String> {
    let uid = users_uid();
    let domain = format!("gui/{uid}");
    // bootout may honestly say "no such thing" -- that is not an error, the job simply was not
    // loaded. An error would be bootstrap.
    let _ = Command::new("launchctl").args(["bootout", &format!("{domain}/{label}")]).output();
    let o = Command::new("launchctl").args(["bootstrap", &domain, path]).output()
        .map_err(|e| format!("could not call launchctl: {e}"))?;
    if !o.status.success() {
        let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
        return Err(if err.is_empty() { "bootstrap failed".into() } else { err });
    }
    Ok(String::new())
}

fn plutil(args: &[&str]) -> Result<(), String> {
    let o = Command::new("plutil").args(args).output()
        .map_err(|e| format!("could not call plutil: {e}"))?;
    if o.status.success() {
        return Ok(());
    }
    Err(String::from_utf8_lossy(&o.stderr).trim().to_string())
}

/// Put the console itself into autostart: a plist pointing at the current binary, loaded into
/// launchd.
///
/// `KeepAlive` is deliberate: a tray that quietly died is a console showing the state as of the
/// moment of its death. Better that it restarts. `RunAtLoad` -- so that it appears at login.
pub fn install_self(label: &str) -> Result<String, String> {
    let exe = std::env::current_exe()
        .map_err(|e| format!("cannot determine the path to myself: {e}"))?
        .to_string_lossy().to_string();
    let home = std::env::var("HOME").map_err(|_| "no HOME".to_string())?;
    let dir = PathBuf::from(&home).join("Library/LaunchAgents");
    std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let path = dir.join(format!("{label}.plist"));
    let log = format!("{home}/Library/Logs/hypermnesia/tray.log");
    if let Some(d) = PathBuf::from(&log).parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let plist = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>{exe}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"#);
    std::fs::write(&path, plist).map_err(|e| format!("{}: {e}", path.display()))?;
    if let Err(e) = plutil(&["-lint", &path.to_string_lossy()]) {
        let _ = std::fs::remove_file(&path);
        return Err(format!("the resulting plist does not pass the check: {e}"));
    }
    reload(label, &path.to_string_lossy())?;
    Ok(format!("{} installed into autostart\nbinary: {exe}\nlog: {log}", path.display()))
}

/// Take the console out of autostart.
pub fn uninstall_self(label: &str) -> Result<String, String> {
    let home = std::env::var("HOME").map_err(|_| "no HOME".to_string())?;
    let path = PathBuf::from(&home).join("Library/LaunchAgents").join(format!("{label}.plist"));
    let uid = users_uid();
    let _ = Command::new("launchctl").args(["bootout", &format!("gui/{uid}/{label}")]).output();
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(format!("{} removed from autostart", path.display())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound =>
            Err(format!("{} is not there anyway", path.display())),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

/// The last `n` lines of a job's log.
pub fn tail(job: &Job, n: usize) -> Result<String, String> {
    let Some(path) = job.log.as_ref() else {
        return Err("the job has no log file configured".into());
    };
    let data = std::fs::read(path)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    let text = String::from_utf8_lossy(&data);
    let lines: Vec<&str> = text.lines().collect();
    Ok(lines[lines.len().saturating_sub(n)..].join("\n"))
}

// -- minimal access to the fields of plutil's JSON ------------------------------------------
// The format is known to us and produced by a system utility, not by the network. A full parser
// here would be a dependency for the sake of five fields.

fn str_field(json: &str, key: &str) -> Option<String> {
    let pat = format!("\"{key}\":\"");
    let i = json.find(&pat)? + pat.len();
    let rest = &json[i..];
    let end = rest.find('"')?;
    Some(rest[..end].replace("\\/", "/"))
}

fn int_field(json: &str, key: &str) -> Option<i64> {
    let pat = format!("\"{key}\":");
    let i = json.find(&pat)? + pat.len();
    let rest = json[i..].trim_start();
    let end = rest.find(|c: char| !c.is_ascii_digit() && c != '-').unwrap_or(rest.len());
    rest[..end].parse().ok()
}

fn obj_field(json: &str, key: &str) -> Option<String> {
    let pat = format!("\"{key}\":{{");
    let i = json.find(&pat)? + pat.len() - 1;
    let rest = &json[i..];
    let mut depth = 0usize;
    for (j, c) in rest.char_indices() {
        if c == '{' { depth += 1; }
        if c == '}' {
            depth -= 1;
            if depth == 0 { return Some(rest[..=j].to_string()); }
        }
    }
    None
}

fn array_field(json: &str, key: &str) -> Vec<String> {
    let pat = format!("\"{key}\":[");
    let Some(i) = json.find(&pat) else { return Vec::new() };
    let rest = &json[i + pat.len()..];
    let Some(end) = rest.find(']') else { return Vec::new() };
    rest[..end].split(',')
        .filter_map(|s| {
            let s = s.trim().trim_matches('"');
            if s.is_empty() { None } else { Some(s.replace("\\/", "/")) }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    // Real output of `plutil -convert json` for our reflect job, shortened.
    const REFLECT: &str = r#"{"Nice":10,"ProcessType":"Background","StandardOutPath":"\/Users\/you\/.hypermnesia\/logs\/mem-reflect.log","StartCalendarInterval":{"Hour":6,"Minute":10,"Weekday":1},"ProgramArguments":["\/usr\/bin\/python3","\/Users\/you\/Work\/hypermnesia\/hooks\/mem_reflect.py"],"RunAtLoad":false,"Label":"com.hypermnesia.reflect"}"#;
    const EXTRACT: &str = r#"{"StandardOutPath":"\/Users\/you\/.hypermnesia\/logs\/mem-extract.log","StartInterval":14400,"ProgramArguments":["\/usr\/bin\/python3","\/x\/mem_extract.py"],"Label":"com.hypermnesia.extract"}"#;

    #[test]
    fn reads_a_weekly_calendar_schedule() {
        assert_eq!(str_field(REFLECT, "Label").as_deref(), Some("com.hypermnesia.reflect"));
        let cal = obj_field(REFLECT, "StartCalendarInterval").expect("calendar");
        assert_eq!(int_field(&cal, "Hour"), Some(6));
        assert_eq!(int_field(&cal, "Minute"), Some(10));
        assert_eq!(int_field(&cal, "Weekday"), Some(1));
        // The hour from the nested object must not be substituted by a number from another field.
        assert_eq!(int_field(REFLECT, "Nice"), Some(10));
    }

    #[test]
    fn reads_an_interval_schedule_and_unescapes_paths() {
        assert_eq!(int_field(EXTRACT, "StartInterval"), Some(14400));
        assert_eq!(str_field(EXTRACT, "StandardOutPath").as_deref(),
                   Some("/Users/you/.hypermnesia/logs/mem-extract.log"));
        assert_eq!(array_field(EXTRACT, "ProgramArguments"),
                   vec!["/usr/bin/python3", "/x/mem_extract.py"]);
    }

    #[test]
    fn schedules_render_in_words() {
        assert_eq!(Schedule::Every(14400).human(), "every 4 h");
        assert_eq!(Schedule::Every(900).human(), "every 15 min");
        // Something not a whole number of minutes stays in seconds instead of being rounded into
        // a lie.
        assert_eq!(Schedule::Every(90).human(), "every 90 s");
        assert_eq!(Schedule::At { hour: 5, minute: 30, weekday: None }.human(), "daily 05:30");
        assert_eq!(Schedule::At { hour: 6, minute: 10, weekday: Some(1) }.human(),
                   "weekly Mon 06:10");
        assert_eq!(Schedule::None.human(), "on demand");
    }

    /// A job launchd has never started must be called exactly that, not "successful".
    /// `launchctl list` prints 0 in the status column for both, which is why the run counter
    /// matters more here than the exit code.
    #[test]
    fn a_scheduled_job_that_never_ran_is_flagged() {
        let mut j = Job {
            label: "x".into(), plist: PathBuf::new(),
            schedule: Schedule::At { hour: 6, minute: 10, weekday: Some(1) },
            program: vec![], log: Some(PathBuf::from("/nope")),
            last_exit: Some(0),               // launchd says "0" -- and that means nothing
            pid: None, last_output: None, runs: Some(0),
            installed: Some(SystemTime::now() - Duration::from_secs(30 * 86_400)),
        };
        assert!(j.never_ran());

        j.runs = Some(44);                    // it did run -- no complaint, even without a fresh log
        assert!(!j.never_ran());

        j.runs = Some(0);
        j.schedule = Schedule::None;          // a service without a schedule -- also not a complaint
        assert!(!j.never_ran());

        // The counter is unavailable (the job is not loaded): fall back to "a log is configured
        // but not there".
        j.schedule = Schedule::Every(3600);
        j.runs = None;
        assert!(j.never_ran());
        j.last_output = Some(SystemTime::now());
        assert!(!j.never_ran());
    }

    /// Zero runs by itself is not a complaint. The complaint is zero runs AFTER a whole period
    /// has passed. A weekly job installed on Monday afternoon honestly shows zero until the next
    /// Monday, and shouting at it would be a false alarm -- and a false alarm teaches people to
    /// scroll past the line that will one day be real.
    #[test]
    fn a_job_installed_after_its_slot_is_not_overdue() {
        let week = Duration::from_secs(7 * 86_400);
        let mut j = Job {
            label: "reflect".into(), plist: PathBuf::new(),
            schedule: Schedule::At { hour: 6, minute: 10, weekday: Some(1) },
            program: vec![], log: Some(PathBuf::from("/nope")), last_exit: Some(0),
            pid: None, last_output: None, runs: Some(0),
            installed: Some(SystemTime::now() - Duration::from_secs(4 * 86_400)),
        };
        assert!(j.never_ran(), "the runs really are zero");
        assert!(!j.overdue(), "but the week is not over yet -- this is not a complaint");

        j.installed = Some(SystemTime::now() - week - Duration::from_secs(3600));
        assert!(j.overdue(), "the period passed with no runs -- now it is a complaint");

        j.runs = Some(1);
        assert!(!j.overdue(), "one run settles the question");
    }

    #[test]
    fn period_matches_the_schedule() {
        let mk = |sch| Job {
            label: "x".into(), plist: PathBuf::new(), schedule: sch, program: vec![],
            log: None, last_exit: None, pid: None, last_output: None, runs: None, installed: None,
        };
        assert_eq!(mk(Schedule::Every(14400)).period(), Some(Duration::from_secs(14400)));
        assert_eq!(mk(Schedule::At { hour: 5, minute: 30, weekday: None }).period(),
                   Some(Duration::from_secs(86_400)));
        assert_eq!(mk(Schedule::At { hour: 6, minute: 10, weekday: Some(1) }).period(),
                   Some(Duration::from_secs(7 * 86_400)));
        assert_eq!(mk(Schedule::None).period(), None);
    }

    #[test]
    fn short_name_drops_the_reverse_dns_prefix() {
        let j = Job {
            label: "com.hypermnesia.consolidate".into(), plist: PathBuf::new(),
            schedule: Schedule::None, program: vec![], log: None,
            last_exit: None, pid: None, last_output: None, runs: None, installed: None,
        };
        assert_eq!(j.short(), "consolidate");
    }
}
