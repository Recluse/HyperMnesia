//! Neither launchd nor systemd: a platform this console has no scheduled-jobs backend for.
//!
//! `list()` errors rather than returning an empty list -- "not supported here" and "no jobs
//! configured" are different facts, and the console must not print the second when it means the
//! first.

use super::{Job, Schedule};

pub fn list(_prefix: &str) -> Result<Vec<Job>, String> {
    Err("scheduled jobs are not supported on this platform".into())
}

pub fn run_now(_label: &str) -> Result<String, String> {
    Err("scheduled jobs are not supported on this platform".into())
}

pub fn set_schedule(_job: &Job, _sched: &Schedule) -> Result<String, String> {
    Err("scheduled jobs are not supported on this platform".into())
}

pub fn install_self(_label: &str) -> Result<String, String> {
    Err("autostart is not supported on this platform".into())
}

pub fn uninstall_self(_label: &str) -> Result<String, String> {
    Err("autostart is not supported on this platform".into())
}

pub fn autostart_fault(_label: &str) -> Option<String> {
    None
}
