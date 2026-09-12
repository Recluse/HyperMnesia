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

/// The label prefix to look for: environment, then the config file, then the default -- the same
/// order as every other setting here.
///
/// The config file matters more than it looks. The tray is started by launchd, which gives it a
/// minimal environment and none of a login shell's variables, so an installation whose jobs are
/// named differently could set HM_JOB_PREFIX in its shell forever and the tray would still show
/// an empty job list -- while every command run by hand showed the right one.
pub fn job_prefix() -> String {
    if let Some(v) = std::env::var("HM_JOB_PREFIX").ok().filter(|s| !s.is_empty()) {
        return v;
    }
    // Filtered the same way as the environment: an empty HM_JOB_PREFIX in the config makes
    // `starts_with` match every LaunchAgent the person owns, and this module edits and kickstarts
    // what it lists.
    if let Some(v) = crate::config().get("HM_JOB_PREFIX").filter(|s| !s.is_empty()) {
        return v.clone();
    }
    DEFAULT_JOB_PREFIX.to_string()
}

#[derive(Debug, Clone, PartialEq)]
pub enum Schedule {
    /// Every N seconds.
    Every(u64),
    /// At the given hour/minute, optionally on a specific weekday (0 = Sunday).
    At { hour: u32, minute: u32, weekday: Option<u32> },
    /// Several calendar slots (launchd accepts an array of dicts). The count is what matters
    /// here; showing all of them would need a screen this console does not have. Kept separate
    /// from `At` so that nothing pretends the first slot is the whole schedule.
    Several(usize),
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
            Schedule::Several(n) => format!("{n} times a day"),
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
    /// Why this job could not be read. A plist in LaunchAgents that `plutil` refuses is a plist
    /// launchd also refused at login -- the job is not running, and that is precisely the case
    /// the console used to have nothing to say about, because the row was dropped from the list.
    pub fault: Option<String>,
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
        match &self.schedule {
            Schedule::Every(s) => Some(Duration::from_secs(*s)),
            Schedule::At { weekday: None, .. } => Some(Duration::from_secs(86_400)),
            Schedule::At { weekday: Some(_), .. } => Some(Duration::from_secs(7 * 86_400)),
            // Several slots a day: the longest gap between them is at most a day.
            Schedule::Several(_) => Some(Duration::from_secs(86_400)),
            Schedule::None => None,
        }
    }

    /// Something is wrong with the job itself, before any question of when it last ran.
    pub fn broken(&self) -> bool {
        self.fault.is_some()
    }

    /// launchd has never started it.
    ///
    /// `runs` alone does not answer this. It is per-bootstrap state: launchd loads every
    /// LaunchAgent again at each login and the counter starts at zero, while the plist's mtime
    /// does not move. Trusting it turned every healthy job into "never ran / overdue" after a
    /// reboot. So the log's mtime -- the only evidence that survives a restart -- has to agree.
    pub fn never_ran(&self) -> bool {
        if self.schedule == Schedule::None || self.broken() {
            return false;                     // a service without a schedule -- not a complaint
        }
        if self.last_output.is_some() {
            return false;                     // it has written something: it ran
        }
        match self.runs {
            Some(n) => n == 0,
            // The counter is unavailable (the job is not loaded): fall back to the sign "a log is
            // configured but not there" -- weaker, but better than silence.
            None => self.log.is_some(),
        }
    }

    /// Has never run AND would have had time to -- or ran once and then stopped. THAT is the
    /// complaint; `never_ran` on its own is NOT.
    ///
    /// The difference is not cosmetic. A weekly job installed on Monday afternoon honestly shows
    /// zero runs until the next Monday, and shouting at it would be a lie. The signal must fire
    /// on "a whole period has passed and nothing happened", otherwise people learn to scroll
    /// past it and it stops working on the day it is needed.
    ///
    /// A period and a half is the threshold, not one period: a job whose slot is due about now
    /// has not failed yet.
    pub fn overdue(&self) -> bool {
        let Some(period) = self.period() else { return false };
        let slack = period + period / 2;
        if self.never_ran() {
            // The only date available is the plist's own.
            return self.since_installed().map(|age| age > slack).unwrap_or(true);
        }
        // It ran at some point. Then the complaint is staleness -- and only the log can date it.
        // A job with no log configured and a positive run counter is evidence of nothing either
        // way, and guessing there is how a console starts crying wolf.
        match self.since_last_output() {
            Some(age) => age > slack,
            None => false,
        }
    }

    /// How long ago it was installed (by the plist's modification time).
    pub fn since_installed(&self) -> Option<Duration> {
        self.installed.and_then(|t| SystemTime::now().duration_since(t).ok())
    }

    pub fn since_last_output(&self) -> Option<Duration> {
        self.last_output.and_then(|t| SystemTime::now().duration_since(t).ok())
    }
}

/// Where the user's own LaunchAgents live.
///
/// An error rather than a guess when HOME is unset: the old fallback produced
/// `/Library/LaunchAgents`, which exists on every Mac, so `read_dir` succeeded and the console
/// reported an honest-looking empty list for the wrong directory. `install_self` already refused
/// to guess in the same situation.
fn agents_dir() -> Result<PathBuf, String> {
    let home = std::env::var("HOME")
        .map_err(|_| "HOME is not set, so I cannot tell where your LaunchAgents are".to_string())?;
    if home.is_empty() {
        return Err("HOME is empty, so I cannot tell where your LaunchAgents are".into());
    }
    Ok(PathBuf::from(home).join("Library/LaunchAgents"))
}

/// Every job with the given label prefix, in name order.
///
/// A plist that cannot be read is KEPT, with `fault` set. Dropping it was the worst of the two
/// choices: an unreadable plist is one launchd also refused, so the job is not running, and the
/// console's list said nothing at all about it.
pub fn list(prefix: &str) -> Result<Vec<Job>, String> {
    let dir = agents_dir()?;
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
        match read_plist(&p, &states) {
            Ok(job) => out.push(job),
            Err(why) => out.push(broken_job(&p, name, why)),
        }
    }
    out.sort_by(|a, b| a.label.cmp(&b.label));
    Ok(out)
}

/// A row for a plist we could not read: named after the file, carrying the reason.
fn broken_job(path: &PathBuf, name: &str, fault: String) -> Job {
    Job {
        label: name.to_string(),
        plist: path.clone(),
        schedule: Schedule::None,
        program: Vec::new(),
        log: None,
        last_exit: None,
        installed: std::fs::metadata(path).ok().and_then(|m| m.modified().ok()),
        runs: None,
        pid: None,
        last_output: None,
        fault: Some(fault),
    }
}

fn read_plist(path: &PathBuf, states: &BTreeMap<String, (Option<u32>, Option<i32>)>)
    -> Result<Job, String> {
    let out = Command::new("plutil")
        .args(["-convert", "json", "-o", "-"])
        .arg(path)
        .output().map_err(|e| format!("could not run plutil: {e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
        return Err(if err.is_empty() { "plutil could not read it".into() } else { err });
    }
    let json = String::from_utf8_lossy(&out.stdout);
    let Plist { label, schedule, log, program } = parse_plist_json(&json)?;
    let last_output = log.as_ref()
        .and_then(|p| std::fs::metadata(p).ok())
        .and_then(|m| m.modified().ok());
    let (pid, last_exit) = states.get(&label).cloned().unwrap_or((None, None));
    let runs = runs_of(&label);
    let installed = std::fs::metadata(path).ok().and_then(|m| m.modified().ok());
    Ok(Job {
        program,
        label, plist: path.clone(), schedule, log, last_exit, pid, last_output, runs, installed,
        fault: None,
    })
}

/// What a plist says, before anything is asked of launchd or of the filesystem. Separate from
/// `read_plist` so it can be tested against real plutil output without a Mac in the loop.
struct Plist {
    label: String,
    schedule: Schedule,
    log: Option<PathBuf>,
    program: Vec<String>,
}

/// Read plutil's JSON with the same structural parser the store's answer goes through.
///
/// The substring version this replaces had the array form of StartCalendarInterval as a blind
/// spot: several run times a day read as no schedule at all, which also exempted the job from
/// every never-ran check, since a job with no schedule is never a complaint.
fn parse_plist_json(json: &str) -> Result<Plist, String> {
    use crate::Json;
    let v = crate::parse_json(json).map_err(|e| format!("plutil's own JSON did not parse: {e}"))?;
    let label = v.get("Label").and_then(Json::as_str)
        .ok_or("the plist has no Label")?.to_string();
    let slot = |d: &Json| Schedule::At {
        hour: d.get("Hour").and_then(Json::as_i64).unwrap_or(0) as u32,
        minute: d.get("Minute").and_then(Json::as_i64).unwrap_or(0) as u32,
        weekday: d.get("Weekday").and_then(Json::as_i64).map(|d| d as u32),
    };
    let schedule = if let Some(n) = v.get("StartInterval").and_then(Json::as_i64) {
        Schedule::Every(n.max(0) as u64)
    } else {
        match v.get("StartCalendarInterval") {
            Some(cal @ Json::Obj(_)) => slot(cal),
            Some(Json::Arr(slots)) if slots.len() == 1 => slot(&slots[0]),
            Some(Json::Arr(slots)) if !slots.is_empty() => Schedule::Several(slots.len()),
            _ => Schedule::None,
        }
    };
    Ok(Plist {
        label,
        schedule,
        log: v.get("StandardOutPath").and_then(Json::as_str).map(PathBuf::from),
        program: v.get("ProgramArguments").and_then(Json::as_arr)
            .map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
            .unwrap_or_default(),
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
/// `kickstart -k` restarts even one that is already running. It returns 0 as soon as launchd
/// ACCEPTS the request, which is not the same as the job having started: a job whose program is
/// missing exits immediately and launchd records that asynchronously. So the state is read back
/// afterwards and the answer says which of the two happened -- "started" over a job that failed
/// on every run is the kind of quiet lie this console exists to prevent.
pub fn run_now(label: &str) -> Result<String, String> {
    let uid = users_uid();
    let target = format!("gui/{uid}/{label}");
    let before = runs_of(label);
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
    if !err.is_empty() {
        return Ok(err);
    }
    // A short look back. Long enough for a program that is not there to have failed, short
    // enough that a person does not notice the wait.
    std::thread::sleep(Duration::from_millis(600));
    let states = launchctl_states();
    let (pid, last_exit) = states.get(label).cloned().unwrap_or((None, None));
    let after = runs_of(label);
    if pid.is_some() {
        return Ok(format!("running: {label}"));
    }
    match (before, after) {
        // It started and has already finished. The exit code is the whole answer.
        (_, Some(_)) if matches!(last_exit, Some(c) if c != 0) => Err(format!(
            "launchd started {label} and it exited {} straight away", last_exit.unwrap_or(-1))),
        (Some(b), Some(a)) if a > b => Ok(format!("ran: {label} (exit {})",
            last_exit.map(|c| c.to_string()).unwrap_or_else(|| "unknown".into()))),
        (Some(b), Some(a)) if a == b => Err(format!(
            "launchd accepted the request but {label} has not started (still {a} runs)")),
        // The counter is unavailable: say that rather than claim a start.
        _ => Ok(format!("launchd accepted the request for {label};                          it does not report a run counter, so check the log")),
    }
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
    if matches!(sched, Schedule::None | Schedule::Several(_)) {
        return Err("the schedule cannot be removed this way: a job without a schedule does not \
                    run at all".into());
    }
    if let Some(why) = job.fault.as_ref() {
        return Err(format!("this plist could not be read ({why}) -- I will not edit it"));
    }
    let path = job.plist.to_string_lossy().to_string();
    let backup = format!("{path}.bak");
    std::fs::copy(&path, &backup).map_err(|e| format!("cannot make a backup copy {backup}: {e}"))?;

    // Every exit from the edit goes through the restore. It used to leave early on a failing
    // insert, with the file already stripped of BOTH schedule keys and never reloaded: a job
    // with no schedule at all, a stray .bak beside it, and a help text promising the opposite.
    let edited = edit_schedule(&path, sched);
    let applied = match edited {
        Ok(applied) => applied,
        Err(e) => {
            let restored = std::fs::copy(&backup, &path).is_ok();
            return Err(if restored {
                format!("the edit failed ({e}); the file was restored from the backup copy")
            } else {
                format!("the edit failed ({e}) AND the file could not be restored -- the backup \
                         is at {backup}")
            });
        }
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
            // Restoring the file is the easy half. The half that matters is whether launchd has
            // the job again: reload() boots it out first, so a second failure here leaves it
            // loaded nowhere -- scheduled by nothing until the next login. Say which it is.
            let _ = std::fs::copy(&backup, &path);
            match reload(&job.label, &path) {
                Ok(_) => Err(format!("launchd did not accept the new schedule ({e}); the file was \
                                      restored and the job is loaded again")),
                Err(e2) => Err(format!("launchd did not accept the new schedule ({e}); the file \
                    was restored BUT the job is NOT loaded and will not run ({e2}). Load it with: \
                    launchctl bootstrap gui/{} {path}", users_uid())),
            }
        }
    }
}

/// The plist edit itself. Returns what was applied, in words.
fn edit_schedule(path: &str, sched: &Schedule) -> Result<String, String> {
    // The two keys are mutually exclusive: leaving the old one next to the new one means two
    // schedules, and which of them wins is up to launchd, not to us.
    let _ = plutil(&["-remove", "StartInterval", path]);
    let _ = plutil(&["-remove", "StartCalendarInterval", path]);
    match sched {
        Schedule::Every(secs) => {
            plutil(&["-insert", "StartInterval", "-integer", &secs.to_string(), path])?;
            Ok(format!("every {secs} s"))
        }
        Schedule::At { hour, minute, weekday } => {
            plutil(&["-insert", "StartCalendarInterval", "-dictionary", path])?;
            plutil(&["-insert", "StartCalendarInterval.Hour", "-integer", &hour.to_string(), path])?;
            plutil(&["-insert", "StartCalendarInterval.Minute", "-integer", &minute.to_string(), path])?;
            if let Some(d) = weekday {
                plutil(&["-insert", "StartCalendarInterval.Weekday", "-integer", &d.to_string(), path])?;
            }
            Ok(sched.human())
        }
        Schedule::Several(_) | Schedule::None => Err("this console can set one schedule at a \
            time; several calendar slots have to be edited in the plist by hand".into()),
    }
}

/// Unload and load the job again, so that launchd sees the new file.
///
/// `bootout` returns as soon as teardown has been *initiated*; while the old instance drains,
/// `bootstrap` fails with "Bootstrap failed: 5: Input/output error". So the bootout is waited out
/// -- `launchctl print` stops knowing the label -- before bootstrapping. That race was the cause
/// of the worst state this module could produce: a job booted out of launchd and a message saying
/// nothing had changed.
fn reload(label: &str, path: &str) -> Result<String, String> {
    let uid = users_uid();
    let domain = format!("gui/{uid}");
    // bootout may honestly say "no such thing" -- that is not an error, the job simply was not
    // loaded. An error would be bootstrap.
    let _ = Command::new("launchctl").args(["bootout", &format!("{domain}/{label}")]).output();
    let gone_by = std::time::Instant::now() + Duration::from_secs(3);
    while std::time::Instant::now() < gone_by && is_loaded(uid, label) {
        std::thread::sleep(Duration::from_millis(100));
    }
    let o = Command::new("launchctl").args(["bootstrap", &domain, path]).output()
        .map_err(|e| format!("could not call launchctl: {e}"))?;
    if !o.status.success() {
        let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
        return Err(if err.is_empty() { "bootstrap failed".into() } else { err });
    }
    Ok(String::new())
}

fn is_loaded(uid: u32, label: &str) -> bool {
    Command::new("launchctl").args(["print", &format!("gui/{uid}/{label}")])
        .output().map(|o| o.status.success()).unwrap_or(false)
}

fn plutil(args: &[&str]) -> Result<(), String> {
    let o = Command::new("plutil").args(args).output()
        .map_err(|e| format!("could not call plutil: {e}"))?;
    if o.status.success() {
        return Ok(());
    }
    Err(String::from_utf8_lossy(&o.stderr).trim().to_string())
}

/// The autostart plist, as text. A function so a test can read what is actually written.
fn tray_plist(label: &str, exe: &str, log: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>{exe}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"#)
}

/// Put the console itself into autostart: a plist pointing at the current binary, loaded into
/// launchd.
///
/// `KeepAlive` is deliberate: a tray that quietly died is a console showing the state as of the
/// moment of its death. Better that it restarts. `RunAtLoad` -- so that it appears at login.
///
/// `KeepAlive` is a dict, not `true`: unconditional means launchd restarts the tray about ten
/// seconds after the person chooses Quit, which is a menu item that does not do what it says.
/// `SuccessfulExit=false` keeps the intent -- restart a tray that crashed -- and lets Quit
/// stick.
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
    let plist = tray_plist(label, &exe, &log);
    std::fs::write(&path, plist).map_err(|e| format!("{}: {e}", path.display()))?;
    if let Err(e) = plutil(&["-lint", &path.to_string_lossy()]) {
        let _ = std::fs::remove_file(&path);
        return Err(format!("the resulting plist does not pass the check: {e}"));
    }
    // The file IS installed at this point, so a bare Err would be a third state: an error
    // message over a completed install that starts at the next login anyway.
    if let Err(e) = reload(label, &path.to_string_lossy()) {
        return Ok(format!("{} written, but launchd did not take it now ({e}).\n\
                           It will start at your next login. To start it now:\n\
                           launchctl bootstrap gui/{} {}",
                          path.display(), users_uid(), path.display()));
    }
    Ok(format!("{} installed into autostart\nbinary: {exe}\nlog: {log}", path.display()))
}

/// Is the installed autostart entry still pointing at this binary?
///
/// `install_self` bakes the current path into the plist and nothing ever looks again. A tray
/// moved, rebuilt elsewhere, or installed from a copy that has since been deleted keeps a plist
/// that launchd will fail to start at every login, silently.
pub fn autostart_fault(label: &str) -> Option<String> {
    let home = std::env::var("HOME").ok()?;
    let path = PathBuf::from(&home).join("Library/LaunchAgents").join(format!("{label}.plist"));
    let out = Command::new("plutil").args(["-convert", "json", "-o", "-"]).arg(&path)
        .output().ok()?;
    if !out.status.success() {
        return Some(format!("the autostart plist cannot be read: {}",
                            String::from_utf8_lossy(&out.stderr).trim()));
    }
    let installed = parse_plist_json(&String::from_utf8_lossy(&out.stdout)).ok()?
        .program.into_iter().next()?;
    if !std::path::Path::new(&installed).exists() {
        return Some(format!("autostart points at {installed}, which is not there any more"));
    }
    let me = std::env::current_exe().ok()?.to_string_lossy().to_string();
    (installed != me).then(|| format!("autostart starts {installed}, not this binary ({me})"))
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

#[cfg(test)]
mod tests {
    use super::*;

    // Real output of `plutil -convert json` for our reflect job, shortened.
    const REFLECT: &str = r#"{"Nice":10,"ProcessType":"Background","StandardOutPath":"\/Users\/you\/.hypermnesia\/logs\/mem-reflect.log","StartCalendarInterval":{"Hour":6,"Minute":10,"Weekday":1},"ProgramArguments":["\/usr\/bin\/python3","\/Users\/you\/Work\/hypermnesia\/hooks\/mem_reflect.py"],"RunAtLoad":false,"Label":"com.hypermnesia.reflect"}"#;
    const EXTRACT: &str = r#"{"StandardOutPath":"\/Users\/you\/.hypermnesia\/logs\/mem-extract.log","StartInterval":14400,"ProgramArguments":["\/usr\/bin\/python3","\/x\/mem_extract.py"],"Label":"com.hypermnesia.extract"}"#;
    // launchd also accepts an ARRAY of calendar dicts. This one ran as "no schedule at all".
    const TWICE: &str = r#"{"Label":"com.hypermnesia.twice","StartCalendarInterval":[{"Hour":6,"Minute":10},{"Hour":18,"Minute":10}],"StandardOutPath":"\/tmp\/twice.log"}"#;

    fn job(schedule: Schedule) -> Job {
        Job {
            label: "x".into(), plist: PathBuf::new(), schedule, program: vec![], log: None,
            last_exit: None, pid: None, last_output: None, runs: None, installed: None,
            fault: None,
        }
    }

    #[test]
    fn reads_a_weekly_calendar_schedule() {
        let p = parse_plist_json(REFLECT).expect("parse");
        assert_eq!(p.label, "com.hypermnesia.reflect");
        assert_eq!(p.schedule, Schedule::At { hour: 6, minute: 10, weekday: Some(1) });
        // The hour must come from the nested object, not from whatever number is nearest.
        assert_eq!(p.log.as_deref().and_then(|x| x.to_str()),
                   Some("/Users/you/.hypermnesia/logs/mem-reflect.log"));
    }

    #[test]
    fn reads_an_interval_schedule_and_unescapes_paths() {
        let p = parse_plist_json(EXTRACT).expect("parse");
        assert_eq!(p.schedule, Schedule::Every(14400));
        assert_eq!(p.log.as_deref().and_then(|x| x.to_str()),
                   Some("/Users/you/.hypermnesia/logs/mem-extract.log"));
        assert_eq!(p.program, vec!["/usr/bin/python3", "/x/mem_extract.py"]);
    }

    /// The array form of StartCalendarInterval used to read as Schedule::None, which both
    /// printed "on demand" for a scheduled job and exempted it from every never-ran check.
    #[test]
    fn several_calendar_slots_are_a_schedule_not_silence() {
        let p = parse_plist_json(TWICE).expect("parse");
        assert_eq!(p.schedule, Schedule::Several(2));
        assert_ne!(p.schedule, Schedule::None);
        assert_eq!(p.schedule.human(), "2 times a day");
        assert_eq!(job(p.schedule).period(), Some(Duration::from_secs(86_400)));
    }

    #[test]
    fn a_plist_without_a_label_is_an_error_not_a_silent_drop() {
        assert!(parse_plist_json(r#"{"StartInterval":60}"#).is_err());
        assert!(parse_plist_json("not json at all").is_err());
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
        let mut j = job(Schedule::At { hour: 6, minute: 10, weekday: Some(1) });
        j.log = Some(PathBuf::from("/nope"));
        j.last_exit = Some(0);                // launchd says "0" -- and that means nothing
        j.runs = Some(0);
        j.installed = Some(SystemTime::now() - Duration::from_secs(30 * 86_400));
        assert!(j.never_ran());

        j.runs = Some(44);                    // launchd started it since the last login
        assert!(!j.never_ran());

        j.runs = Some(0);
        j.schedule = Schedule::None;          // a service without a schedule -- not a complaint
        assert!(!j.never_ran());

        // The counter is unavailable (the job is not loaded): fall back to "a log is configured
        // but not there".
        j.schedule = Schedule::Every(3600);
        j.runs = None;
        assert!(j.never_ran());
        j.last_output = Some(SystemTime::now());
        assert!(!j.never_ran());
    }

    /// The run counter is per-bootstrap: launchd resets it at every login while the plist's
    /// mtime stays where it was. Keying "never ran" on it alone turned every healthy job into a
    /// false alarm after a reboot -- which is how a warning stops being read.
    #[test]
    fn a_reboot_does_not_turn_a_working_job_into_a_false_alarm() {
        let mut j = job(Schedule::Every(14400));
        j.log = Some(PathBuf::from("/nope"));
        j.installed = Some(SystemTime::now() - Duration::from_secs(30 * 86_400));
        j.runs = Some(0);                                       // just booted: counter reset
        j.last_output = Some(SystemTime::now() - Duration::from_secs(600)); // but it ran recently
        assert!(!j.never_ran(), "the log says it ran ten minutes ago");
        assert!(!j.overdue());
    }

    /// The other direction of the same mistake: a job that ran once and then stopped used to be
    /// exempt forever, because a positive counter ended the question.
    #[test]
    fn a_job_that_ran_once_and_then_stalled_is_still_overdue() {
        let mut j = job(Schedule::Every(3600));
        j.log = Some(PathBuf::from("/nope"));
        j.runs = Some(12);
        j.installed = Some(SystemTime::now() - Duration::from_secs(30 * 86_400));
        j.last_output = Some(SystemTime::now() - Duration::from_secs(600));
        assert!(!j.overdue(), "ten minutes into an hourly job is fine");

        j.last_output = Some(SystemTime::now() - Duration::from_secs(6 * 3600));
        assert!(j.overdue(), "six hours into an hourly job is not");
    }

    /// Zero runs by itself is not a complaint. The complaint is zero runs AFTER a whole period
    /// has passed. A weekly job installed on Monday afternoon honestly shows zero until the next
    /// Monday, and shouting at it would be a false alarm -- and a false alarm teaches people to
    /// scroll past the line that will one day be real.
    #[test]
    fn a_job_installed_after_its_slot_is_not_overdue() {
        let week = Duration::from_secs(7 * 86_400);
        let mut j = job(Schedule::At { hour: 6, minute: 10, weekday: Some(1) });
        j.log = Some(PathBuf::from("/nope"));
        j.last_exit = Some(0);
        j.runs = Some(0);
        j.installed = Some(SystemTime::now() - Duration::from_secs(4 * 86_400));
        assert!(j.never_ran(), "the runs really are zero");
        assert!(!j.overdue(), "but the week is not over yet -- this is not a complaint");

        j.installed = Some(SystemTime::now() - week - week / 2 - Duration::from_secs(3600));
        assert!(j.overdue(), "the period passed with no runs -- now it is a complaint");

        j.last_output = Some(SystemTime::now());
        assert!(!j.overdue(), "a fresh log settles the question");
    }

    /// A plist that cannot be read is kept as a row with a fault, never dropped: an unreadable
    /// plist is one launchd also refused, so the job is not running -- the one case the list had
    /// nothing to say about.
    #[test]
    fn an_unreadable_plist_becomes_a_row_not_a_gap() {
        let j = broken_job(&PathBuf::from("/x/com.hypermnesia.bad.plist"),
                           "com.hypermnesia.bad", "plutil: unexpected character".into());
        assert!(j.broken());
        assert_eq!(j.short(), "bad");
        assert!(!j.never_ran(), "a fault is its own complaint, not a run-count one");
        assert!(!j.overdue());
    }

    #[test]
    fn period_matches_the_schedule() {
        assert_eq!(job(Schedule::Every(14400)).period(), Some(Duration::from_secs(14400)));
        assert_eq!(job(Schedule::At { hour: 5, minute: 30, weekday: None }).period(),
                   Some(Duration::from_secs(86_400)));
        assert_eq!(job(Schedule::At { hour: 6, minute: 10, weekday: Some(1) }).period(),
                   Some(Duration::from_secs(7 * 86_400)));
        assert_eq!(job(Schedule::None).period(), None);
    }

    #[test]
    fn short_name_drops_the_reverse_dns_prefix() {
        let mut j = job(Schedule::None);
        j.label = "com.hypermnesia.consolidate".into();
        assert_eq!(j.short(), "consolidate");
    }

    /// Quit has to stick. `KeepAlive` set unconditionally relaunches the tray about ten seconds
    /// after the person chooses Quit, which makes the menu item a lie. Conditional on a failed
    /// exit keeps the intent -- restart a tray that crashed.
    #[test]
    fn autostart_restarts_a_crash_but_not_a_deliberate_quit() {
        let plist = tray_plist("com.hypermnesia.tray", "/opt/hm/hypermnesia", "/tmp/tray.log");
        let keep = plist.lines().find(|l| l.contains("KeepAlive")).expect("KeepAlive");
        assert!(keep.contains("SuccessfulExit"), "KeepAlive must be conditional: {keep}");
        assert!(!keep.contains("<true/>"), "unconditional KeepAlive defeats Quit: {keep}");
        assert!(plist.contains("<key>RunAtLoad</key><true/>"), "it still has to start at login");
    }
}
