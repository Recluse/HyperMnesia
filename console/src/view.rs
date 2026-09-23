//! How the tray renders what it knows, shared by every platform's menu shell.
//!
//! Nothing here calls a GUI library or a platform's service manager. It turns a `Stats` and a
//! `[Job]` into the same lines and the same single yes/no "does this need a mark" verdict on
//! macOS and on Linux, so the two menu shells cannot drift into disagreeing about what a person
//! should be shown. `warned()` is deliberately *derived* from the lines this module renders,
//! never recomputed beside them: a title mark that disagrees with the menu under it is the same
//! quiet lie as a stale reading drawn as a fresh one.

use std::time::{Duration, SystemTime};

use crate::jobs::{self, Job};
use crate::Stats;

/// What is visible without opening the menu. What is visible without opening the menu. A mark
/// matters more than a number here: the title/icon is where a problem is NOTICED, not where a
/// report is read.
///
/// Derived from the same lines `summary` and `job_line` produce, not from a second list of
/// conditions kept in step by hand -- recomputing separately is what once let a warning line sit
/// in the menu under a calm mark.
pub fn warned(stats: &Stats, jobs: &[Job], jobs_error: &Option<String>) -> bool {
    summary(stats).iter().any(|l| l.starts_with('!'))
        || jobs_error.is_some()
        || jobs.iter().any(Job::broken)
        || jobs.iter().any(Job::overdue)
}

/// The lines describing the store's numbers, for the menu body.
pub fn summary(s: &Stats) -> Vec<String> {
    let (docs, chunks, embedded) = s.corpus.iter()
        .filter(|r| !r.is_memory_page())
        .fold((0, 0, 0), |(d, c, e), r| (d + r.docs, c + r.chunks, e + r.embedded));
    let mut out = vec![
        format!("Memory: {} active of {}", s.memories_active, s.memories_total),
        format!("Knowledge pages: {}", s.pages),
        format!("Corpus: {docs} docs, {chunks} chunks"),
    ];
    if embedded < chunks {
        out.push(format!("! not embedded: {}", chunks - embedded));
    }
    if s.embedding_models.len() > 1 {
        // Two models in one store means part of the corpus cannot be reached by meaning at all.
        out.push(format!("! embedding models: {}", s.embedding_models.len()));
    }
    // Marked when there is something in it, so the title can be derived from these lines rather
    // than from a second list of conditions kept in step by hand.
    out.push(if s.review_pending > 0 {
        format!("! Review queue: {} (oldest {} days)", s.review_pending, s.review_oldest_days)
    } else {
        "Review queue: 0".to_string()
    });
    out.push(format!("Stale: {}", s.stale));
    out.push(format!("Database: {}", s.db_size));
    out
}

/// How old something is, by the wall clock. `Instant` would stop while the machine sleeps.
pub fn age_of(t: SystemTime) -> Duration {
    SystemTime::now().duration_since(t).unwrap_or_default()
}

pub fn ago(d: Duration) -> String {
    let s = d.as_secs();
    if s < 90 { format!("{s}s") }
    else if s < 5400 { format!("{}m", s / 60) }
    else if s < 172_800 { format!("{}h", s / 3600) }
    else { format!("{}d", s / 86_400) }
}

/// One job's line in the "Run now" / status listing.
pub fn job_line(j: &Job) -> String {
    if let Some(why) = &j.fault {
        // The backend refused this unit too, so the job is not running. It used to be missing
        // from the menu entirely.
        let first = why.lines().next().unwrap_or(why);
        return format!("{}  (! unreadable {}: {first})", j.short(), jobs::UNIT_NOUN);
    }
    let state = if j.running() {
        "running".to_string()
    } else if j.overdue() {
        "! overdue".to_string()
    } else if j.never_ran() {
        "never run yet".to_string()
    } else if let Some(_) = j.freshness_unknown() {
        // Neither fresh nor stale: the log that would date it is gone or undatable. Drawing
        // that as health is how a job that quietly stopped stays invisible.
        "? cannot be dated".to_string()
    } else if j.last_exit.is_none() {
        "not loaded".to_string()
    } else if j.runs == Some(0) {
        // The service manager prints exit 0 both for "finished successfully" and for "never
        // finished at all". With no run since this login there is nothing to call successful.
        "no run since login".to_string()
    } else {
        // The exit code belongs here. Without it a job that fails on every single run reads
        // exactly like one that works: same schedule, same fresh log timestamp.
        let when = match j.since_last_output() {
            Some(d) => format!("{} ago", ago(d)),
            None => "—".to_string(),
        };
        match j.last_exit {
            Some(0) => when,
            Some(c) => format!("! exit {c}, {when}"),
            None => format!("{when}, not loaded"),
        }
    };
    format!("{}  ({}, {})", j.short(), j.schedule.human(), state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jobs::{LogState, Schedule};
    use std::path::PathBuf;

    fn job(schedule: Schedule) -> Job {
        Job {
            label: "com.hypermnesia.extract".into(), source: PathBuf::new(), schedule,
            program: vec![], log: Some(PathBuf::from("/nope")), last_exit: Some(0), pid: None,
            log_state: LogState::Written(SystemTime::now() - Duration::from_secs(300)),
            runs: Some(9), trigger: jobs::TriggerState::NotTracked,
            installed: Some(SystemTime::now() - Duration::from_secs(90_000)), fault: None,
            armed: None,
        }
    }

    /// A job that fails on every run used to read exactly like one that works: same schedule,
    /// same fresh log timestamp, no code anywhere in the menu.
    #[test]
    fn a_failing_job_says_so_in_its_own_line() {
        let mut j = job(Schedule::Every(14_400));
        assert_eq!(job_line(&j), "extract  (every 4 h, 5m ago)");

        j.last_exit = Some(2);
        let line = job_line(&j);
        assert!(line.contains("exit 2"), "the exit code has to be on the line: {line}");
        assert!(line.starts_with("extract  (every 4 h, !"), "and marked: {line}");
    }

    /// A unit the backend also refused is one that is not running. It used to be absent from the
    /// menu altogether.
    #[test]
    fn an_unreadable_unit_is_a_visible_line() {
        let mut j = job(Schedule::None);
        j.fault = Some("plutil: unexpected character\nsecond line".into());
        let line = job_line(&j);
        assert!(line.contains(&format!("unreadable {}", jobs::UNIT_NOUN)), "{line}");
        assert!(!line.contains("second line"), "one line only, it is a menu: {line}");
    }

    #[test]
    fn a_job_never_run_is_not_called_fresh() {
        let mut j = job(Schedule::Calendar(jobs::Cal {
            hour: Some(6), minute: Some(10), day: None, weekday: Some(1), month: None }));
        j.runs = Some(0);
        j.log_state = LogState::Missing;
        assert!(job_line(&j).contains("never run yet"), "{}", job_line(&j));
    }
}
