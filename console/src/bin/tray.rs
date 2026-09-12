//! The memory console in the menu bar.
//!
//! A menu, not a window. Everything this console has to show is a dozen lines of state and a
//! dozen buttons; a window would mean a GUI framework for the same result.
//!
//! Two rules everything else follows from:
//!
//! 1. The main thread never waits. Reading the store can take seconds (a connection, a query);
//!    doing that on the menu thread freezes the menu bar, and on macOS the whole NSApplication
//!    run loop with it. Everything slow lives on a worker thread and sends its result back.
//! 2. A failure is visible. The menu-bar title and the first menu line say when the data did not
//!    arrive, instead of letting yesterday's numbers look current. A console whose stale state
//!    is indistinguishable from its fresh state is worse than no console.

use std::sync::mpsc;
use std::time::{Duration, Instant, SystemTime};

use hypermnesia_console::jobs::{self, Job};
use hypermnesia_console::{fetch, Stats, Target};

use tray_icon::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
use tray_icon::{TrayIcon, TrayIconBuilder};
use winit::application::ApplicationHandler;
use winit::event_loop::{ActiveEventLoop, ControlFlow, EventLoop};

/// How often to refresh on its own. A minute: the numbers move slowly and every reading is a
/// round trip to the store.
const REFRESH: Duration = Duration::from_secs(60);

/// How long the worker may be silent after being asked something before the menu says so. The
/// store's own timeout is 30 s by default, so this is well past any normal answer: the point is
/// to notice a worker that will never answer at all, not to hurry a slow one.
const WORKER_PATIENCE: Duration = Duration::from_secs(90);

/// Ready-made schedules offered in the menu. Exactly what is usually wanted and nothing more:
/// the rare case belongs on the command line.
const PRESETS: &[(&str, jobs::Schedule)] = &[
    ("hourly", jobs::Schedule::Every(3600)),
    ("every 4 hours", jobs::Schedule::Every(14_400)),
    ("every 12 hours", jobs::Schedule::Every(43_200)),
    ("daily at 05:30", jobs::Schedule::At { hour: 5, minute: 30, weekday: None }),
    ("weekly, Mon 06:10", jobs::Schedule::At { hour: 6, minute: 10, weekday: Some(1) }),
];

fn main() {
    // Install and uninstall run before the event loop exists: both finish immediately and ask
    // for no window.
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--install") => return finish(jobs::install_self(jobs::DEFAULT_TRAY_LABEL)),
        Some("--uninstall") => return finish(jobs::uninstall_self(jobs::DEFAULT_TRAY_LABEL)),
        Some("-h") | Some("--help") => {
            print!("{HELP}");
            return;
        }
        Some(other) => {
            eprintln!("hypermnesia: unknown argument: {other}");
            std::process::exit(1);
        }
        None => {}
    }

    let event_loop = EventLoop::builder().build().expect("event loop");
    event_loop.set_control_flow(ControlFlow::WaitUntil(Instant::now() + Duration::from_millis(200)));

    let (tx, rx) = mpsc::channel::<Update>();
    let (cmd_tx, cmd_rx) = mpsc::channel::<Command>();
    spawn_worker(tx, cmd_rx);
    // Read immediately: an empty menu at startup looks like a broken console.
    let _ = cmd_tx.send(Command::Refresh);

    let mut app = App {
        tray: None,
        items: Items::default(),
        rx,
        cmd: cmd_tx,
        last: None,
        status: "loading…".to_string(),
        note: None,
        awaiting: Some(("the first reading".into(), Instant::now())),
        worker_dead: false,
        last_refresh: Instant::now(),
    };
    if let Err(e) = event_loop.run_app(&mut app) {
        eprintln!("hypermnesia: the event loop ended: {e}");
    }
}

fn finish(r: Result<String, String>) {
    match r {
        Ok(msg) => println!("{msg}"),
        Err(e) => {
            eprintln!("hypermnesia: {e}");
            std::process::exit(1);
        }
    }
}

const HELP: &str = "\
hypermnesia — the memory console in the menu bar.

    hypermnesia              run the tray
    hypermnesia --install    add to autostart (launchd: at login, restarted if it crashes)
    hypermnesia --uninstall  remove from autostart

The numbers and the buttons are the same ones hypermnesia-stats and hypermnesia-jobs give: the
tray calls the same functions. Connection settings come from hypermnesia-setup.
";

/// What the worker thread sends back.
enum Update {
    Data(Box<Snapshot>),
    Failed(String),
    /// The outcome of a button press: a line to show at the top of the menu.
    Note(String),
}

struct Snapshot {
    stats: Stats,
    jobs: Vec<Job>,
    /// Why the job list is missing, if it is. `unwrap_or_default()` used to turn "could not read
    /// LaunchAgents" into an empty list, which rendered as empty Run-now and Schedule submenus
    /// and a calm title -- "no jobs readable" and "every job healthy" looked identical.
    jobs_error: Option<String>,
    /// Wall clock, not `Instant`: the age of the data has to survive the Mac going to sleep, and
    /// a monotonic clock stops while it does. That made a two-hour nap look like a fresh reading.
    at: SystemTime,
}

enum Command {
    Refresh,
    RunJob(String),
    SetSchedule(String, jobs::Schedule),
}

/// The worker: every slow thing lives here. The main thread only posts commands to it.
fn spawn_worker(tx: mpsc::Sender<Update>, rx: mpsc::Receiver<Command>) {
    std::thread::spawn(move || {
        let read_all = |tx: &mpsc::Sender<Update>| {
            // Re-read the settings on every pass rather than once at startup. The tray runs for
            // weeks; a connection fixed with the setup wizard in the meantime has to reach the
            // running tray, or the fix looks like it did not work.
            let target = Target::default();
            let prefix = jobs::job_prefix();
            // Jobs are read locally and fast; the store is read over whatever transport was
            // configured and may be slow. If the store does not answer, the jobs are still worth
            // showing.
            let (job_list, jobs_error) = match jobs::list(&prefix) {
                Ok(j) => (j, None),
                Err(e) => (Vec::new(), Some(e)),
            };
            match fetch(&target) {
                Ok(stats) => {
                    let _ = tx.send(Update::Data(Box::new(Snapshot {
                        stats, jobs: job_list, jobs_error, at: SystemTime::now(),
                    })));
                }
                Err(e) => { let _ = tx.send(Update::Failed(e)); }
            }
        };
        while let Ok(cmd) = rx.recv() {
            match cmd {
                Command::Refresh => read_all(&tx),
                Command::RunJob(label) => {
                    let msg = match jobs::run_now(&label) {
                        Ok(m) => m,
                        // Marked, like every other failure: the title reads the leading "!".
                        Err(e) => format!("! did not start: {e}"),
                    };
                    let _ = tx.send(Update::Note(msg));
                    read_all(&tx);
                }
                Command::SetSchedule(label, sched) => {
                    // Look the job up again: the list the menu was drawn from may be a minute
                    // old, and editing a schedule from a stale record means editing something
                    // other than what the person saw.
                    let msg = match jobs::list(&jobs::job_prefix()).ok()
                        .and_then(|all| all.into_iter().find(|j| j.label == label)) {
                        Some(j) => match jobs::set_schedule(&j, &sched) {
                            Ok(m) => m,
                            Err(e) => format!("! {e}"),
                        },
                        None => format!("! the job {label} is gone"),
                    };
                    let _ = tx.send(Update::Note(msg));
                    read_all(&tx);
                }
            }
        }
    });
}

/// The menu items we later act on. The menu is rebuilt whole on every change: there are dozens
/// of items, not thousands, and rebuilding wholesale rules out a label disagreeing with its data
/// -- a partly updated menu is the same class of quiet lie as everything else this fixes.
#[derive(Default)]
struct Items {
    refresh: Option<MenuItem>,
    quit: Option<MenuItem>,
    run: Vec<(MenuItem, String)>,
    sched: Vec<(MenuItem, String, jobs::Schedule)>,
}

struct App {
    tray: Option<TrayIcon>,
    items: Items,
    rx: mpsc::Receiver<Update>,
    cmd: mpsc::Sender<Command>,
    last: Option<Snapshot>,
    status: String,
    /// The outcome of the last button press, on its own line and with its own age.
    ///
    /// It used to share one field with the status line, and the refresh that every button starts
    /// overwrote it a moment later -- so a "Run now" that failed ended up reading "updated just
    /// now". The outcome of something a person did is the last thing that should be overwritten.
    note: Option<(String, SystemTime)>,
    /// What the worker was last asked, and when. Cleared by any answer.
    awaiting: Option<(String, Instant)>,
    /// The worker thread is gone: nothing will ever be read again.
    worker_dead: bool,
    last_refresh: Instant,
}

impl ApplicationHandler for App {
    fn resumed(&mut self, _: &ActiveEventLoop) {
        if self.tray.is_none() {
            self.rebuild();
        }
    }

    fn window_event(&mut self, _: &ActiveEventLoop, _: winit::window::WindowId,
                    _: winit::event::WindowEvent) {}

    fn about_to_wait(&mut self, el: &ActiveEventLoop) {
        // Not one blocking call here: a non-blocking channel read and a timer.
        let mut dirty = false;
        loop {
            match self.rx.try_recv() {
                Ok(u) => {
                    self.awaiting = None;
                    match u {
                        Update::Data(s) => {
                            self.status = format!("updated just now, in {:.1}s",
                                                  s.stats.took.as_secs_f32());
                            self.last = Some(*s);
                        }
                        Update::Failed(e) => {
                            // The old numbers stay -- they beat an empty screen -- but they are
                            // labelled with the reason and with HOW OLD they are. Without the age
                            // you cannot tell "could not reach it a second ago" from "stuck since
                            // yesterday".
                            let age = self.last.as_ref()
                                .map(|s| format!(", showing state from {} ago", ago(age_of(s.at))))
                                .unwrap_or_default();
                            self.status = format!("! not updated: {e}{age}");
                        }
                        Update::Note(n) => self.note = Some((n, SystemTime::now())),
                    }
                    dirty = true;
                }
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => {
                    // The worker thread is gone. Nothing will be read again, and the alternative
                    // to saying so is a menu that keeps showing a reading from the moment it died.
                    if !self.worker_dead {
                        self.worker_dead = true;
                        self.status = "! the reader thread has died -- nothing here will update \
                                       again; quit and start the tray anew".into();
                        dirty = true;
                    }
                    break;
                }
            }
        }

        // A worker that is alive but has stopped answering. Without this the menu keeps asserting
        // "updated just now" over a reading that never arrived.
        if let Some((what, since)) = self.awaiting.clone() {
            if since.elapsed() > WORKER_PATIENCE && !self.status.starts_with("! no answer") {
                self.status = format!("! no answer about {what} for {} -- the reader is stuck",
                                      ago(since.elapsed()));
                dirty = true;
            }
        }

        while let Ok(ev) = MenuEvent::receiver().try_recv() {
            if Some(&ev.id) == self.items.quit.as_ref().map(|i| i.id()) {
                el.exit();
                return;
            }
            if Some(&ev.id) == self.items.refresh.as_ref().map(|i| i.id()) {
                self.ask(Command::Refresh, "refreshing…", "a reading");
                dirty = true;
                continue;
            }
            if let Some((_, label)) = self.items.run.iter().find(|(i, _)| i.id() == &ev.id) {
                let (label, what) = (label.clone(), format!("running {label}"));
                self.ask(Command::RunJob(label), &format!("{what}…"), &what);
                dirty = true;
                continue;
            }
            if let Some((_, label, sched)) =
                self.items.sched.iter().find(|(i, _, _)| i.id() == &ev.id) {
                let (label, sched) = (label.clone(), sched.clone());
                let what = format!("the schedule of {label}");
                self.ask(Command::SetSchedule(label, sched), &format!("changing {what}…"), &what);
                dirty = true;
                continue;
            }
            // A press on an item from a menu that has since been rebuilt: the ids are new, so
            // nothing above matched. Silently dropping it left a person pressing a button that
            // did nothing, with no way to tell that from a button that did nothing visible.
            self.note = Some(("that button was from an older copy of the menu -- press it again"
                              .into(), SystemTime::now()));
            dirty = true;
        }

        if self.last_refresh.elapsed() >= REFRESH {
            self.last_refresh = Instant::now();
            self.ask(Command::Refresh, &self.status.clone(), "a reading");
        }
        if dirty {
            self.rebuild();
        }
        el.set_control_flow(ControlFlow::WaitUntil(Instant::now() + Duration::from_millis(200)));
    }
}

impl App {
    /// Post a command and remember that an answer is owed. Every path to the worker goes through
    /// here so that nothing can be asked without the watchdog knowing about it.
    fn ask(&mut self, cmd: Command, status: &str, what: &str) {
        self.status = status.to_string();
        self.awaiting = Some((what.to_string(), Instant::now()));
        let _ = self.cmd.send(cmd);
    }

    fn rebuild(&mut self) {
        let menu = Menu::new();
        let mut items = Items::default();

        let _ = menu.append(&MenuItem::new(&self.status, false, None));
        // The outcome of the last button press keeps its own line, with its age, until another
        // press replaces it.
        if let Some((note, when)) = &self.note {
            let _ = menu.append(&MenuItem::new(format!("{note}  ({} ago)", ago(age_of(*when))),
                                               false, None));
        }
        if let Some(why) = jobs::autostart_fault(jobs::DEFAULT_TRAY_LABEL) {
            let _ = menu.append(&MenuItem::new(format!("! {why}"), false, None));
        }
        let _ = menu.append(&PredefinedMenuItem::separator());

        if let Some(s) = &self.last {
            for line in summary(&s.stats) {
                let _ = menu.append(&MenuItem::new(line, false, None));
            }
            let _ = menu.append(&PredefinedMenuItem::separator());

            if let Some(why) = &s.jobs_error {
                let _ = menu.append(&MenuItem::new(format!("! jobs unreadable: {why}"),
                                                   false, None));
            }
            let run = Submenu::new("Run now", true);
            for j in &s.jobs {
                let item = MenuItem::new(job_line(j), true, None);
                let _ = run.append(&item);
                items.run.push((item, j.label.clone()));
            }
            let _ = menu.append(&run);

            let sched = Submenu::new("Schedule", true);
            for j in &s.jobs {
                if matches!(j.schedule, jobs::Schedule::None) || j.broken() {
                    continue;               // nothing to change, or nothing readable to change
                }
                let sub = Submenu::new(format!("{} ({})", j.short(), j.schedule.human()), true);
                for (label, preset) in PRESETS {
                    let item = MenuItem::new(*label, true, None);
                    let _ = sub.append(&item);
                    items.sched.push((item, j.label.clone(), preset.clone()));
                }
                let _ = sched.append(&sub);
            }
            let _ = menu.append(&sched);
        }

        let _ = menu.append(&PredefinedMenuItem::separator());
        let refresh = MenuItem::new("Refresh now", true, None);
        let _ = menu.append(&refresh);
        items.refresh = Some(refresh);
        let quit = MenuItem::new("Quit", true, None);
        let _ = menu.append(&quit);
        items.quit = Some(quit);

        self.items = items;
        let title = self.title();           // computed before borrowing self.tray
        match self.tray.as_mut() {
            Some(t) => {
                t.set_menu(Some(Box::new(menu)));
                let _ = t.set_title(Some(title));
            }
            None => {
                // Not `.ok()`: an icon that never appeared leaves a process running with no way
                // to see anything, and launchd counts it as healthy. Better to say so and stop.
                match TrayIconBuilder::new()
                    .with_menu(Box::new(menu))
                    .with_title(title)
                    .with_tooltip("HyperMnesia")
                    .build() {
                    Ok(t) => self.tray = Some(t),
                    Err(e) => {
                        eprintln!("hypermnesia: the menu-bar icon could not be created: {e}");
                        std::process::exit(1);
                    }
                }
            }
        }
    }

    /// What is visible without opening the menu. A mark matters more than a number here: the
    /// menu bar is where a problem is NOTICED, not where a report is read.
    fn title(&self) -> String {
        if self.status.starts_with('!') || self.note.as_ref().is_some_and(|(n, _)| n.starts_with('!')) {
            return "HM !".into();
        }
        match &self.last {
            None => "HM …".into(),
            Some(s) => {
                // A non-zero exit code is deliberately NOT a mark here: the freshness job exits 1
                // to mean "discrepancies found", by design, and a permanent "!" is a "!" nobody
                // looks at. The code is on the job's own line instead.
                let warn = s.stats.review_pending > 0
                    || s.stats.embedding_models.len() > 1
                    || s.jobs_error.is_some()
                    || s.jobs.iter().any(Job::broken)
                    || s.jobs.iter().any(Job::overdue);
                format!("HM{}", if warn { " !" } else { "" })
            }
        }
    }
}

fn summary(s: &Stats) -> Vec<String> {
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
    out.push(format!("Review queue: {}{}", s.review_pending,
                     if s.review_pending > 0 {
                         format!(" (oldest {} days)", s.review_oldest_days)
                     } else { String::new() }));
    out.push(format!("Stale: {}", s.stale));
    out.push(format!("Database: {}", s.db_size));
    out
}

/// How old something is, by the wall clock. `Instant` would stop while the Mac sleeps.
fn age_of(t: SystemTime) -> Duration {
    SystemTime::now().duration_since(t).unwrap_or_default()
}

fn ago(d: Duration) -> String {
    let s = d.as_secs();
    if s < 90 { format!("{s}s") }
    else if s < 5400 { format!("{}m", s / 60) }
    else if s < 172_800 { format!("{}h", s / 3600) }
    else { format!("{}d", s / 86_400) }
}

fn job_line(j: &Job) -> String {
    if let Some(why) = &j.fault {
        // launchd refused this plist too, so the job is not running. It used to be missing from
        // the menu entirely.
        let first = why.lines().next().unwrap_or(why);
        return format!("{}  (! unreadable plist: {first})", j.short());
    }
    let state = if j.running() {
        "running".to_string()
    } else if j.overdue() {
        "! overdue".to_string()
    } else if j.never_ran() {
        "never run yet".to_string()
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
    use std::path::PathBuf;

    fn job(schedule: jobs::Schedule) -> Job {
        Job {
            label: "com.hypermnesia.extract".into(), plist: PathBuf::new(), schedule,
            program: vec![], log: Some(PathBuf::from("/nope")), last_exit: Some(0), pid: None,
            last_output: Some(SystemTime::now() - Duration::from_secs(300)), runs: Some(9),
            installed: Some(SystemTime::now() - Duration::from_secs(90_000)), fault: None,
        }
    }

    /// A job that fails on every run used to read exactly like one that works: same schedule,
    /// same fresh log timestamp, no code anywhere in the menu.
    #[test]
    fn a_failing_job_says_so_in_its_own_line() {
        let mut j = job(jobs::Schedule::Every(14_400));
        assert_eq!(job_line(&j), "extract  (every 4 h, 5m ago)");

        j.last_exit = Some(2);
        let line = job_line(&j);
        assert!(line.contains("exit 2"), "the exit code has to be on the line: {line}");
        assert!(line.starts_with("extract  (every 4 h, !"), "and marked: {line}");
    }

    /// An unreadable plist is one launchd also refused, so the job is not running. It used to be
    /// absent from the menu altogether.
    #[test]
    fn an_unreadable_plist_is_a_visible_line() {
        let mut j = job(jobs::Schedule::None);
        j.fault = Some("plutil: unexpected character\nsecond line".into());
        let line = job_line(&j);
        assert!(line.contains("unreadable plist"), "{line}");
        assert!(!line.contains("second line"), "one line only, it is a menu: {line}");
    }

    #[test]
    fn a_job_never_run_is_not_called_fresh() {
        let mut j = job(jobs::Schedule::At { hour: 6, minute: 10, weekday: Some(1) });
        j.runs = Some(0);
        j.last_output = None;
        assert!(job_line(&j).contains("never run yet"), "{}", job_line(&j));
    }
}
