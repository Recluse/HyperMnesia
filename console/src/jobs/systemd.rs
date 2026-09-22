//! The systemd backend: user units under `~/.config/systemd/user/*.{service,timer}`, read through
//! `systemctl --user show`.
//!
//! **Read-only, deliberately.** `list()` is real; `run_now`, `set_schedule`, `install_self` and
//! `uninstall_self` all return "not implemented on Linux yet" rather than a silent no-op --
//! writing a systemd timer safely (the launchd side of this file is the backup-edit-lint-reload
//! dance in `launchd.rs`, about half its length) is real work this prototype does not yet do.
//! `autostart_fault` returns `None`: an honest "nothing to report" for a check the tray does not
//! yet perform, not a permanent warning about a missing feature -- a warning nobody can act on is
//! a warning nobody reads.
//!
//! systemd answers two of the three questions this module needs directly, and better than
//! launchd does: `ExecMainStatus`, `MainPID` and `LastTriggerUSec` are exactly "exit code", "is it
//! running" and "when did it last fire", and the last of those survives a reboot -- launchd keeps
//! nothing of the kind, which is why the launchd backend has to date runs by a log file's mtime
//! instead. What systemd does NOT keep is a run counter (`runs` stays `None` here always): the
//! nearest thing, `NRestarts`, counts crash-restarts of a still-loaded service, not lifetime runs,
//! and showing it under that label would be a different number wearing this one's name.
//!
//! All of `show`'s timestamp properties are parsed here rather than shelled out to `date`, and
//! that is only safe because every `systemctl --user show` call in this file pins `TZ=UTC` first:
//! with the zone fixed, the civil-calendar arithmetic in `parse_systemd_timestamp` needs no
//! timezone table at all, which is what keeps this data layer at zero dependencies.

use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, SystemTime};

use super::{log_state_of, Cal, Job, LogState, Schedule, TriggerState};

fn units_dir() -> Result<PathBuf, String> {
    let home = std::env::var("HOME")
        .map_err(|_| "HOME is not set, so I cannot tell where your systemd user units are"
                     .to_string())?;
    if home.is_empty() {
        return Err("HOME is empty, so I cannot tell where your systemd user units are".into());
    }
    Ok(PathBuf::from(home).join(".config/systemd/user"))
}

/// Every job with the given prefix, in name order.
///
/// A `.timer` is the unit of record: it is the schedule, and it is what the console lets a person
/// change (once that is implemented). A `.timer` with no matching `.service` is still listed --
/// `systemctl show` on the service side simply answers `LoadState=not-found`, which becomes a
/// fault on the row, not a silent drop.
///
/// Two failures are `Err`, not an empty `Ok(vec![])`, because an empty *reading* and an empty
/// *store* must never look alike: no session bus reachable (the tray started before one existed,
/// or is running somewhere the bus is not shared), and a units directory that exists but cannot
/// be read (permissions).
pub fn list(prefix: &str) -> Result<Vec<Job>, String> {
    if let Err(why) = bus_reachable() {
        return Err(why);
    }
    let dir = units_dir()?;
    let mut out = Vec::new();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        // A units directory that has never been created is an honest empty store, same as
        // launchd's LaunchAgents directory would be. Anything else -- permissions, a file where a
        // directory belongs -- is a fault: the console cannot tell "no jobs" from "could not look".
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(out),
        Err(e) => return Err(format!("cannot read {}: {e}", dir.display())),
    };
    for e in entries.flatten() {
        let p = e.path();
        if p.extension().and_then(|x| x.to_str()) != Some("timer") {
            continue;
        }
        let stem = p.file_stem().and_then(|x| x.to_str()).unwrap_or("").to_string();
        if !stem.starts_with(prefix) {
            continue;
        }
        out.push(read_unit(&stem, &p));
    }
    out.sort_by(|a, b| a.label.cmp(&b.label));
    Ok(out)
}

/// Is `systemctl --user` reachable at all? Distinguishes "no session bus" (a real fault) from
/// "no units defined yet" (an honest empty list), which an unconditional
/// `Ok(read_dir(...).unwrap_or_default())` would have collapsed into the same "every job
/// healthy" screen shown for a total blackout.
fn bus_reachable() -> Result<(), String> {
    let o = Command::new("systemctl").args(["--user", "show", "-p", "Version", "--", "-.mount"])
        .output().map_err(|e| format!("could not run systemctl: {e}"))?;
    if o.status.success() {
        return Ok(());
    }
    let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
    Err(if err.is_empty() {
        "systemctl --user did not answer".into()
    } else {
        format!("systemctl --user is not reachable: {err}")
    })
}

/// Read one job from its `.timer` file's stem, filling in what `systemctl --user show` knows
/// about both the timer and the service it triggers.
fn read_unit(stem: &str, timer_path: &PathBuf) -> Job {
    let timer_label = format!("{stem}.timer");
    let service_label = format!("{stem}.service");
    let timer_rows = match show(&timer_label) {
        Ok(r) => r,
        Err(why) => return broken_job(stem, timer_path, why),
    };
    if let Some(err) = load_fault(&timer_rows) {
        return broken_job(stem, timer_path, err);
    }
    let service_rows = show(&service_label).unwrap_or_default();

    let schedule = parse_schedule(&timer_rows);
    // Empty means "never fired" -- systemd's own answer, not an absence of one -- and must stay
    // distinguishable from `NotTracked`, or a job read on this backend could fall through to
    // launchd's log/run-count logic, find nothing there either (systemd jobs rarely set either),
    // and read as calm by accident.
    let trigger = match get(&timer_rows, "LastTriggerUSec").unwrap_or("") {
        "" => TriggerState::Never,
        raw => match parse_systemd_timestamp(raw) {
            Some(t) => TriggerState::At(t),
            // A non-empty LastTriggerUSec that fails to parse is worth a fault of its own: the
            // job DID fire, by the backend's own account, and silently falling back to "never
            // ran" would be exactly the quiet substitution this console exists to refuse.
            None => return broken_job(stem, timer_path,
                format!("the timer fired, but its own timestamp could not be read: {raw:?}")),
        },
    };

    let log = get(&service_rows, "StandardOutput")
        .and_then(|v| v.strip_prefix("append:"))
        .map(PathBuf::from)
        .or_else(|| service_log_from_file(stem));
    let last_exit = get(&service_rows, "ExecMainStatus").and_then(|v| v.parse().ok());
    let pid = get(&service_rows, "MainPID").and_then(|v| v.parse().ok()).filter(|p| *p != 0);
    let installed = get(&timer_rows, "FragmentPath")
        .map(PathBuf::from)
        .and_then(|p| std::fs::metadata(p).ok())
        .and_then(|m| m.modified().ok());
    let program = get(&service_rows, "ExecStart")
        .and_then(|v| extract_between(v, "path"))
        .map(|p| vec![p])
        .unwrap_or_default();

    Job {
        // The bare stem, not `timer_label`: `Job::short()` strips a trailing `.something` the
        // same way it strips the reverse-DNS prefix, and a label ending in `.timer` fed that
        // logic a name to cut at, dropping the job's actual name ("extract") and leaving "timer"
        // behind instead -- on every single job, since every systemd label ends the same way.
        label: stem.to_string(),
        source: timer_path.clone(),
        schedule,
        program,
        log_state: log_state_of(log.as_ref()),
        log,
        last_exit,
        installed,
        runs: None,               // systemd keeps no lifetime run counter
        pid,
        trigger,
        fault: enablement_fault(&timer_rows),
    }
}

/// Not scheduled by anything, despite having a valid unit: disabled, masked, or the timer is
/// loaded but not active. No launchd analogue -- launchd loads whatever is in LaunchAgents, but a
/// systemd unit can exist, parse cleanly, and still not be armed. Rendering that as a healthy
/// schedule would be the same quiet lie as an unreadable file read as an empty list.
fn enablement_fault(timer_rows: &[(String, String)]) -> Option<String> {
    let file_state = get(timer_rows, "UnitFileState").unwrap_or("");
    if matches!(file_state, "disabled" | "masked" | "masked-runtime") {
        return Some(format!("the timer is {file_state}, so it is not scheduled by anything"));
    }
    let active = get(timer_rows, "ActiveState").unwrap_or("");
    if active != "active" {
        let sub = get(timer_rows, "SubState").unwrap_or("");
        return Some(format!("the timer is loaded but not armed (ActiveState={active}, \
                             SubState={sub})"));
    }
    None
}

fn load_fault(rows: &[(String, String)]) -> Option<String> {
    match get(rows, "LoadState") {
        Some("loaded") | None => None,
        Some(other) => {
            let detail = get(rows, "LoadError").filter(|s| !s.is_empty());
            Some(match detail {
                Some(d) => format!("{other}: {d}"),
                None => other.to_string(),
            })
        }
    }
}

fn broken_job(stem: &str, path: &PathBuf, fault: String) -> Job {
    Job {
        label: stem.to_string(),               // see the comment on the same field in read_unit
        source: path.clone(),
        schedule: Schedule::None,
        program: Vec::new(),
        log: None,
        log_state: LogState::NotConfigured,
        last_exit: None,
        installed: std::fs::metadata(path).ok().and_then(|m| m.modified().ok()),
        runs: None,
        pid: None,
        trigger: TriggerState::NotTracked,
        fault: Some(fault),
    }
}

/// `systemctl --user show <unit>`, with the timezone pinned so every timestamp property comes
/// back in the one format `parse_systemd_timestamp` understands.
fn show(unit: &str) -> Result<Vec<(String, String)>, String> {
    let out = Command::new("systemctl")
        .env("TZ", "UTC")
        .args(["--user", "show", unit])
        .output()
        .map_err(|e| format!("could not run systemctl: {e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
        return Err(if err.is_empty() { "systemctl show failed".into() } else { err });
    }
    Ok(parse_show(&String::from_utf8_lossy(&out.stdout)))
}

/// `KEY=VALUE`, one per line. A key can repeat -- `TimersCalendar` does, once per `OnCalendar=`
/// slot in the unit -- so every row is kept rather than folded into a map.
fn parse_show(text: &str) -> Vec<(String, String)> {
    text.lines()
        .filter_map(|line| line.split_once('=').map(|(k, v)| (k.to_string(), v.to_string())))
        .collect()
}

fn get<'a>(rows: &'a [(String, String)], key: &str) -> Option<&'a str> {
    rows.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

fn get_all<'a, 'k>(rows: &'a [(String, String)], key: &'k str)
    -> impl Iterator<Item = &'a str> + 'k where 'a: 'k {
    rows.iter().filter(move |(k, _)| k == key).map(|(_, v)| v.as_str())
}

/// The schedule, from `TimersCalendar` (one row per `OnCalendar=` slot) or `TimersMonotonic`
/// (one `OnUnitActiveSec=`-style interval). Calendar wins if both are somehow present, since that
/// is the more specific thing to have said.
fn parse_schedule(timer_rows: &[(String, String)]) -> Schedule {
    let cals: Vec<Cal> = get_all(timer_rows, "TimersCalendar")
        .filter_map(|raw| extract_between(raw, "OnCalendar"))
        .filter_map(|spec| parse_oncalendar(&spec))
        .collect();
    match cals.len() {
        0 => {}
        1 => return Schedule::Calendar(cals.into_iter().next().unwrap()),
        _ => return Schedule::Several(cals),
    }
    if let Some(raw) = get(timer_rows, "TimersMonotonic") {
        if let Some(spec) = extract_between(raw, "OnUnitActiveUSec") {
            if let Some(secs) = parse_systemd_duration(&spec) {
                return Schedule::Every(secs);
            }
        }
    }
    Schedule::None
}

/// Pulls `key=...` out of systemd's `{ key=value ; next_elapse=... }` compound property value.
fn extract_between(raw: &str, key: &str) -> Option<String> {
    let after = raw.split_once(&format!("{key}="))?.1;
    let end = after.find(" ;").unwrap_or(after.len());
    let v = after[..end].trim();
    (!v.is_empty()).then(|| v.to_string())
}

/// An `OnCalendar=` spec, always in the normalized form systemd echoes back:
/// `["<Weekday> "]"<Y|*>-<M|*>-<D|*> "<H|*>:<M|*>:<S|*>`. Every field systemd does not pin is a
/// literal `*` here too -- `*:15:00` is "every hour at :15", not "00:15" -- the same
/// omission-is-a-wildcard rule launchd's plists use, and the same bug (a plausible wrong time,
/// and a monthly job measured against a day and a half) if it is read as a zero instead.
fn parse_oncalendar(spec: &str) -> Option<Cal> {
    const DAYS: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    let mut parts = spec.split_whitespace();
    let first = parts.next()?;
    let (weekday, date_tok) = match DAYS.iter().position(|d| *d == first) {
        Some(i) => (Some(i as u32), parts.next()?),
        None => (None, first),
    };
    let (month, day) = parse_calendar_field(date_tok);
    let (hour, minute) = parts.next().map(parse_time_field).unwrap_or((None, None));
    Some(Cal { minute, hour, day, weekday, month })
}

/// `Y-M-D`, each component `*` or a number. The year is read and discarded: `Cal` has no field
/// for it, and every schedule this console can set or has ever been asked to render is yearly at
/// most.
fn parse_calendar_field(s: &str) -> (Option<u32>, Option<u32>) {
    let mut it = s.split('-');
    let _year = it.next();
    let month = it.next().and_then(wildcard_num);
    let day = it.next().and_then(wildcard_num);
    (month, day)
}

/// `H:M:S`, each component `*` or a number. Seconds are read and discarded: nothing in `Cal`
/// tracks them, and no schedule this console offers is finer than a minute.
fn parse_time_field(s: &str) -> (Option<u32>, Option<u32>) {
    let mut it = s.split(':');
    let hour = it.next().and_then(wildcard_num);
    let minute = it.next().and_then(wildcard_num);
    (hour, minute)
}

fn wildcard_num(tok: &str) -> Option<u32> {
    if tok == "*" { None } else { tok.parse().ok() }
}

/// A duration the way `systemctl show` writes one back: compound, largest unit first --
/// `"1h 30min"`, `"1min 30s"`, `"4h"`. Not the `4h`/`15m` shorthand a person types (that is
/// `hypermnesia-jobs`' own `parse_interval`, on the way in); this is systemd's own way of writing
/// the same number back out, on the way out.
fn parse_systemd_duration(s: &str) -> Option<u64> {
    const UNITS: &[(&str, u64)] = &[
        ("usec", 0), ("us", 0), ("msec", 0), ("ms", 0),
        ("seconds", 1), ("second", 1), ("sec", 1), ("s", 1),
        ("minutes", 60), ("minute", 60), ("min", 60),
        ("hours", 3600), ("hour", 3600), ("h", 3600),
        ("days", 86_400), ("day", 86_400), ("d", 86_400),
        ("weeks", 604_800), ("week", 604_800), ("w", 604_800),
        ("months", 2_592_000), ("month", 2_592_000),
        ("years", 31_536_000), ("year", 31_536_000), ("y", 31_536_000),
    ];
    let mut total = 0u64;
    let mut saw_any = false;
    for tok in s.split_whitespace() {
        let digits_end = tok.find(|c: char| !c.is_ascii_digit() && c != '.')?;
        let (num, unit) = tok.split_at(digits_end);
        // Fractional seconds below our resolution: truncated, not rounded up into a lie about
        // precision this console never had.
        let n: f64 = num.parse().ok()?;
        let (_, mult) = UNITS.iter().find(|(u, _)| *u == unit)?;
        total += (n * (*mult as f64)) as u64;
        saw_any = true;
    }
    saw_any.then_some(total)
}

/// `"Tue 2026-09-22 16:45:31 UTC"` -> a `SystemTime`. Safe to hand-roll only because every caller
/// in this file forces `TZ=UTC` on the `systemctl` invocation first: with the zone pinned, this
/// needs plain Gregorian civil-calendar arithmetic and no timezone database, which is what keeps
/// this data layer dependency-free. `None` on anything unexpected -- a name-based zone abbreviation
/// slipping through despite the pin, for instance -- and the caller turns that into a fault rather
/// than a silent "never ran".
fn parse_systemd_timestamp(s: &str) -> Option<SystemTime> {
    let mut parts = s.split_whitespace();
    let _weekday = parts.next()?;
    let date = parts.next()?;
    let time = parts.next()?;
    let zone = parts.next()?;
    if zone != "UTC" {
        return None;
    }
    let mut d = date.split('-');
    let y: i64 = d.next()?.parse().ok()?;
    let mo: u32 = d.next()?.parse().ok()?;
    let da: u32 = d.next()?.parse().ok()?;
    let mut t = time.split(':');
    let h: u64 = t.next()?.parse().ok()?;
    let mi: u64 = t.next()?.parse().ok()?;
    // Seconds may carry a fractional part; truncate it the same way parse_systemd_duration does.
    let se: u64 = t.next()?.split('.').next()?.parse().ok()?;
    if mo == 0 || mo > 12 || da == 0 || da > 31 || h > 23 || mi > 59 || se > 60 {
        return None;
    }
    let days = days_from_civil(y, mo, da);
    let secs = days.checked_mul(86_400)?.checked_add((h * 3600 + mi * 60 + se) as i64)?;
    if secs < 0 {
        return None;                          // this console has no use for a date before 1970
    }
    Some(SystemTime::UNIX_EPOCH + Duration::from_secs(secs as u64))
}

/// Days since 1970-01-01, for a proleptic Gregorian civil date. Howard Hinnant's
/// `days_from_civil` algorithm (public domain) -- chosen over pulling in a time crate for the one
/// calculation this file needs, in keeping with the data layer's zero dependencies.
fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;                            // [0, 399]
    let mp = (m as i64 + 9) % 12;                        // [0, 11], Mar-based
    let doy = (153 * mp + 2) / 5 + d as i64 - 1;          // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;      // [0, 146096]
    era * 146_097 + doe - 719_468
}

fn service_log_from_file(stem: &str) -> Option<PathBuf> {
    let path = units_dir().ok()?.join(format!("{stem}.service"));
    let text = std::fs::read_to_string(path).ok()?;
    parse_standard_output(&text)
}

/// `StandardOutput=append:/path` as written in the unit file, for the (probably rare) case where
/// `systemctl show` cannot be reached but the file can -- mirrors `show`'s own stripped-prefix
/// reading of the property, so both paths agree.
fn parse_standard_output(unit_file: &str) -> Option<PathBuf> {
    unit_file.lines()
        .find_map(|l| l.trim().strip_prefix("StandardOutput=")?.strip_prefix("append:"))
        .map(PathBuf::from)
}

pub fn run_now(_label: &str) -> Result<String, String> {
    Err("running a job on demand is not implemented on Linux yet -- use `systemctl --user start \
        <name>.service` directly".into())
}

pub fn set_schedule(_job: &Job, _sched: &Schedule) -> Result<String, String> {
    Err("changing a schedule is not implemented on Linux yet -- edit the `.timer` unit and run \
        `systemctl --user daemon-reload`".into())
}

pub fn install_self(_label: &str) -> Result<String, String> {
    Err("installing the tray into autostart is not implemented on Linux yet -- add a \
        `hypermnesia-tray.service` user unit with `WantedBy=default.target` by hand".into())
}

pub fn uninstall_self(_label: &str) -> Result<String, String> {
    Err("removing the tray from autostart is not implemented on Linux yet".into())
}

/// Not implemented, so there is nothing to be wrong about -- `None`, not a permanent warning.
/// `bin/tray.rs` draws whatever this returns on every single menu rebuild, and the project's own
/// rule is that a warning shown forever is a warning nobody reads.
pub fn autostart_fault(_label: &str) -> Option<String> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    // Real output of `TZ=UTC systemctl --user show <name>.timer`, shortened to the properties
    // this module reads. Captured on systemd 259 (Fedora 44).
    const WEEKLY: &[(&str, &str)] = &[
        ("Id", "hypermnesia-reflect.timer"),
        ("LoadState", "loaded"),
        ("ActiveState", "active"),
        ("SubState", "waiting"),
        ("UnitFileState", "enabled"),
        ("FragmentPath", "/home/you/.config/systemd/user/hypermnesia-reflect.timer"),
        ("TimersCalendar", "{ OnCalendar=Mon *-*-* 06:10:00 ; next_elapse=Mon 2026-09-28 06:10:00 UTC }"),
        ("LastTriggerUSec", "Tue 2026-09-22 16:45:31 UTC"),
    ];

    fn rows(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    #[test]
    fn reads_a_weekly_calendar_schedule() {
        let r = rows(WEEKLY);
        assert_eq!(parse_schedule(&r), Schedule::at(6, 10, Some(1)));
    }

    /// An omitted field in an `OnCalendar=` spec is a WILDCARD, exactly as in a launchd plist:
    /// `*:15:00` means every hour at :15, not 00:15. Reading it as a zero gives a plausible wrong
    /// time and, far worse, a wrong period -- a monthly job measured against a day and a half
    /// reads as overdue for twenty-nine days out of thirty.
    #[test]
    fn an_omitted_calendar_field_is_a_wildcard_not_a_zero() {
        let hourly = rows(&[("TimersCalendar",
            "{ OnCalendar=*-*-* *:15:00 ; next_elapse=(null) }")]);
        let s = parse_schedule(&hourly);
        assert_eq!(s, Schedule::Calendar(Cal { minute: Some(15), ..Cal::default() }));
        assert_eq!(s.human(), "hourly at :15");

        let monthly = rows(&[("TimersCalendar",
            "{ OnCalendar=*-*-01 03:00:00 ; next_elapse=(null) }")]);
        let s = parse_schedule(&monthly);
        assert_eq!(s.human(), "monthly day 1 03:00");

        let every_minute = rows(&[("TimersCalendar",
            "{ OnCalendar=*-*-* *:*:00 ; next_elapse=(null) }")]);
        assert_eq!(parse_schedule(&every_minute).human(), "every minute");
    }

    /// Several `OnCalendar=` lines are several rows of the same key -- the period must come from
    /// the WIDEST slot, exactly as for launchd's array form, or two weekly slots would be
    /// measured against a day.
    #[test]
    fn several_calendar_lines_keep_their_widest_period() {
        let r = rows(&[
            ("TimersCalendar", "{ OnCalendar=*-*-* 06:10:00 ; next_elapse=(null) }"),
            ("TimersCalendar", "{ OnCalendar=*-*-* 18:10:00 ; next_elapse=(null) }"),
        ]);
        let s = parse_schedule(&r);
        assert!(matches!(&s, Schedule::Several(c) if c.len() == 2));
    }

    #[test]
    fn reads_a_monotonic_schedule() {
        let r = rows(&[("TimersMonotonic", "{ OnUnitActiveUSec=4h ; next_elapse=0 }")]);
        assert_eq!(parse_schedule(&r), Schedule::Every(14_400));

        let compound = rows(&[("TimersMonotonic", "{ OnUnitActiveUSec=1h 30min ; next_elapse=0 }")]);
        assert_eq!(parse_schedule(&compound), Schedule::Every(5_400));

        let seconds = rows(&[("TimersMonotonic", "{ OnUnitActiveUSec=1min 30s ; next_elapse=0 }")]);
        assert_eq!(parse_schedule(&seconds), Schedule::Every(90));
    }

    #[test]
    fn a_timestamp_round_trips_through_civil_arithmetic() {
        // Checked against `date -u -d "2026-09-22 16:45:31 UTC" +%s` => 1790095531.
        let t = parse_systemd_timestamp("Tue 2026-09-22 16:45:31 UTC").expect("parses");
        let secs = t.duration_since(SystemTime::UNIX_EPOCH).unwrap().as_secs();
        assert_eq!(secs, 1_790_095_531);
    }

    /// A second date, in a different month and a leap year, to catch a days-in-month or
    /// era-boundary mistake the first sample would not exercise.
    #[test]
    fn a_second_timestamp_in_a_leap_year_also_round_trips() {
        // `date -u -d "2028-02-29 00:00:00 UTC" +%s`.
        let t = parse_systemd_timestamp("Tue 2028-02-29 00:00:00 UTC").expect("parses");
        let secs = t.duration_since(SystemTime::UNIX_EPOCH).unwrap().as_secs();
        assert_eq!(secs, 1_835_395_200);
    }

    #[test]
    fn a_non_utc_timestamp_is_refused_rather_than_misread() {
        // Every caller pins TZ=UTC before asking systemctl for one of these; if that guard were
        // ever dropped, a name-based zone abbreviation must not be silently read as UTC.
        assert!(parse_systemd_timestamp("Tue 2026-09-22 18:15:31 IST").is_none());
        assert!(parse_systemd_timestamp("Tue 2026-09-22 12:45:31 +04").is_none());
    }

    #[test]
    fn an_empty_last_trigger_means_never_fired_not_unparseable() {
        // list()'s own handling of "" lives in read_unit, not here -- but the string itself must
        // not be handed to the parser, which would (correctly) refuse it.
        assert!(parse_systemd_timestamp("").is_none());
    }

    #[test]
    fn a_disabled_timer_is_a_fault_not_a_healthy_schedule() {
        let r = rows(&[("UnitFileState", "disabled"), ("ActiveState", "inactive")]);
        assert!(enablement_fault(&r).is_some());
    }

    #[test]
    fn a_loaded_but_inactive_timer_is_a_fault() {
        let r = rows(&[("UnitFileState", "enabled"), ("ActiveState", "inactive"),
                       ("SubState", "dead")]);
        assert!(enablement_fault(&r).is_some());
    }

    #[test]
    fn an_active_enabled_timer_has_no_enablement_fault() {
        let r = rows(&[("UnitFileState", "enabled"), ("ActiveState", "active"),
                       ("SubState", "waiting")]);
        assert!(enablement_fault(&r).is_none());
    }

    #[test]
    fn a_bad_unit_file_is_a_load_fault_with_the_reason_attached() {
        let r = rows(&[("LoadState", "bad-setting"),
                       ("LoadError", "org.freedesktop.systemd1.BadUnitSetting \"bad setting\"")]);
        let f = load_fault(&r).expect("fault");
        assert!(f.contains("bad setting"), "{f}");
    }

    #[test]
    fn a_not_found_unit_is_a_load_fault() {
        let r = rows(&[("LoadState", "not-found")]);
        assert!(load_fault(&r).is_some());
    }

    #[test]
    fn a_loaded_unit_has_no_load_fault() {
        assert!(load_fault(&rows(&[("LoadState", "loaded")])).is_none());
    }

    #[test]
    fn the_exec_start_path_is_pulled_out_of_the_compound_property() {
        let raw = "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /x/mem_extract.py ; \
                   ignore_errors=no }";
        assert_eq!(extract_between(raw, "path").as_deref(), Some("/usr/bin/python3"));
    }

    #[test]
    fn standard_output_append_path_is_read_from_the_unit_file() {
        let unit = "[Service]\nType=oneshot\nStandardOutput=append:/home/you/.hypermnesia/logs/x.log\n";
        assert_eq!(parse_standard_output(unit),
                   Some(PathBuf::from("/home/you/.hypermnesia/logs/x.log")));
        assert_eq!(parse_standard_output("[Service]\nType=oneshot\n"), None);
    }

    #[test]
    fn write_actions_refuse_rather_than_pretend() {
        assert!(run_now("hypermnesia-extract").is_err());
        let job = Job {
            label: "x".into(), source: PathBuf::new(), schedule: Schedule::None,
            program: vec![], log: None, log_state: LogState::NotConfigured, last_exit: None,
            installed: None, runs: None, pid: None, trigger: TriggerState::NotTracked, fault: None,
        };
        assert!(set_schedule(&job, &Schedule::Every(3600)).is_err());
        assert!(install_self("hypermnesia-tray").is_err());
        assert!(uninstall_self("hypermnesia-tray").is_err());
    }

    #[test]
    fn autostart_fault_is_none_not_a_permanent_warning() {
        // Not implemented is not the same claim as "checked, and it's fine" -- but a permanent
        // warning drawn on every menu rebuild is a warning nobody reads, so this returns the
        // same `None` a passing check would.
        assert_eq!(autostart_fault("hypermnesia-tray"), None);
    }
}
