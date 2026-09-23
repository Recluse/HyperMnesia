//! The pipeline's scheduled jobs: what is configured, when it last worked, and how to run it now.
//!
//! What is asked here -- a schedule, a last exit code, how long since it last did anything, a way
//! to run it now -- is the same question on every platform. The answer comes from a different
//! place on each: launchd's plists and `launchctl` on macOS, systemd user units and `systemctl
//! --user` on Linux. This module holds the question -- `Cal`, `Schedule`, `LogState`, `Job` and
//! the freshness logic in `impl Job` -- once, and knows nothing about either backend. `launchd`
//! and `systemd` hold the answers, selected by `cfg(target_os)`, and nothing above this module
//! (the tray, `hypermnesia-jobs`) has to know which one is in effect.
//!
//! The important part about "when it last worked" is not the same fact on both platforms. launchd
//! does not keep it: it knows only the exit code of the LAST run, so the time has to come from the
//! mtime of the job's own log file, and a missing log means "it has not run once since the path
//! was set", not "it is working quietly". systemd keeps the answer directly -- a timer's own
//! `LastTriggerUSec` -- which is why `Job` carries both `log_state` and `trigger`: the second is
//! `NotTracked` on macOS and, wherever it is tracked, it is trusted over the log every time.

use std::path::PathBuf;
use std::time::{Duration, SystemTime};

#[cfg(target_os = "macos")]
mod launchd;
#[cfg(target_os = "macos")]
pub use launchd::{autostart_fault, install_self, list, run_now, set_enabled, set_schedule,
                   uninstall_self, SUPPORTS_ENABLE};

#[cfg(target_os = "linux")]
mod systemd;
#[cfg(target_os = "linux")]
pub use systemd::{autostart_fault, install_self, list, run_now, set_enabled, set_schedule,
                   uninstall_self, SUPPORTS_ENABLE};

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
mod unsupported;
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub use unsupported::{autostart_fault, install_self, list, run_now, set_enabled, set_schedule,
                       uninstall_self, SUPPORTS_ENABLE};

/// Reverse-DNS prefix of the launchd labels this console manages, on macOS.
#[cfg(target_os = "macos")]
pub const DEFAULT_JOB_PREFIX: &str = "com.hypermnesia";
/// Prefix of the systemd unit names this console manages, on Linux: `hypermnesia-extract.timer`,
/// not a reverse-DNS label -- that convention is launchd's, not systemd's.
#[cfg(target_os = "linux")]
pub const DEFAULT_JOB_PREFIX: &str = "hypermnesia-";
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub const DEFAULT_JOB_PREFIX: &str = "hypermnesia-";

/// Label of the tray's own autostart job, passed to `install_self`.
#[cfg(target_os = "macos")]
pub const DEFAULT_TRAY_LABEL: &str = "com.hypermnesia.tray";
#[cfg(not(target_os = "macos"))]
pub const DEFAULT_TRAY_LABEL: &str = "hypermnesia-tray";

/// What to call the file a job is defined in, in a sentence a person reads. A plist on macOS, a
/// unit file on Linux -- getting this wrong is exactly the kind of small, confident lie the rest
/// of this console refuses to tell.
#[cfg(target_os = "macos")]
pub const UNIT_NOUN: &str = "plist";
#[cfg(target_os = "linux")]
pub const UNIT_NOUN: &str = "unit";
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub const UNIT_NOUN: &str = "job file";

/// The service manager in charge, named the way a person would say it: "launchd would not load
/// it", not "the service manager would not load it".
#[cfg(target_os = "macos")]
pub const BACKEND_NAME: &str = "launchd";
#[cfg(target_os = "linux")]
pub const BACKEND_NAME: &str = "systemd";
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub const BACKEND_NAME: &str = "the service manager";

/// Where job definitions live, for messages that have to name a place. `~`, not `$HOME`
/// expanded: this is prose for a person, not a path handed to a shell.
#[cfg(target_os = "macos")]
pub const UNITS_LOCATION: &str = "~/Library/LaunchAgents";
#[cfg(target_os = "linux")]
pub const UNITS_LOCATION: &str = "~/.config/systemd/user";
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub const UNITS_LOCATION: &str = "wherever this platform's scheduled jobs live";

/// The label prefix to look for: environment, then the config file, then the default -- the same
/// order as every other setting here.
///
/// The config file matters more than it looks. The tray is started by the platform's own service
/// manager, which gives it a minimal environment and none of a login shell's variables, so an
/// installation whose jobs are named differently could set HM_JOB_PREFIX in its shell forever and
/// the tray would still show an empty job list -- while every command run by hand showed the
/// right one.
pub fn job_prefix() -> String {
    if let Some(v) = std::env::var("HM_JOB_PREFIX").ok().filter(|s| !s.is_empty()) {
        return v;
    }
    // Filtered the same way as the environment: an empty HM_JOB_PREFIX in the config makes
    // `starts_with` match every job the person owns, and this module edits and restarts what it
    // lists.
    if let Some(v) = crate::config().get("HM_JOB_PREFIX").filter(|s| !s.is_empty()) {
        return v.clone();
    }
    DEFAULT_JOB_PREFIX.to_string()
}

/// One calendar slot, as launchd stores it -- and as a systemd `OnCalendar=` line is parsed into,
/// since it expresses the same wildcard-by-omission idea.
///
/// Every field is optional AND an omitted field is a WILDCARD, which is the whole reason this is
/// a struct rather than an hour and a minute. `{"Minute": 15}` means every hour at :15, not
/// "daily 00:15"; `{"Day": 1, "Hour": 3}` means the first of each month, not "daily 03:00".
/// Reading the omissions as zeroes printed a plausible wrong time -- and, worse, a wrong period:
/// a healthy monthly job was measured against a day and a half and reported overdue for
/// twenty-nine days out of thirty.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Cal {
    pub minute: Option<u32>,
    pub hour: Option<u32>,
    /// Day of the month, 1-31.
    pub day: Option<u32>,
    /// 0 = Sunday.
    pub weekday: Option<u32>,
    pub month: Option<u32>,
}

impl Cal {
    pub fn at(hour: u32, minute: u32, weekday: Option<u32>) -> Self {
        Self { hour: Some(hour), minute: Some(minute), weekday, ..Self::default() }
    }

    /// How long between two firings, taken from the COARSEST field that is pinned: everything
    /// finer than it repeats inside that cycle, everything coarser is a wildcard.
    pub fn period(&self) -> Duration {
        let day = 86_400;
        Duration::from_secs(if self.month.is_some() {
            365 * day
        } else if self.day.is_some() {
            31 * day
        } else if self.weekday.is_some() {
            7 * day
        } else if self.hour.is_some() {
            day
        } else if self.minute.is_some() {
            3_600
        } else {
            // Nothing pinned at all: fires every minute.
            60
        })
    }

    pub fn human(&self) -> String {
        const DAYS: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
        const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
        // "xx" where the schedule would repeat: an hour with no minute fires every minute of it.
        let time = match (self.hour, self.minute) {
            (Some(h), Some(m)) => format!("{h:02}:{m:02}"),
            (Some(h), None) => format!("{h:02}:xx"),
            (None, Some(m)) => format!(":{m:02}"),
            (None, None) => String::new(),
        };
        let month = self.month
            .map(|m| format!("{} ", MONTHS[((m.max(1) - 1) as usize) % 12]))
            .unwrap_or_default();
        match (self.month, self.day, self.weekday, self.hour, self.minute) {
            (_, Some(d), _, _, _) => format!("{} {month}day {d} {time}",
                if self.month.is_some() { "yearly" } else { "monthly" }),
            (_, None, Some(w), _, _) => format!("weekly {} {time}", DAYS[(w as usize) % 7]),
            (_, None, None, Some(_), _) => format!("daily {time}"),
            (_, None, None, None, Some(_)) => format!("hourly at {time}"),
            _ => "every minute".into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Schedule {
    /// Every N seconds.
    Every(u64),
    /// One calendar slot.
    Calendar(Cal),
    /// Several calendar slots: launchd accepts an array of dicts, and systemd accepts several
    /// `OnCalendar=` lines in one unit. All of them are kept -- the period has to come from the
    /// widest of them, and the first slot is not the schedule.
    Several(Vec<Cal>),
    /// No schedule: a service that is simply kept running.
    None,
}

impl Schedule {
    /// A one-slot calendar schedule, which is what the console can set.
    pub fn at(hour: u32, minute: u32, weekday: Option<u32>) -> Self {
        Schedule::Calendar(Cal::at(hour, minute, weekday))
    }

    pub fn human(&self) -> String {
        match self {
            Schedule::Every(s) if *s % 3600 == 0 => format!("every {} h", s / 3600),
            Schedule::Every(s) if *s % 60 == 0 => format!("every {} min", s / 60),
            Schedule::Every(s) => format!("every {s} s"),
            Schedule::Calendar(c) => c.human(),
            Schedule::Several(c) if c.len() == 1 => c[0].human(),
            Schedule::Several(c) => format!("{} slots ({})", c.len(), c[0].human()),
            Schedule::None => "on demand".into(),
        }
    }
}

/// What is known about a job's log.
#[derive(Debug, Clone, PartialEq)]
pub enum LogState {
    /// The unit names no log: nothing can be dated by it, in either direction.
    NotConfigured,
    /// A log is configured and it is not there -- the job has not written anything since the
    /// path was set.
    Missing,
    /// The log was last written at this time.
    Written(SystemTime),
    /// A log is configured, exists, and cannot be dated: unreadable, or stamped in the future
    /// (a clock that moved). Not evidence of health and not evidence of failure -- and saying so
    /// beats both of the alternatives.
    Unknown(String),
}

impl LogState {
    pub fn at(&self) -> Option<SystemTime> {
        match self {
            LogState::Written(t) => Some(*t),
            _ => None,
        }
    }
}

/// What the backend's own record says about when the job last fired, where it keeps one at all.
///
/// A plain `Option<SystemTime>` cannot say this: it collapses "this backend keeps no such record"
/// (launchd -- the log's mtime is the only evidence there is) and "this backend keeps one, and it
/// says never" (systemd, a timer whose `LastTriggerUSec` is empty) into the same `None`, and a
/// job read through the second case would then fall through to launchd's log/run-count logic,
/// find nothing there either, and read as calm by accident -- the same quiet lie `LogState`
/// exists to prevent for the log itself.
#[derive(Debug, Clone, PartialEq)]
pub enum TriggerState {
    /// This backend keeps no direct record of when a job last fired.
    NotTracked,
    /// The backend tracks it, and it has never fired.
    Never,
    /// The backend tracks it, and it last fired at this time.
    At(SystemTime),
}

#[derive(Debug, Clone)]
pub struct Job {
    pub label: String,
    /// The file the job is defined in: a `.plist` on macOS, a `.service`/`.timer` on Linux. Named
    /// `source` rather than `plist` so that a systemd path in this field is not a small lie about
    /// which platform produced it.
    pub source: PathBuf,
    pub schedule: Schedule,
    pub program: Vec<String>,
    pub log: Option<PathBuf>,
    /// Exit code of the last run, as the service manager remembers it. `None` -- the job is not
    /// loaded.
    pub last_exit: Option<i32>,
    /// When the unit last changed. Needed so as not to raise a false alarm: a job installed
    /// later than its own last slot simply has not come due yet.
    pub installed: Option<SystemTime>,
    /// How many times the service manager has started it since this login/boot. Zero -- not once.
    ///
    /// Without this number the status column lies: launchd prints `0` in the status column both
    /// for "finished successfully" and for "never finished at all", so a job that has never
    /// started looks like one that ran without errors. `None` when the backend keeps no such
    /// counter at all (systemd does not).
    pub runs: Option<u64>,
    /// PID, if the job is running right now.
    pub pid: Option<u32>,
    /// What the log says about when the job last worked. The only sign that survives a reboot on
    /// launchd, and the reason it is three states rather than a timestamp: "no log configured",
    /// "a log is configured and is not there" and "unreadable / dated in the future" are three
    /// different pieces of knowledge, and collapsing them into None made a rotated-away log read
    /// as a healthy job forever.
    pub log_state: LogState,
    /// When the backend's own record says the job last fired, where it keeps one at all.
    /// systemd's `LastTriggerUSec` answers this directly and survives a reboot; launchd keeps
    /// nothing of the kind, so this is always `NotTracked` there and the log's mtime is what
    /// dates a run instead. Preferred over `log_state` wherever both could answer the same
    /// question, because it is the more direct evidence.
    pub trigger: TriggerState,
    /// Why this job could not be read. A unit that the parser or the service manager refuses is
    /// one that is not running, and that is precisely the case the console used to have nothing
    /// to say about, because the row was dropped from the list.
    pub fault: Option<String>,
    /// Whether the schedule is armed, where the backend can say. `Some(true)` on systemd is a
    /// timer that is `UnitFileState=enabled` and `ActiveState=active`; `Some(false)` is loaded but
    /// not armed -- exactly what `enablement_fault` already flags as a fault. `None` on launchd,
    /// which has no separate "loaded but not armed" state to report: a plist launchd has loaded is
    /// armed, full stop, so there is nothing here to switch.
    pub armed: Option<bool>,
}

impl Job {
    /// Short name without the reverse-DNS prefix or the `hypermnesia-` prefix: on the console's
    /// screen `extract`, not `com.hypermnesia.extract` or `hypermnesia-extract`.
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
            Schedule::Calendar(c) => Some(c.period()),
            // The widest of the slots, not the narrowest: two weekly slots are still a week
            // apart at worst, and measuring them against a day would cry wolf for six days of it.
            Schedule::Several(cals) => cals.iter().map(Cal::period).max(),
            Schedule::None => None,
        }
    }

    /// Something is wrong with the job itself, before any question of when it last ran.
    pub fn broken(&self) -> bool {
        self.fault.is_some()
    }

    /// Wrong enough that a write action should refuse to touch the unit -- unlike
    /// `armed == Some(false)`, which means the unit loaded and parsed just fine and is simply
    /// disabled, a state `set_enabled`/`set_schedule` exist to fix rather than a reason to
    /// refuse them. `armed` is `Some(_)` on systemd whenever the unit could be loaded at all, and
    /// `None` only when it could not -- the same distinction `read_unit`/`broken_job` already
    /// draw there. On launchd, where enablement is not a separate state, `armed` is always
    /// `None`, so this collapses back to plain `broken()` -- exactly what that backend's own
    /// `fault` has always meant.
    pub fn unwritable(&self) -> bool {
        self.fault.is_some() && self.armed.is_none()
    }

    /// The service manager has never started it.
    ///
    /// `runs` alone does not answer this on launchd: it is per-bootstrap state, reset to zero at
    /// every login while the plist's mtime does not move. Trusting it turned every healthy job
    /// into "never ran / overdue" after a reboot. `trigger`, where the backend tracks one, is
    /// authoritative and settles the question directly in either direction. Otherwise the log --
    /// the only evidence that survives a restart on launchd -- has to agree, and it only counts
    /// if it was written AFTER the job was installed: a log left behind by a previous
    /// installation says nothing about this one.
    pub fn never_ran(&self) -> bool {
        if self.schedule == Schedule::None || self.broken() {
            return false;                     // a service without a schedule -- not a complaint
        }
        match self.trigger {
            TriggerState::At(_) => return false,   // the backend's own record says it fired
            TriggerState::Never => return true,    // ...and here, that it never has
            TriggerState::NotTracked => {}
        }
        if self.log_after_install() {
            return false;                     // it has written something since: it ran
        }
        match self.runs {
            Some(n) => n == 0,
            // The counter is unavailable: fall back to the sign "a log is configured but not
            // there" -- weaker, but better than silence.
            None => self.log_state == LogState::Missing,
        }
    }

    /// Was the log written after this version of the job was installed? A log from a previous
    /// install is not evidence about this one -- in either direction.
    fn log_after_install(&self) -> bool {
        match (self.log_state.at(), self.installed) {
            (Some(out), Some(inst)) => out >= inst,
            (Some(_), None) => true,
            _ => false,
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
            // The only date available is the unit's own.
            return self.since_installed().map(|age| age > slack).unwrap_or(true);
        }
        // It ran at some point. Then the complaint is staleness, dated by the best evidence
        // available -- and never from before the job was installed, or a fresh install inheriting
        // an old log would be called overdue the moment it was made.
        match (self.since_last_output(), self.since_installed()) {
            (Some(age), Some(since_install)) => age.min(since_install) > slack,
            (Some(age), None) => age > slack,
            (None, _) => false,
        }
    }

    /// Can this job's freshness be judged at all? A configured log that is gone or undatable
    /// leaves the question open -- and an open question must not be drawn as health.
    ///
    /// `trigger`, where the backend tracks one, answers the question directly either way -- there
    /// is nothing left unknown, whether it says "fired" or "never has".
    pub fn freshness_unknown(&self) -> Option<String> {
        if self.broken() || self.schedule == Schedule::None
            || matches!(self.trigger, TriggerState::At(_) | TriggerState::Never) {
            return None;
        }
        match &self.log_state {
            LogState::Unknown(why) => Some(why.clone()),
            // Ran at least once since this boot, yet the log it is supposed to write is not
            // there: it was rotated away, or the job is writing nowhere.
            LogState::Missing if self.runs.unwrap_or(0) > 0 =>
                Some("the configured log is not there, so nothing can date its last run".into()),
            LogState::NotConfigured if self.runs.unwrap_or(0) > 0 =>
                Some("no log is configured, so nothing survives a reboot to date its runs".into()),
            _ => None,
        }
    }

    /// How long ago it was installed (by the unit's modification time).
    pub fn since_installed(&self) -> Option<Duration> {
        self.installed.and_then(|t| SystemTime::now().duration_since(t).ok())
    }

    /// How long since the job last did something, from the best evidence available: the backend's
    /// own trigger record if it keeps one, the log's mtime otherwise.
    pub fn since_last_output(&self) -> Option<Duration> {
        let at = match self.trigger {
            TriggerState::At(t) => Some(t),
            TriggerState::Never | TriggerState::NotTracked => self.log_state.at(),
        }?;
        SystemTime::now().duration_since(at).ok()
    }
}

/// What the log can tell us, kept as the three different things it can be. Shared by both
/// backends: a log path and a filesystem are the same kind of fact on either platform.
pub(crate) fn log_state_of(log: Option<&PathBuf>) -> LogState {
    let Some(p) = log else { return LogState::NotConfigured };
    let md = match std::fs::metadata(p) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return LogState::Missing,
        Err(e) => return LogState::Unknown(format!("{}: {e}", p.display())),
    };
    match md.modified() {
        Ok(t) if t > SystemTime::now() + Duration::from_secs(60) =>
            LogState::Unknown(format!("{} is stamped in the future -- a clock moved",
                                      p.display())),
        Ok(t) => LogState::Written(t),
        Err(e) => LogState::Unknown(format!("{}: {e}", p.display())),
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

    fn job(schedule: Schedule) -> Job {
        Job {
            label: "x".into(), source: PathBuf::new(), schedule, program: vec![], log: None,
            last_exit: None, pid: None, log_state: LogState::NotConfigured, runs: None,
            installed: None, trigger: TriggerState::NotTracked, fault: None, armed: None,
        }
    }

    /// A job with a log that was written `ago` ago, installed `installed_ago` ago.
    fn with_log(schedule: Schedule, runs: Option<u64>, written: Option<Duration>,
                installed_ago: Duration) -> Job {
        let mut j = job(schedule);
        j.log = Some(PathBuf::from("/nope"));
        j.runs = runs;
        j.last_exit = Some(0);
        j.installed = Some(SystemTime::now() - installed_ago);
        j.log_state = match written {
            Some(d) => LogState::Written(SystemTime::now() - d),
            None => LogState::Missing,
        };
        j
    }

    /// A job whose backend dates runs directly (systemd's `LastTriggerUSec`), with no log at all.
    fn with_trigger(schedule: Schedule, ago: Option<Duration>) -> Job {
        let mut j = job(schedule);
        j.last_exit = Some(0);
        j.installed = Some(SystemTime::now() - Duration::from_secs(30 * 86_400));
        j.trigger = match ago {
            Some(d) => TriggerState::At(SystemTime::now() - d),
            None => TriggerState::Never,
        };
        j
    }

    #[test]
    fn schedules_render_in_words() {
        assert_eq!(Schedule::Every(14400).human(), "every 4 h");
        assert_eq!(Schedule::Every(900).human(), "every 15 min");
        // Something not a whole number of minutes stays in seconds instead of being rounded into
        // a lie.
        assert_eq!(Schedule::Every(90).human(), "every 90 s");
        assert_eq!(Schedule::at(5, 30, None).human(), "daily 05:30");
        assert_eq!(Schedule::at(6, 10, Some(1)).human(), "weekly Mon 06:10");
        assert_eq!(Schedule::None.human(), "on demand");
    }

    /// A job the service manager has never started must be called exactly that, not
    /// "successful".
    #[test]
    fn a_scheduled_job_that_never_ran_is_flagged() {
        let mut j = with_log(Schedule::at(6, 10, Some(1)), Some(0), None,
                             Duration::from_secs(30 * 86_400));
        assert!(j.never_ran());

        j.runs = Some(44);                    // started since the last login
        assert!(!j.never_ran());

        j.runs = Some(0);
        j.schedule = Schedule::None;          // a service without a schedule -- not a complaint
        assert!(!j.never_ran());

        // The counter is unavailable (the job is not loaded): fall back to "a log is configured
        // but not there".
        j.schedule = Schedule::Every(3600);
        j.runs = None;
        assert!(j.never_ran());
        j.log_state = LogState::Written(SystemTime::now());
        assert!(!j.never_ran());
    }

    /// The run counter is per-bootstrap on launchd: it resets at every login while the unit's
    /// mtime stays where it was. Keying "never ran" on it alone turned every healthy job into a
    /// false alarm after a reboot -- which is how a warning stops being read.
    #[test]
    fn a_reboot_does_not_turn_a_working_job_into_a_false_alarm() {
        let j = with_log(Schedule::Every(14_400), Some(0), Some(Duration::from_secs(600)),
                         Duration::from_secs(30 * 86_400));
        assert!(!j.never_ran(), "the log says it ran ten minutes ago");
        assert!(!j.overdue());
    }

    /// The other direction of the same mistake: a job that ran once and then stopped used to be
    /// exempt forever, because a positive counter ended the question.
    #[test]
    fn a_job_that_ran_once_and_then_stalled_is_still_overdue() {
        let mut j = with_log(Schedule::Every(3600), Some(12), Some(Duration::from_secs(600)),
                             Duration::from_secs(30 * 86_400));
        assert!(!j.overdue(), "ten minutes into an hourly job is fine");

        j.log_state = LogState::Written(SystemTime::now() - Duration::from_secs(6 * 3600));
        assert!(j.overdue(), "six hours into an hourly job is not");
    }

    /// A log left behind by a PREVIOUS installation is not evidence about this one. Without
    /// that rule a job installed a minute ago, pointing at a month-old log, was called overdue
    /// on the spot.
    #[test]
    fn an_inherited_log_does_not_condemn_a_fresh_install() {
        let j = with_log(Schedule::at(6, 10, Some(1)), Some(0),
                         Some(Duration::from_secs(30 * 86_400)),  // log: a month old
                         Duration::from_secs(60));                // installed a minute ago
        assert!(!j.overdue(), "it has not had a chance to run yet");
        assert!(j.never_ran(), "and the old log is not evidence that it did");
    }

    /// Freshness that cannot be judged must say so. A rotated-away log used to leave a stalled
    /// job reading "last run ok" with an empty timestamp, for ever.
    #[test]
    fn a_log_that_cannot_date_a_run_is_an_open_question_not_health() {
        let j = with_log(Schedule::Every(3600), Some(9), None, Duration::from_secs(86_400));
        assert!(j.freshness_unknown().is_some(), "the log is configured and gone");
        assert!(!j.overdue(), "and that is not the same as proof it stopped");

        let mut future = with_log(Schedule::Every(3600), Some(9), Some(Duration::from_secs(60)),
                                  Duration::from_secs(86_400));
        future.log_state = LogState::Unknown("stamped in the future".into());
        assert!(future.freshness_unknown().is_some());

        let healthy = with_log(Schedule::Every(3600), Some(9), Some(Duration::from_secs(60)),
                               Duration::from_secs(86_400));
        assert!(healthy.freshness_unknown().is_none());
    }

    #[test]
    fn period_matches_the_schedule() {
        assert_eq!(job(Schedule::Every(14400)).period(), Some(Duration::from_secs(14400)));
        assert_eq!(job(Schedule::at(5, 30, None)).period(), Some(Duration::from_secs(86_400)));
        assert_eq!(job(Schedule::at(6, 10, Some(1))).period(),
                   Some(Duration::from_secs(7 * 86_400)));
        assert_eq!(job(Schedule::None).period(), None);
    }

    #[test]
    fn short_name_drops_the_reverse_dns_prefix() {
        let mut j = job(Schedule::None);
        j.label = "com.hypermnesia.consolidate".into();
        assert_eq!(j.short(), "consolidate");
    }

    #[test]
    fn short_name_drops_the_hyphen_prefix() {
        let mut j = job(Schedule::None);
        j.label = "hypermnesia-consolidate".into();
        assert_eq!(j.short(), "consolidate");
    }

    /// A job whose backend answers "when did it last fire" directly is neither never-ran, nor
    /// overdue, nor unknown -- the question is settled, and drawing it as open or as healthy by
    /// accident would both be wrong.
    #[test]
    fn a_backend_supplied_trigger_answers_freshness_directly() {
        let fresh = with_trigger(Schedule::Every(3600), Some(Duration::from_secs(300)));
        assert!(!fresh.never_ran());
        assert!(!fresh.overdue());
        assert!(fresh.freshness_unknown().is_none());
        // Not an exact `assert_eq!`: two `SystemTime::now()` calls a few instructions apart are a
        // handful of microseconds apart too, and the age has to allow for that.
        let age = fresh.since_last_output().expect("a trigger time was set");
        assert!(age >= Duration::from_secs(300) && age < Duration::from_secs(301),
                "expected ~300s, got {age:?}");

        let stale = with_trigger(Schedule::Every(3600), Some(Duration::from_secs(6 * 3600)));
        assert!(!stale.never_ran(), "it did fire once, just long ago");
        assert!(stale.overdue());
        assert!(stale.freshness_unknown().is_none(), "the timestamp answers the question");

        // The backend tracks triggers AND says it has never fired -- `TriggerState::Never`, not
        // an absence of evidence. Must be a settled "never ran", not a fall-through to the
        // log/run-count logic that has nothing to say here (no log, no run counter) and would
        // otherwise read this job as calm by accident.
        let never = with_trigger(Schedule::Every(3600), None);
        assert!(never.never_ran());
        assert!(never.freshness_unknown().is_none(), "never-fired is a settled answer too");
    }
}
