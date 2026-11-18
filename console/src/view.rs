//! Shared tray presentation: typed reasons drive both the menu and the icon.
use std::time::{Duration, SystemTime};
use crate::jobs::Job;
use crate::Stats;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    #[default]
    Healthy,
    Attention,
    Error,
}

#[derive(Clone, Debug)]
pub struct StatusLine {
    pub severity: Severity,
    pub text: String,
}

impl StatusLine {
    pub fn new(severity: Severity, text: impl Into<String>) -> Self {
        Self { severity, text: text.into() }
    }

    pub fn marked(&self) -> String {
        let prefix = match self.severity {
            Severity::Healthy => "", Severity::Attention => "~ ", Severity::Error => "! ",
        };
        format!("{prefix}{}", self.text)
    }
}

#[derive(Default)]
pub struct Health {
    pub severity: Severity,
    pub reasons: Vec<StatusLine>,
}

impl Health {
    pub fn push(&mut self, line: StatusLine) {
        self.severity = self.severity.max(line.severity);
        if line.severity != Severity::Healthy { self.reasons.push(line); }
    }

    pub fn title(&self) -> &'static str {
        match self.severity {
            Severity::Healthy => "Healthy",
            Severity::Attention => "Needs attention",
            Severity::Error => "Errors detected",
        }
    }
}

/// Job errors remain visible before the first successful database reading.
pub fn health(stats: Option<&Stats>, jobs: &[Job], jobs_error: Option<&str>) -> Health {
    let mut out = Health::default();
    if let Some(s) = stats {
        for line in summary_rows(s) { out.push(line); }
    } else {
        out.push(StatusLine::new(Severity::Attention, "Waiting for the first store reading"));
    }
    if let Some(why) = jobs_error {
        out.push(StatusLine::new(Severity::Error, format!("Jobs unreadable: {why}")));
    }
    for j in jobs {
        let status = job_status(j);
        out.push(StatusLine::new(status.severity, format!("{}: {}", j.short(), status.text)));
    }
    out
}

pub fn summary(s: &Stats) -> Vec<String> {
    summary_rows(s).iter().map(StatusLine::marked).collect()
}

fn summary_rows(s: &Stats) -> Vec<StatusLine> {
    use Severity::{Attention, Error, Healthy};
    // Wider sums prevent multiple valid i64 database counts from wrapping at the UI boundary.
    let totals = |pages| s.corpus.iter().filter(|r| r.is_memory_page() == pages)
        .fold((0i128, 0i128, 0i128), |(d, c, e), r|
              (d + i128::from(r.docs), c + i128::from(r.chunks), e + i128::from(r.embedded)));
    let (docs, chunks, embedded) = totals(false);
    let mut out = vec![
        StatusLine::new(Healthy, format!("Memory: {} active of {}", s.memories_active, s.memories_total)),
        StatusLine::new(Healthy, format!("Knowledge pages: {}", s.pages)),
        StatusLine::new(Healthy, format!("Corpus: {docs} docs, {chunks} chunks")),
    ];
    if embedded < chunks {
        out.push(StatusLine::new(Attention, format!("Not embedded: {}", chunks - embedded)));
    }
    let (_, mchunks, membedded) = totals(true);
    if membedded < mchunks {
        out.push(StatusLine::new(Attention, format!("Pages not embedded: {}", mchunks - membedded)));
    }
    if s.embedding_models.len() > 1 {
        out.push(StatusLine::new(Error, format!("Embedding models: {}", s.embedding_models.len())));
    }
    if s.embedding_unset > 0 {
        out.push(StatusLine::new(Error, format!("Chunks with no model recorded: {}", s.embedding_unset)));
    }
    out.push(if s.review_pending > 0 {
        StatusLine::new(Attention, format!("Review queue: {} (oldest {} days)",
                                         s.review_pending, s.review_oldest_days))
    } else { StatusLine::new(Healthy, "Review queue: 0") });
    out.push(StatusLine::new(Healthy, format!("Old unused memories (180+ days): {}", s.stale)));
    out.push(StatusLine::new(Healthy, format!("Database: {}", s.db_size)));
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

/// The same classified state is used in the submenu and in the top-level reasons.
pub fn job_line(j: &Job) -> String {
    format!("{}  ({}, {})", j.short(), j.schedule.human(), job_status(j).marked())
}

fn job_status(j: &Job) -> StatusLine {
    use Severity::{Attention, Error, Healthy};
    if let Some(why) = &j.fault {
        return StatusLine::new(Error, why.lines().next().unwrap_or(why));
    }
    // A live rerun is not an overdue idle job, nor the previous run's exit code.
    if j.running() { return StatusLine::new(Healthy, "running"); }
    let when = j.since_last_output().map(|d| format!("{} ago", ago(d)))
        .unwrap_or_else(|| "last output unknown".into());
    // Timestamp uncertainty and overdue checks must not hide a failed run.
    if let Some(code) = j.last_exit.filter(|c| *c != 0) {
        // The freshness sweep uses 10 only after every source check completed.
        // Findings remain visible; a failed check uses 1 and stays red.
        if j.short() == "freshness" && code == 10 {
            if j.overdue() { return StatusLine::new(Error, "overdue"); }
            return StatusLine::new(Attention, format!("review findings, {when}"));
        }
        return StatusLine::new(Error, format!("exit {code}, {when}"));
    }
    if j.overdue() { return StatusLine::new(Error, "overdue"); }
    if j.never_ran() { return StatusLine::new(Healthy, "never run yet"); }
    if let Some(why) = j.freshness_unknown() {
        return StatusLine::new(Attention, format!("cannot be dated: {why}"));
    }
    if j.last_exit.is_none() {
        return StatusLine::new(Attention, "not loaded / no completion recorded");
    }
    if j.runs == Some(0) { return StatusLine::new(Healthy, "no run since login"); }
    StatusLine::new(Healthy, when)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jobs::{self, LogState, Schedule};
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


    #[test]
    fn completed_freshness_findings_differ_from_failed_checks() {
        let mut j = job(Schedule::Every(86_400));
        j.label = "ru.recluse.agentmem.freshness".into();
        j.last_exit = Some(10);
        assert_eq!(job_status(&j).severity, Severity::Attention);
        assert!(job_line(&j).contains("review findings"));
        j.last_exit = Some(1);
        assert_eq!(job_status(&j).severity, Severity::Error);
        j.last_exit = Some(10);
        j.label = "ru.recluse.agentmem.ingest".into();
        assert_eq!(job_status(&j).severity, Severity::Error);
    }

    #[test]
    fn review_alone_is_amber_and_errors_take_precedence() {
        let mut s = Stats::default();
        s.review_pending = 2;
        s.review_oldest_days = 11;
        let h = health(Some(&s), &[], None);
        assert_eq!(h.severity, Severity::Attention);
        assert_eq!(h.reasons.len(), 1);
        assert!(h.reasons[0].text.contains("Review queue: 2"));
        let mut j = job(Schedule::Every(14_400));
        j.last_exit = Some(1);
        let h = health(Some(&s), &[j], None);
        assert_eq!(h.severity, Severity::Error);
        assert!(h.reasons.iter().any(|r| r.text.contains("exit 1")));
        assert!(h.reasons.iter().any(|r| r.text.contains("Review queue")));
        s.review_pending = 0;
        assert_eq!(health(Some(&s), &[], None).severity, Severity::Healthy);
    }

    #[test]
    fn job_errors_do_not_require_a_store_snapshot() {
        let mut j = job(Schedule::Every(14_400));
        j.last_exit = Some(2);
        let h = health(None, &[j], Some("service manager unavailable"));
        assert_eq!(h.severity, Severity::Error);
        assert!(h.reasons.iter().any(|r| r.text.contains("exit 2")));
        assert!(h.reasons.iter().any(|r| r.text.contains("service manager")));
    }

    #[test]
    fn running_and_unknown_jobs_are_not_misclassified() {
        let mut j = job(Schedule::Every(60));
        j.last_exit = Some(1);
        j.pid = Some(123);
        assert_eq!(health(Some(&Stats::default()), &[j.clone()], None).severity, Severity::Healthy);
        assert!(job_line(&j).contains("running"));
        j.pid = None;
        j.log_state = LogState::Missing;
        assert_eq!(health(Some(&Stats::default()), &[j.clone()], None).severity, Severity::Error);
        j.last_exit = Some(0);
        assert_eq!(health(Some(&Stats::default()), &[j], None).severity, Severity::Attention);
    }

    #[test]
    fn embedding_backlog_and_invalid_model_metadata_have_distinct_severity() {
        let mut s = Stats::default();
        s.corpus.push(crate::RepoStats { repo: "project~mem".into(),
            docs: 1, chunks: 1, embedded: 0 });
        assert_eq!(health(Some(&s), &[], None).severity, Severity::Attention);
        s.embedding_unset = 1;
        assert_eq!(health(Some(&s), &[], None).severity, Severity::Error);
    }

    #[test]
    fn corpus_totals_do_not_wrap_at_the_i64_boundary() {
        let mut s = Stats::default();
        for repo in ["a", "b"] {
            s.corpus.push(crate::RepoStats { repo: repo.into(),
                docs: 1, chunks: i64::MAX, embedded: 0 });
        }
        assert!(summary(&s).iter().any(|l| l.contains("Not embedded: 18446744073709551614")));
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

    #[test]
    fn recent_failure_also_marks_the_icon() {
        let mut j = job(Schedule::Every(14_400));
        j.last_exit = Some(1);
        assert_eq!(health(Some(&Stats::default()), &[j], None).severity, Severity::Error);
    }

    /// A job the backend cannot run as written is one that is not running. It used to be absent
    /// from the menu altogether. The reason is printed as given: these faults are not all of one
    /// kind -- "the timer is loaded but not armed" reads perfectly well and is not unreadable --
    /// so the line must not prepend a word that contradicts half of them.
    #[test]
    fn a_faulted_job_shows_its_reason_verbatim() {
        let mut j = job(Schedule::None);
        j.fault = Some("plutil: unexpected character\nsecond line".into());
        let line = job_line(&j);
        assert!(line.contains("plutil: unexpected character"), "{line}");
        assert!(!line.contains("unreadable"), "the reason speaks for itself: {line}");
        assert!(!line.contains("second line"), "one line only, it is a menu: {line}");

        let mut armed = job(Schedule::None);
        armed.fault = Some("the timer is loaded but not armed".into());
        let line = job_line(&armed);
        assert!(line.contains("not armed") && !line.contains("unreadable"), "{line}");
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
