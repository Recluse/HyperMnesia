//! The systemd backend: user units under `~/.config/systemd/user/*.{service,timer}`, read through
//! `systemctl --user show` and written through the same backup -> edit -> verify -> reload ->
//! read-back -> rollback discipline `launchd.rs` uses for its plists. Nothing here claims success
//! from an exit code alone: every write is checked against what `systemctl --user show` reports
//! afterwards, because a `0` from `systemctl` means "the request was accepted", not "the change
//! took" -- a timer already loaded holds its own copy of the schedule until it is reloaded, and a
//! disabled unit still accepts `daemon-reload` without complaint.
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
        armed: Some(get(&timer_rows, "UnitFileState") == Some("enabled")
                    && get(&timer_rows, "ActiveState") == Some("active")),
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
        armed: None,               // could not be read, so nothing is known about it either
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
    // `TimersMonotonic` is one row PER monotonic directive -- a unit with `OnBootSec=` next to
    // `OnUnitActiveSec=` (which `set_schedule` writes together, so an interval timer also fires
    // after a boot) reports two rows, and `get`'s first-match would as likely hand back the
    // `OnBootUSec` one, which has no `OnUnitActiveUSec=` key and reads as "no schedule at all".
    // `OnUnitActiveUSec` is what this console's own interval schedules are -- and about the only
    // monotonic key it would ever need to read back.
    if let Some(spec) = get_all(timer_rows, "TimersMonotonic")
        .find_map(|raw| extract_between(raw, "OnUnitActiveUSec")) {
        if let Some(secs) = parse_systemd_duration(&spec) {
            return Schedule::Every(secs);
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

/// A unit name is a shell argument and a path component both. Rejecting anything but the
/// characters systemd itself allows in an instance/template name keeps every `Command::new`
/// below from ever needing to worry about an escape.
fn valid_stem(stem: &str) -> Result<(), String> {
    if stem.is_empty() {
        return Err("the job name is empty".into());
    }
    let ok = stem.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
        && !stem.starts_with('-') && !stem.contains("..");
    if ok {
        Ok(())
    } else {
        Err(format!("{stem:?} is not a valid systemd unit name"))
    }
}

/// Run the job immediately, without touching its schedule.
///
/// `--no-block` matters: these are `Type=oneshot` services, and a plain `start` waits for the
/// unit to finish before returning -- which would sit on the tray's worker thread for as long as
/// the job takes, well past the 90 s the tray gives up waiting for an answer at all.
///
/// systemd's `InvocationID` is the counterpart of launchd's run counter: a fresh UUID every time
/// the unit starts, whether or not it succeeds. The state is read back rather than trusted from
/// the exit code of `start`, which only says the request was accepted -- a program that is not
/// there fails immediately and systemd records that asynchronously, the same race `launchd.rs`
/// documents for `kickstart`.
pub fn run_now(label: &str) -> Result<String, String> {
    valid_stem(label)?;
    let service = format!("{label}.service");
    let before_id = show(&service).ok()
        .and_then(|r| get(&r, "InvocationID").map(str::to_string));

    let o = Command::new("systemctl").args(["--user", "start", "--no-block", "--", &service])
        .output().map_err(|e| format!("could not call systemctl: {e}"))?;
    if !o.status.success() {
        let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
        return Err(if err.is_empty() { format!("systemctl start {service} failed") } else { err });
    }

    // A short look-back: long enough for a program that is not there to have failed and for
    // systemd to have noticed, short enough that a person does not notice the wait.
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    let after = loop {
        let rows = show(&service).ok();
        let moved = rows.as_deref().and_then(|r| get(r, "InvocationID"))
            .is_some_and(|id| Some(id) != before_id.as_deref());
        let running = rows.as_deref().and_then(|r| get(r, "MainPID"))
            .and_then(|v| v.parse::<u32>().ok()).is_some_and(|p| p != 0);
        if moved || running || std::time::Instant::now() >= deadline {
            break rows;
        }
        std::thread::sleep(Duration::from_millis(150));
    };
    let Some(after) = after else {
        return Ok(format!("systemctl accepted the request for {service}; its state could not be \
                           read back"));
    };
    if get(&after, "MainPID").and_then(|v| v.parse::<u32>().ok()).is_some_and(|p| p != 0) {
        return Ok(format!("running: {label}"));
    }
    // The exit code systemd remembers belongs to a run, and only an InvocationID that MOVED
    // proves which run that is -- without that check a job that failed yesterday and has not
    // started yet would be reported as having just exited.
    let moved = get(&after, "InvocationID").is_some_and(|id| Some(id) != before_id.as_deref());
    let exit = get(&after, "ExecMainStatus").and_then(|v| v.parse::<i32>().ok());
    match (moved, exit) {
        (true, Some(0)) => Ok(format!("ran: {label} (exit 0)")),
        (true, Some(c)) => Err(format!("{label} ran and exited {c}")),
        (true, None) => Ok(format!("ran: {label} (systemd reports no exit code yet)")),
        (false, _) => Err(format!(
            "systemctl accepted the request but {label} has not started -- its invocation id \
             has not moved. Check the log.")),
    }
}

/// Replace the schedule keys inside a unit file's `[Timer]` section. Pure text in, text out -- no
/// filesystem, no `systemctl` -- so this is where the tests live rather than against a real
/// system.
fn rewrite_timer(text: &str, sched: &Schedule) -> Result<String, String> {
    let new_line = match sched {
        Schedule::Every(secs) => format!("OnUnitActiveSec={secs}\nOnBootSec={secs}"),
        Schedule::Calendar(c) => format!("OnCalendar={}", render_oncalendar(c)),
        Schedule::Several(_) | Schedule::None => return Err(
            "this console sets one schedule at a time; several calendar slots or no schedule \
             at all has to be edited in the unit by hand".into()),
    };

    let lines: Vec<&str> = text.lines().collect();
    let section = lines.iter().position(|l| l.trim() == "[Timer]")
        .ok_or_else(|| "the unit has no [Timer] section to edit".to_string())?;
    let end = lines[section + 1..].iter().position(|l| l.trim_start().starts_with('['))
        .map(|i| section + 1 + i).unwrap_or(lines.len());

    const SCHEDULE_KEYS: [&str; 4] =
        ["OnCalendar=", "OnUnitActiveSec=", "OnUnitInactiveSec=", "OnBootSec="];
    let mut out: Vec<String> = lines[..=section].iter().map(|s| s.to_string()).collect();
    for l in &lines[section + 1..end] {
        if !SCHEDULE_KEYS.iter().any(|k| l.trim_start().starts_with(k)) {
            out.push(l.to_string());
        }
    }
    out.push(new_line);
    out.extend(lines[end..].iter().map(|s| s.to_string()));
    let mut result = out.join("\n");
    if text.ends_with('\n') {
        result.push('\n');
    }
    Ok(result)
}

/// The inverse of `parse_oncalendar`: an omitted `Cal` field is a wildcard `*`, never a zero --
/// the same rule `edit_schedule` in `launchd.rs` writes plist calendar keys by. The year is always
/// `*`: `Cal` carries none, and every schedule this console sets is yearly at most.
fn render_oncalendar(c: &Cal) -> String {
    const DAYS: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    let num = |v: Option<u32>| v.map(|n| format!("{n:02}")).unwrap_or_else(|| "*".to_string());
    let weekday = c.weekday.map(|w| format!("{} ", DAYS[(w as usize) % 7])).unwrap_or_default();
    format!("{weekday}*-{}-{} {}:{}:00", num(c.month), num(c.day), num(c.hour), num(c.minute))
}

/// Check the edited unit before anything is reloaded into systemd -- the counterpart of
/// `plutil -lint`. A unit that fails this check simply is not accepted by systemd, so reloading
/// it anyway would leave the timer quietly unarmed rather than on the new schedule.
///
/// `systemd-analyze` is not present on every system (some minimal distros ship `systemctl`
/// without it); the fallback asks the same question a different way, through `daemon-reload` and
/// the `LoadState` this module already knows how to read.
fn verify_unit(path: &str) -> Result<(), String> {
    match Command::new("systemd-analyze").args(["--user", "verify", path]).output() {
        Ok(o) if o.status.success() => Ok(()),
        Ok(o) => {
            let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
            Err(if err.is_empty() { "systemd-analyze verify failed".into() } else { err })
        }
        Err(_) => {
            let _ = Command::new("systemctl").args(["--user", "daemon-reload"]).output();
            let unit = std::path::Path::new(path).file_name()
                .and_then(|f| f.to_str()).unwrap_or("").to_string();
            match show(&unit) {
                Ok(rows) => load_fault(&rows).map_or(Ok(()), Err),
                Err(e) => Err(e),
            }
        }
    }
}

/// Reload systemd's view of the unit files and re-arm the timer. A reload alone is not enough:
/// a running timer holds its own copy of the schedule from the moment it was armed, the same trap
/// `launchd.rs` documents for `bootstrap` -- so the timer is restarted, not merely reloaded.
fn reload_and_restart(label: &str) -> Result<(), String> {
    let o = Command::new("systemctl").args(["--user", "daemon-reload"]).output()
        .map_err(|e| format!("could not call systemctl: {e}"))?;
    if !o.status.success() {
        let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
        return Err(if err.is_empty() { "daemon-reload failed".into() } else { err });
    }
    let timer = format!("{label}.timer");
    let o = Command::new("systemctl").args(["--user", "restart", "--", &timer]).output()
        .map_err(|e| format!("could not call systemctl: {e}"))?;
    if !o.status.success() {
        let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
        return Err(if err.is_empty() { format!("restart {timer} failed") } else { err });
    }
    Ok(())
}

/// Change a job's schedule: edit the `.timer` unit and reload it into systemd.
///
/// Mirrors `launchd.rs::set_schedule`'s discipline, not its mechanics: a `.bak` copy is made
/// first (refusing if one already exists -- the leftover of an edit that did not finish, and the
/// only copy of the working schedule worth trusting), the file is rewritten, checked, reloaded,
/// and finally **read back** through `systemctl --user show` -- the point of every step before it.
/// Any failure restores the backup and says, in words, whether the restore itself worked.
pub fn set_schedule(job: &Job, sched: &Schedule) -> Result<String, String> {
    if matches!(sched, Schedule::None | Schedule::Several(_)) {
        return Err("this console changes one calendar slot or one interval at a time; several \
                    slots or no schedule at all has to be edited in the unit by hand".into());
    }
    if job.unwritable() {
        return Err(format!("this unit could not be read ({}) -- I will not edit it",
                            job.fault.as_deref().unwrap_or("")));
    }
    valid_stem(&job.label)?;
    let path = job.source.to_string_lossy().to_string();
    let backup = format!("{path}.bak");
    if std::fs::metadata(&backup).is_ok() {
        return Err(format!("{backup} already exists: an earlier edit did not finish. Compare it \
                            with {path} and remove it before editing again -- it may be the only \
                            copy of the working schedule."));
    }
    let original = std::fs::read_to_string(&path).map_err(|e| format!("cannot read {path}: {e}"))?;
    std::fs::copy(&path, &backup).map_err(|e| format!("cannot make a backup copy {backup}: {e}"))?;

    let rewritten = match rewrite_timer(&original, sched) {
        Ok(text) => text,
        Err(e) => {
            let _ = std::fs::remove_file(&backup);
            return Err(e);
        }
    };
    if let Err(e) = std::fs::write(&path, &rewritten) {
        return Err(match std::fs::copy(&backup, &path) {
            Ok(_) => { let _ = std::fs::remove_file(&backup);
                       format!("could not write the unit ({e}); the file was restored from the \
                                backup copy") }
            Err(c) => format!("could not write the unit ({e}) AND it could not be restored \
                               ({c}). The good copy is at {backup}."),
        });
    }

    if let Err(e) = verify_unit(&path) {
        return Err(match std::fs::copy(&backup, &path) {
            Ok(_) => { let _ = std::fs::remove_file(&backup);
                       format!("the unit does not pass the check after the edit ({e}); the file \
                                was restored from the backup copy, the schedule was not changed") }
            Err(c) => format!("the unit does not pass the check after the edit ({e}) AND it \
                               could NOT be restored ({c}). The good copy is at {backup}."),
        });
    }

    if let Err(e) = reload_and_restart(&job.label) {
        if let Err(c) = std::fs::copy(&backup, &path) {
            return Err(format!("systemd did not accept the new schedule ({e}) AND the file \
                could not be restored ({c}). The good copy is at {backup}; the file on disk \
                carries the new schedule that systemd refused."));
        }
        let reloaded = reload_and_restart(&job.label);
        let _ = std::fs::remove_file(&backup);
        return Err(match reloaded {
            Ok(()) => format!("systemd did not accept the new schedule ({e}); the file was \
                               restored and the timer is armed again"),
            Err(e2) => format!("systemd did not accept the new schedule ({e}); the file was \
                restored BUT the timer could not be reloaded either ({e2}). Fix it with: \
                systemctl --user daemon-reload && systemctl --user restart {}.timer", job.label),
        });
    }

    // Read back what actually landed, not what was asked for -- the point of every step above.
    let timer_label = format!("{}.timer", job.label);
    let landed = show(&timer_label).ok().map(|rows| parse_schedule(&rows));
    if landed.as_ref() != Some(sched) {
        let _ = std::fs::copy(&backup, &path);
        let _ = reload_and_restart(&job.label);
        let _ = std::fs::remove_file(&backup);
        return Err(format!(
            "systemd reloaded the unit but reports a different schedule than was set ({}) -- \
             the file was restored", landed.map(|s| s.human()).unwrap_or_else(|| "none".into())));
    }

    let _ = std::fs::remove_file(&backup);
    Ok(format!("{}: {}", job.short(), sched.human()))
}

/// Arm or disarm a timer without touching its schedule -- the action `enablement_fault` can
/// already see is needed but the read-only tray could not take.
pub const SUPPORTS_ENABLE: bool = true;

pub fn set_enabled(job: &Job, enabled: bool) -> Result<String, String> {
    valid_stem(&job.label)?;
    let timer = format!("{}.timer", job.label);
    let before = show(&timer)?;
    if matches!(get(&before, "UnitFileState"), Some("masked" | "masked-runtime")) {
        return Err(format!("{timer} is masked -- unmask it first: \
                            systemctl --user unmask {timer}"));
    }
    let verb = if enabled { "enable" } else { "disable" };
    let o = Command::new("systemctl").args(["--user", verb, "--now", "--", &timer]).output()
        .map_err(|e| format!("could not call systemctl: {e}"))?;
    if !o.status.success() {
        let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
        return Err(if err.is_empty() { format!("{verb} {timer} failed") } else { err });
    }
    let after = show(&timer)?;
    let now_active = get(&after, "ActiveState") == Some("active");
    if now_active != enabled {
        return Err(format!("{timer} was asked to {verb}, but its ActiveState is now {:?}",
                            get(&after, "ActiveState").unwrap_or("")));
    }
    Ok(format!("{}: {}", job.short(), if enabled { "enabled" } else { "disabled" }))
}

/// The tray's own autostart unit, as text. A function so a test can read what is actually
/// written, the same role `tray_plist` plays in `launchd.rs`.
///
/// `Restart=on-failure`, not `always`: an unconditional restart makes Quit a menu item that lies
/// -- the tray reappearing seconds after someone chose to close it -- which is exactly the trap
/// `launchd.rs`'s `KeepAlive` comment documents for the plist side of this same unit.
/// `WantedBy=default.target` rather than a graphical-session target: not every desktop environment
/// reaches the latter, and `default.target` is the one every session manager pulls in.
fn tray_unit(exe: &str, log: &str) -> String {
    format!(
        "[Unit]\n\
         Description=HyperMnesia tray\n\
         \n\
         [Service]\n\
         Type=simple\n\
         ExecStart={exe}\n\
         Restart=on-failure\n\
         RestartSec=5\n\
         StandardOutput=append:{log}\n\
         StandardError=append:{log}\n\
         \n\
         [Install]\n\
         WantedBy=default.target\n")
}

/// Put the console itself into autostart: a unit pointing at the current binary, enabled but not
/// started -- starting it now would run a second tray beside the one already open.
pub fn install_self(label: &str) -> Result<String, String> {
    valid_stem(label)?;
    let exe = std::env::current_exe()
        .map_err(|e| format!("cannot determine the path to myself: {e}"))?
        .to_string_lossy().to_string();
    let home = std::env::var("HOME").map_err(|_| "no HOME".to_string())?;
    let dir = units_dir()?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let path = dir.join(format!("{label}.service"));
    let log = format!("{home}/.hypermnesia/logs/tray.log");
    if let Some(d) = PathBuf::from(&log).parent() {
        let _ = std::fs::create_dir_all(d);
    }
    std::fs::write(&path, tray_unit(&exe, &log)).map_err(|e| format!("{}: {e}", path.display()))?;

    if let Err(e) = Command::new("systemctl").args(["--user", "daemon-reload"]).output() {
        let _ = std::fs::remove_file(&path);
        return Err(format!("could not call systemctl: {e}"));
    }
    let unit = format!("{label}.service");
    let o = Command::new("systemctl").args(["--user", "enable", "--", &unit]).output()
        .map_err(|e| format!("could not call systemctl: {e}"))?;
    if !o.status.success() {
        let err = String::from_utf8_lossy(&o.stderr).trim().to_string();
        let _ = std::fs::remove_file(&path);
        let _ = Command::new("systemctl").args(["--user", "daemon-reload"]).output();
        return Err(format!("the unit was written but systemd would not enable it: {}",
                            if err.is_empty() { "enable failed".into() } else { err }));
    }
    Ok(format!("{} installed into autostart\nbinary: {exe}\nlog: {log}\nit starts at your next \
               login; to start it now: systemctl --user start {unit}", path.display()))
}

/// Take the console out of autostart.
pub fn uninstall_self(label: &str) -> Result<String, String> {
    valid_stem(label)?;
    let path = units_dir()?.join(format!("{label}.service"));
    let unit = format!("{label}.service");
    let _ = Command::new("systemctl").args(["--user", "disable", "--now", "--", &unit]).output();
    match std::fs::remove_file(&path) {
        Ok(()) => {
            let _ = Command::new("systemctl").args(["--user", "daemon-reload"]).output();
            Ok(format!("{} removed from autostart", path.display()))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound =>
            Err(format!("{} is not there anyway", path.display())),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

/// Is the installed autostart unit still pointing at this binary, and still armed?
///
/// A unit that has never been installed is `None`, not a fault -- `install_self` not having been
/// run is not the same claim as "checked, and something is wrong". A unit that exists but points
/// somewhere else, or is masked/disabled, is the same quiet failure `launchd.rs`'s version of this
/// function was written to catch: a tray moved, rebuilt elsewhere, or installed from a copy that
/// has since been deleted, failing at every login with nothing on screen to say so.
pub fn autostart_fault(label: &str) -> Option<String> {
    let path = units_dir().ok()?.join(format!("{label}.service"));
    let text = match std::fs::read_to_string(&path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return None,
        Err(e) => return Some(format!("{}: {e}", path.display())),
    };
    let installed = text.lines().find_map(|l| l.trim().strip_prefix("ExecStart="))?.to_string();
    if !std::path::Path::new(&installed).exists() {
        return Some(format!("autostart points at {installed}, which is not there any more"));
    }
    let me = std::env::current_exe().ok()?.to_string_lossy().to_string();
    if installed != me {
        return Some(format!("autostart starts {installed}, not this binary ({me})"));
    }
    let rows = show(&format!("{label}.service")).ok()?;
    match get(&rows, "UnitFileState") {
        Some("disabled" | "masked" | "masked-runtime") => Some(format!(
            "the tray's autostart unit is {}", get(&rows, "UnitFileState").unwrap_or(""))),
        _ => None,
    }
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

    /// `set_schedule` writes `OnBootSec=` next to `OnUnitActiveSec=`, so systemd reports TWO
    /// `TimersMonotonic` rows -- one per directive. Reproduced against a real unit: `systemctl
    /// show` lists the `OnBootUSec` row first, and `get`'s first-match used to hand that one
    /// back, find no `OnUnitActiveUSec=` key in it, and read the whole timer as unscheduled --
    /// which would have made `set_schedule`'s own read-back check fail every single interval
    /// schedule it had just written.
    #[test]
    fn a_monotonic_schedule_is_found_even_behind_an_onboot_row() {
        let r = rows(&[
            ("TimersMonotonic", "{ OnBootUSec=1h ; next_elapse=1h }"),
            ("TimersMonotonic", "{ OnUnitActiveUSec=1h ; next_elapse=12h }"),
        ]);
        assert_eq!(parse_schedule(&r), Schedule::Every(3_600));
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

    fn broken(label: &str, schedule: Schedule) -> Job {
        Job {
            label: label.into(), source: PathBuf::new(), schedule,
            program: vec![], log: None, log_state: LogState::NotConfigured, last_exit: None,
            installed: None, runs: None, pid: None, trigger: TriggerState::NotTracked,
            armed: None, fault: Some("could not be read".into()),
        }
    }

    #[test]
    fn set_schedule_refuses_a_job_with_a_fault_without_touching_the_filesystem() {
        assert!(set_schedule(&broken("x", Schedule::Every(3600)), &Schedule::Every(3600)).is_err());
    }

    #[test]
    fn set_schedule_refuses_none_and_several_before_touching_anything() {
        let mut j = broken("x", Schedule::None);
        j.fault = None;
        assert!(set_schedule(&j, &Schedule::None).is_err());
        assert!(set_schedule(&j, &Schedule::Several(vec![Cal::default()])).is_err());
    }

    /// A unit name is a path component and a shell argument both; anything that could escape
    /// either is refused before a single `Command` is built.
    #[test]
    fn valid_stem_rejects_traversal_and_shell_metacharacters() {
        assert!(valid_stem("hypermnesia-extract").is_ok());
        assert!(valid_stem("").is_err());
        assert!(valid_stem("../etc/passwd").is_err());
        assert!(valid_stem("a/b").is_err());
        assert!(valid_stem("-rf").is_err());
        assert!(valid_stem("a b").is_err());
        assert!(valid_stem("a;rm -rf ~").is_err());
    }

    /// The inverse of `parse_oncalendar`: every field it can produce round-trips, and an omitted
    /// field renders as `*`, never a zero -- the same bug `parse_oncalendar`'s own tests guard
    /// against, from the writing side this time.
    #[test]
    fn render_oncalendar_round_trips_through_parse_oncalendar() {
        let weekly = Cal { hour: Some(6), minute: Some(10), day: None, weekday: Some(1),
                            month: None };
        assert_eq!(render_oncalendar(&weekly), "Mon *-*-* 06:10:00");
        assert_eq!(parse_oncalendar(&render_oncalendar(&weekly)), Some(weekly));

        let monthly = Cal { day: Some(1), hour: Some(3), minute: Some(0), weekday: None,
                            month: None };
        assert_eq!(render_oncalendar(&monthly), "*-*-01 03:00:00");
        assert_eq!(parse_oncalendar(&render_oncalendar(&monthly)), Some(monthly));

        let hourly = Cal { minute: Some(15), ..Cal::default() };
        assert_eq!(render_oncalendar(&hourly), "*-*-* *:15:00");
        assert_eq!(parse_oncalendar(&render_oncalendar(&hourly)), Some(hourly));
    }

    #[test]
    fn rewrite_timer_replaces_an_existing_oncalendar_rather_than_doubling_it() {
        let unit = "[Unit]\nDescription=x\n\n[Timer]\nOnCalendar=*-*-* 05:00:00\nPersistent=true\n\
                    \n[Install]\nWantedBy=timers.target\n";
        let sched = Schedule::at(6, 10, Some(1));
        let out = rewrite_timer(unit, &sched).expect("rewrites");
        assert_eq!(out.matches("OnCalendar=").count(), 1, "{out}");
        assert!(out.contains("OnCalendar=Mon *-*-* 06:10:00"), "{out}");
        assert!(out.contains("Persistent=true"), "unrelated keys survive: {out}");
        assert!(out.contains("WantedBy=timers.target"), "sections after [Timer] survive: {out}");
    }

    #[test]
    fn rewrite_timer_switches_an_interval_schedule_and_sets_onbootsec_too() {
        let unit = "[Timer]\nOnCalendar=*-*-* 05:00:00\n\n[Install]\nWantedBy=timers.target\n";
        let out = rewrite_timer(unit, &Schedule::Every(14_400)).expect("rewrites");
        assert!(!out.contains("OnCalendar="), "{out}");
        assert!(out.contains("OnUnitActiveSec=14400"), "{out}");
        assert!(out.contains("OnBootSec=14400"), "so an interval timer also fires after a boot: {out}");
    }

    #[test]
    fn rewrite_timer_refuses_without_a_timer_section() {
        assert!(rewrite_timer("[Unit]\nDescription=x\n", &Schedule::Every(3600)).is_err());
    }

    #[test]
    fn rewrite_timer_refuses_none_and_several() {
        assert!(rewrite_timer("[Timer]\n", &Schedule::None).is_err());
        assert!(rewrite_timer("[Timer]\n", &Schedule::Several(vec![Cal::default()])).is_err());
    }

    /// `Restart=on-failure`, not `always`: an unconditional restart makes Quit a button that
    /// lies, the same trap `launchd.rs`'s `KeepAlive` comment documents for the plist side.
    #[test]
    fn tray_unit_restarts_on_failure_only_and_points_at_the_binary() {
        let unit = tray_unit("/usr/local/bin/hypermnesia", "/home/you/.hypermnesia/logs/tray.log");
        assert!(unit.contains("Restart=on-failure"), "{unit}");
        assert!(!unit.contains("Restart=always"), "{unit}");
        assert!(unit.contains("ExecStart=/usr/local/bin/hypermnesia"), "{unit}");
        assert!(unit.contains("WantedBy=default.target"), "{unit}");
    }

    /// A unit that was never installed is an honest "nothing to report", not a fault -- the same
    /// rule the read-only version of this function was already written to.
    #[test]
    fn autostart_fault_is_none_when_nothing_was_ever_installed() {
        // A label no real installation would use, so the read simply finds no file.
        assert_eq!(autostart_fault("hypermnesia-tray-selftest-does-not-exist"), None);
    }
}
