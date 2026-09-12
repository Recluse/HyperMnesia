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
use std::time::{Duration, Instant};

use hypermnesia_console::jobs::{self, Job};
use hypermnesia_console::{fetch, Stats, Target};

use tray_icon::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
use tray_icon::{TrayIcon, TrayIconBuilder};
use winit::application::ApplicationHandler;
use winit::event_loop::{ActiveEventLoop, ControlFlow, EventLoop};

/// How often to refresh on its own. A minute: the numbers move slowly and every reading is a
/// round trip to the store.
const REFRESH: Duration = Duration::from_secs(60);

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
    hypermnesia --install    add to autostart (launchd, RunAtLoad + KeepAlive)
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
    at: Instant,
}

enum Command {
    Refresh,
    RunJob(String),
    SetSchedule(String, jobs::Schedule),
}

/// The worker: every slow thing lives here. The main thread only posts commands to it.
fn spawn_worker(tx: mpsc::Sender<Update>, rx: mpsc::Receiver<Command>) {
    std::thread::spawn(move || {
        let target = Target::default();
        let prefix = jobs::job_prefix();
        let read_all = |tx: &mpsc::Sender<Update>| {
            // Jobs are read locally and fast; the store is read over whatever transport was
            // configured and may be slow. If the store does not answer, the jobs are still worth
            // showing.
            let job_list = jobs::list(&prefix).unwrap_or_default();
            match fetch(&target) {
                Ok(stats) => {
                    let _ = tx.send(Update::Data(Box::new(Snapshot {
                        stats, jobs: job_list, at: Instant::now(),
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
                        Err(e) => format!("did not start: {e}"),
                    };
                    let _ = tx.send(Update::Note(msg));
                    read_all(&tx);
                }
                Command::SetSchedule(label, sched) => {
                    // Look the job up again: the list the menu was drawn from may be a minute
                    // old, and editing a schedule from a stale record means editing something
                    // other than what the person saw.
                    let msg = match jobs::list(&prefix).ok()
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
        while let Ok(u) = self.rx.try_recv() {
            match u {
                Update::Data(s) => {
                    self.status = format!("updated just now, in {:.1}s", s.stats.took.as_secs_f32());
                    self.last = Some(*s);
                }
                Update::Failed(e) => {
                    // The old numbers stay -- they beat an empty screen -- but they are labelled
                    // with the reason and with HOW OLD they are. Without the age you cannot tell
                    // "could not reach it a second ago" from "stuck since yesterday".
                    let age = self.last.as_ref()
                        .map(|s| format!(", showing state from {} ago", ago(s.at.elapsed())))
                        .unwrap_or_default();
                    self.status = format!("! not updated: {e}{age}");
                }
                Update::Note(n) => self.status = n,
            }
            dirty = true;
        }

        while let Ok(ev) = MenuEvent::receiver().try_recv() {
            if Some(&ev.id) == self.items.quit.as_ref().map(|i| i.id()) {
                el.exit();
                return;
            }
            if Some(&ev.id) == self.items.refresh.as_ref().map(|i| i.id()) {
                self.status = "refreshing…".into();
                let _ = self.cmd.send(Command::Refresh);
                dirty = true;
                continue;
            }
            if let Some((_, label)) = self.items.run.iter().find(|(i, _)| i.id() == &ev.id) {
                self.status = format!("running {label}…");
                let _ = self.cmd.send(Command::RunJob(label.clone()));
                dirty = true;
                continue;
            }
            if let Some((_, label, sched)) =
                self.items.sched.iter().find(|(i, _, _)| i.id() == &ev.id) {
                self.status = format!("changing the schedule of {label}…");
                let _ = self.cmd.send(Command::SetSchedule(label.clone(), sched.clone()));
                dirty = true;
            }
        }

        if self.last_refresh.elapsed() >= REFRESH {
            self.last_refresh = Instant::now();
            let _ = self.cmd.send(Command::Refresh);
        }
        if dirty {
            self.rebuild();
        }
        el.set_control_flow(ControlFlow::WaitUntil(Instant::now() + Duration::from_millis(200)));
    }
}

impl App {
    fn rebuild(&mut self) {
        let menu = Menu::new();
        let mut items = Items::default();

        let _ = menu.append(&MenuItem::new(&self.status, false, None));
        let _ = menu.append(&PredefinedMenuItem::separator());

        if let Some(s) = &self.last {
            for line in summary(&s.stats) {
                let _ = menu.append(&MenuItem::new(line, false, None));
            }
            let _ = menu.append(&PredefinedMenuItem::separator());

            let run = Submenu::new("Run now", true);
            for j in &s.jobs {
                let item = MenuItem::new(job_line(j), true, None);
                let _ = run.append(&item);
                items.run.push((item, j.label.clone()));
            }
            let _ = menu.append(&run);

            let sched = Submenu::new("Schedule", true);
            for j in &s.jobs {
                if matches!(j.schedule, jobs::Schedule::None) {
                    continue;               // a job with no schedule has nothing to change
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
                self.tray = TrayIconBuilder::new()
                    .with_menu(Box::new(menu))
                    .with_title(title)
                    .with_tooltip("HyperMnesia")
                    .build()
                    .ok();
            }
        }
    }

    /// What is visible without opening the menu. A mark matters more than a number here: the
    /// menu bar is where a problem is NOTICED, not where a report is read.
    fn title(&self) -> String {
        if self.status.starts_with('!') {
            return "HM !".into();
        }
        match &self.last {
            None => "HM …".into(),
            Some(s) => {
                let warn = s.stats.review_pending > 0
                    || s.stats.embedding_models.len() > 1
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

fn ago(d: Duration) -> String {
    let s = d.as_secs();
    if s < 90 { format!("{s}s") }
    else if s < 5400 { format!("{}m", s / 60) }
    else if s < 172_800 { format!("{}h", s / 3600) }
    else { format!("{}d", s / 86_400) }
}

fn job_line(j: &Job) -> String {
    let state = if j.running() {
        "running".to_string()
    } else if j.overdue() {
        "! overdue".to_string()
    } else if j.runs == Some(0) {
        "never run yet".to_string()
    } else {
        match j.since_last_output() {
            Some(d) => format!("{} ago", ago(d)),
            None => "—".to_string(),
        }
    };
    format!("{}  ({}, {})", j.short(), j.schedule.human(), state)
}
