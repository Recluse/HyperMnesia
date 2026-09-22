//! The memory console in the menu bar / system tray.
//!
//! A menu, not a window. Everything this console has to show is a dozen lines of state and a
//! dozen buttons; a window would mean a GUI framework for the same result.
//!
//! Two rules everything else follows from:
//!
//! 1. The main thread never waits. Reading the store can take seconds (a connection, a query);
//!    doing that on the menu thread freezes the menu, and on macOS the whole NSApplication run
//!    loop with it. Everything slow lives on a worker thread and sends its result back.
//! 2. A failure is visible. The menu-bar title and the first menu line say when the data did not
//!    arrive, instead of letting yesterday's numbers look current. A console whose stale state
//!    is indistinguishable from its fresh state is worse than no console.
//!
//! `mod app` is everything above the event loop: state, the worker thread, and how the menu is
//! rendered from `hypermnesia_console::view`. It is shared, unchanged, by every platform this
//! binary supports, because none of it is platform-specific -- reading the store and reading the
//! job list are already portable, and `tray_icon`'s `Menu`/`MenuItem`/`TrayIcon` API is the same
//! crate on macOS and on Linux. Only *driving* that API differs: macOS needs a winit
//! `ApplicationHandler` and its NSApplication run loop; Linux needs a GTK main loop, since that is
//! what `tray-icon`'s Linux backend (`libayatana-appindicator`) is built on. `mod mac` and
//! `mod linux` hold exactly that seam and nothing else.

#[cfg(any(target_os = "macos", all(target_os = "linux", feature = "tray")))]
mod app {
    use std::sync::mpsc;
    use std::time::{Duration, Instant, SystemTime};

    use hypermnesia_console::jobs::{self, Job};
    use hypermnesia_console::view;
    use hypermnesia_console::{fetch, Stats, Target};

    use tray_icon::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
    use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

    /// How often to refresh on its own. A minute: the numbers move slowly and every reading is a
    /// round trip to the store.
    pub const REFRESH: Duration = Duration::from_secs(60);

    /// How long the worker may be silent after being asked something before the menu says so. The
    /// store's own timeout is 30 s by default, so this is well past any normal answer: the point
    /// is to notice a worker that will never answer at all, not to hurry a slow one.
    const WORKER_PATIENCE: Duration = Duration::from_secs(90);

    /// Ready-made schedules offered in the menu. Exactly what is usually wanted and nothing more:
    /// the rare case belongs on the command line -- where, on Linux today, it is the only place:
    /// `jobs::set_schedule` refuses with "not implemented on Linux yet", and picking one of these
    /// items just shows that refusal in the note line rather than silently doing nothing.
    const PRESETS: &[(&str, jobs::Schedule)] = &[
        ("hourly", jobs::Schedule::Every(3600)),
        ("every 4 hours", jobs::Schedule::Every(14_400)),
        ("every 12 hours", jobs::Schedule::Every(43_200)),
        ("daily at 05:30", jobs::Schedule::Calendar(jobs::Cal { hour: Some(5), minute: Some(30), day: None, weekday: None, month: None })),
        ("weekly, Mon 06:10", jobs::Schedule::Calendar(jobs::Cal { hour: Some(6), minute: Some(10), day: None, weekday: Some(1), month: None })),
    ];

    pub const HELP: &str = "\
hypermnesia — the memory console in the menu bar / system tray.

    hypermnesia              run the tray
    hypermnesia --install    add to autostart, where supported
    hypermnesia --uninstall  remove from autostart, where supported

The numbers and the buttons are the same ones hypermnesia-stats and hypermnesia-jobs give: the
tray calls the same functions. Connection settings come from hypermnesia-setup. Autostart and
schedule editing are launchd-only for now; on Linux those actions say so rather than doing
nothing silently.
";

    /// Handle `--install` / `--uninstall` / `--help` before any window or GTK loop exists: all
    /// three finish immediately and ask for no window. Returns `true` if one of them was handled
    /// (the caller should return without building the tray), `false` to proceed.
    pub fn handle_early_args(args: &[String]) -> bool {
        match args.first().map(String::as_str) {
            Some("--install") => { finish(jobs::install_self(jobs::DEFAULT_TRAY_LABEL)); true }
            Some("--uninstall") => { finish(jobs::uninstall_self(jobs::DEFAULT_TRAY_LABEL)); true }
            Some("-h") | Some("--help") => { print!("{HELP}"); true }
            Some(other) => {
                eprintln!("hypermnesia: unknown argument: {other}");
                std::process::exit(1);
            }
            None => false,
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

    /// What the worker thread sends back.
    pub enum Update {
        Data(Box<Snapshot>),
        Failed(String),
        /// The outcome of a button press: a line to show at the top of the menu.
        Note(String),
    }

    pub struct Snapshot {
        stats: Stats,
        jobs: Vec<Job>,
        /// Why the job list is missing, if it is. `unwrap_or_default()` used to turn "could not
        /// read the job directory" into an empty list, which rendered as empty Run-now and
        /// Schedule submenus and a calm title -- "no jobs readable" and "every job healthy"
        /// looked identical.
        jobs_error: Option<String>,
        /// Wall clock, not `Instant`: the age of the data has to survive the machine sleeping,
        /// and a monotonic clock stops while it does. That made a two-hour nap look like a fresh
        /// reading.
        at: SystemTime,
    }

    pub enum Command {
        Refresh,
        RunJob(String),
        SetSchedule(String, jobs::Schedule),
    }

    /// The worker: every slow thing lives here. The main thread only posts commands to it.
    pub fn spawn_worker(tx: mpsc::Sender<Update>, rx: mpsc::Receiver<Command>) {
        std::thread::spawn(move || {
            let read_all = |tx: &mpsc::Sender<Update>| {
                // Re-read the settings on every pass rather than once at startup. The tray runs
                // for weeks; a connection fixed with the setup wizard in the meantime has to
                // reach the running tray, or the fix looks like it did not work.
                let target = Target::default();
                let prefix = jobs::job_prefix();
                // Jobs are read locally and fast; the store is read over whatever transport was
                // configured and may be slow. If the store does not answer, the jobs are still
                // worth showing.
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
                        // Look the job up again: the list the menu was drawn from may be a
                        // minute old, and editing a schedule from a stale record means editing
                        // something other than what the person saw.
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

    /// The menu items we later act on. The menu is rebuilt whole on every change: there are
    /// dozens of items, not thousands, and rebuilding wholesale rules out a label disagreeing
    /// with its data -- a partly updated menu is the same class of quiet lie as everything else
    /// this fixes.
    #[derive(Default)]
    struct Items {
        refresh: Option<MenuItem>,
        quit: Option<MenuItem>,
        run: Vec<(MenuItem, String)>,
        sched: Vec<(MenuItem, String, jobs::Schedule)>,
    }

    pub struct App {
        tray: Option<TrayIcon>,
        items: Items,
        rx: mpsc::Receiver<Update>,
        cmd: mpsc::Sender<Command>,
        last: Option<Snapshot>,
        status: String,
        /// The outcome of the last button press, on its own line and with its own age.
        ///
        /// It used to share one field with the status line, and the refresh that every button
        /// starts overwrote it a moment later -- so a "Run now" that failed ended up reading
        /// "updated just now". The outcome of something a person did is the last thing that
        /// should be overwritten.
        note: Option<(String, SystemTime)>,
        /// What the worker was last asked, and when. Cleared by any answer.
        awaiting: Option<(String, Instant)>,
        /// The worker thread is gone: nothing will ever be read again.
        worker_dead: bool,
        last_refresh: Instant,
    }

    /// What a platform's event loop should do after a tick.
    pub enum Tick {
        /// Nothing to do until the next scheduled wake-up.
        Continue,
        /// The person chose Quit.
        Quit,
    }

    impl App {
        pub fn new(rx: mpsc::Receiver<Update>, cmd: mpsc::Sender<Command>) -> Self {
            App {
                tray: None,
                items: Items::default(),
                rx,
                cmd,
                last: None,
                status: "loading…".to_string(),
                note: None,
                awaiting: Some(("the first reading".into(), Instant::now())),
                worker_dead: false,
                last_refresh: Instant::now(),
            }
        }

        /// Post a command and remember that an answer is owed. Every path to the worker goes
        /// through here so that nothing can be asked without the watchdog knowing about it.
        fn ask(&mut self, cmd: Command, status: &str, what: &str) {
            self.status = status.to_string();
            self.awaiting = Some((what.to_string(), Instant::now()));
            let _ = self.cmd.send(cmd);
        }

        /// Not one blocking call in here: a non-blocking channel read and a timer. Called on a
        /// ~200ms tick by whichever platform loop is driving this `App`.
        pub fn tick(&mut self) -> Tick {
            if self.tray.is_none() {
                self.rebuild();
            }

            let mut dirty = false;
            loop {
                match self.rx.try_recv() {
                    Ok(u) => {
                        self.awaiting = None;
                        match u {
                            Update::Data(s) => {
                                // Not a sentence: the sentence is rendered at rebuild time from
                                // `last.at`. Frozen, it said "updated just now" for the whole
                                // refresh cycle -- and after the machine slept, for as long as
                                // the nap lasted, because the refresh timer is monotonic and
                                // stops with it.
                                self.status = String::new();
                                self.last = Some(*s);
                            }
                            Update::Failed(e) => {
                                // The old numbers stay -- they beat an empty screen -- but they
                                // are labelled with the reason and with HOW OLD they are.
                                // Without the age you cannot tell "could not reach it a second
                                // ago" from "stuck since yesterday".
                                let age = self.last.as_ref()
                                    .map(|s| format!(", showing state from {} ago",
                                                     view::ago(view::age_of(s.at))))
                                    .unwrap_or_default();
                                self.status = format!("! not updated: {e}{age}");
                            }
                            Update::Note(n) => self.note = Some((n, SystemTime::now())),
                        }
                        dirty = true;
                    }
                    Err(mpsc::TryRecvError::Empty) => break,
                    Err(mpsc::TryRecvError::Disconnected) => {
                        // The worker thread is gone. Nothing will be read again, and the
                        // alternative to saying so is a menu that keeps showing a reading from
                        // the moment it died.
                        if !self.worker_dead {
                            self.worker_dead = true;
                            self.status = "! the reader thread has died -- nothing here will \
                                           update again; quit and start the tray anew".into();
                            dirty = true;
                        }
                        break;
                    }
                }
            }

            // A worker that is alive but has stopped answering. Without this the menu keeps
            // asserting "updated just now" over a reading that never arrived.
            if let Some((what, since)) = self.awaiting.clone() {
                if since.elapsed() > WORKER_PATIENCE && !self.status.starts_with("! no answer") {
                    self.status = format!("! no answer about {what} for {} -- the reader is \
                                           stuck", view::ago(since.elapsed()));
                    dirty = true;
                }
            }

            while let Ok(ev) = MenuEvent::receiver().try_recv() {
                if Some(&ev.id) == self.items.quit.as_ref().map(|i| i.id()) {
                    return Tick::Quit;
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
                    self.ask(Command::SetSchedule(label, sched), &format!("changing {what}…"),
                             &what);
                    dirty = true;
                    continue;
                }
                // A press on an item from a menu that has since been rebuilt: the ids are new,
                // so nothing above matched. Silently dropping it left a person pressing a button
                // that did nothing, with no way to tell that from a button that did nothing
                // visible.
                self.note = Some(("that button was from an older copy of the menu -- press it \
                                   again".into(), SystemTime::now()));
                dirty = true;
            }

            if self.last_refresh.elapsed() >= REFRESH {
                self.last_refresh = Instant::now();
                self.ask(Command::Refresh, &self.status.clone(), "a reading");
            }
            if dirty {
                self.rebuild();
            }
            Tick::Continue
        }

        fn rebuild(&mut self) {
            let menu = Menu::new();
            let mut items = Items::default();

            let _ = menu.append(&MenuItem::new(self.status_line(), false, None));
            // The outcome of the last button press keeps its own line, with its age, until
            // another press replaces it.
            if let Some((note, when)) = &self.note {
                let _ = menu.append(&MenuItem::new(
                    format!("{note}  ({} ago)", view::ago(view::age_of(*when))), false, None));
            }
            if let Some(why) = jobs::autostart_fault(jobs::DEFAULT_TRAY_LABEL) {
                let _ = menu.append(&MenuItem::new(format!("! {why}"), false, None));
            }
            let _ = menu.append(&PredefinedMenuItem::separator());

            let mut warned = false;
            if let Some(s) = &self.last {
                for line in view::summary(&s.stats) {
                    let _ = menu.append(&MenuItem::new(line, false, None));
                }
                let _ = menu.append(&PredefinedMenuItem::separator());

                if let Some(why) = &s.jobs_error {
                    let _ = menu.append(&MenuItem::new(format!("! jobs unreadable: {why}"),
                                                       false, None));
                }
                let run = Submenu::new("Run now", true);
                for j in &s.jobs {
                    let item = MenuItem::new(view::job_line(j), true, None);
                    let _ = run.append(&item);
                    items.run.push((item, j.label.clone()));
                }
                let _ = menu.append(&run);

                let sched = Submenu::new("Schedule", true);
                for j in &s.jobs {
                    if matches!(j.schedule, jobs::Schedule::None) || j.broken() {
                        continue;           // nothing to change, or nothing readable to change
                    }
                    let sub = Submenu::new(format!("{} ({})", j.short(), j.schedule.human()),
                                           true);
                    for (label, preset) in PRESETS {
                        let item = MenuItem::new(*label, true, None);
                        let _ = sub.append(&item);
                        items.sched.push((item, j.label.clone(), preset.clone()));
                    }
                    let _ = sched.append(&sub);
                }
                let _ = menu.append(&sched);

                warned = view::warned(&s.stats, &s.jobs, &s.jobs_error);
            }
            // The status line and the last note are as much a part of "does this need a mark" as
            // the store's own numbers: a failed refresh has no `Snapshot` to derive a warning
            // from, and without this a dead worker or a failed refresh would sit under a calm
            // icon forever.
            warned = warned
                || self.status_line().starts_with('!')
                || self.note.as_ref().is_some_and(|(n, _)| n.starts_with('!'));

            let _ = menu.append(&PredefinedMenuItem::separator());
            let refresh = MenuItem::new("Refresh now", true, None);
            let _ = menu.append(&refresh);
            items.refresh = Some(refresh);
            let quit = MenuItem::new("Quit", true, None);
            let _ = menu.append(&quit);
            items.quit = Some(quit);

            self.items = items;
            let title = if self.last.is_none() && !warned { "HM …".to_string() }
                        else { format!("HM{}", if warned { " !" } else { "" }) };
            let icon = mark_icon(warned);
            match self.tray.as_ref() {
                Some(t) => {
                    t.set_menu(Some(Box::new(menu)));
                    // `set_title` only draws anything on macOS; harmless, and cheaper than a
                    // second cfg-gated code path, to call it everywhere and let the icon carry
                    // the mark where there is no menu-bar text to put it in.
                    let _ = t.set_title(Some(&title));
                    let _ = t.set_icon(Some(icon));
                }
                None => {
                    // Not `.ok()`: an icon that never appeared leaves a process running with no
                    // way to see anything, and the service manager counts it as healthy. Better
                    // to say so and stop.
                    match TrayIconBuilder::new()
                        .with_menu(Box::new(menu))
                        .with_icon(icon)
                        .with_title(title)
                        .with_tooltip("HyperMnesia")
                        .build() {
                        Ok(t) => self.tray = Some(t),
                        Err(e) => {
                            eprintln!("hypermnesia: the tray icon could not be created: {e}");
                            std::process::exit(1);
                        }
                    }
                }
            }
        }

        /// The first line of the menu: how old the numbers are, said in the present tense.
        ///
        /// Computed here rather than stored, because a stored sentence cannot age. `at` is wall
        /// clock, so a machine that slept for two hours reports two hours, not "just now".
        fn status_line(&self) -> String {
            if !self.status.is_empty() {
                return self.status.clone();
            }
            match &self.last {
                None => "loading…".into(),
                Some(s) => {
                    let age = view::age_of(s.at);
                    let took = s.stats.took.as_secs_f32();
                    if age < Duration::from_secs(5) {
                        format!("updated just now, in {took:.1}s")
                    } else {
                        format!("updated {} ago, in {took:.1}s", view::ago(age))
                    }
                }
            }
        }
    }

    /// A flat, solid-colour square: everything this tray's icon needs to say is "fine" or "not
    /// fine", which two colours already say without a single asset file. 22x22 is a comfortable
    /// size for a Linux system tray at typical DPI; macOS scales `set_icon`'s image itself.
    fn mark_icon(warned: bool) -> Icon {
        const SIZE: u32 = 22;
        let (r, g, b) = if warned { (196, 60, 48) } else { (52, 150, 90) };
        let mut rgba = Vec::with_capacity((SIZE * SIZE * 4) as usize);
        for _ in 0..(SIZE * SIZE) {
            rgba.extend_from_slice(&[r, g, b, 255]);
        }
        Icon::from_rgba(rgba, SIZE, SIZE).expect("a fixed-size solid RGBA buffer is always valid")
    }
}

// The tray needs a GUI toolkit: winit + tray-icon on macOS (declared unconditionally, since that
// is where the schedules it edits live), tray-icon + GTK on Linux (declared only behind the
// `tray` feature, so the data layer and the four CLI tools keep building on a machine with no GUI
// libraries at all -- which is what CI's bare runner is).
#[cfg(target_os = "macos")]
mod mac {
    use std::sync::mpsc;
    use std::time::Duration;

    use winit::application::ApplicationHandler;
    use winit::event_loop::{ActiveEventLoop, ControlFlow, EventLoop};

    use super::app::{self, App, Command, Tick, Update};

    pub fn main() {
        let args: Vec<String> = std::env::args().skip(1).collect();
        if app::handle_early_args(&args) {
            return;
        }

        let event_loop = EventLoop::builder().build().expect("event loop");
        event_loop.set_control_flow(ControlFlow::WaitUntil(
            std::time::Instant::now() + Duration::from_millis(200)));

        let (tx, rx) = mpsc::channel::<Update>();
        let (cmd_tx, cmd_rx) = mpsc::channel::<Command>();
        app::spawn_worker(tx, cmd_rx);
        // Read immediately: an empty menu at startup looks like a broken console.
        let _ = cmd_tx.send(Command::Refresh);

        let mut handler = Handler { app: App::new(rx, cmd_tx) };
        if let Err(e) = event_loop.run_app(&mut handler) {
            eprintln!("hypermnesia: the event loop ended: {e}");
        }
    }

    struct Handler {
        app: App,
    }

    impl ApplicationHandler for Handler {
        fn resumed(&mut self, _: &ActiveEventLoop) {
            // The first `tick()` builds the tray if it does not exist yet.
            let _ = self.app.tick();
        }

        fn window_event(&mut self, _: &ActiveEventLoop, _: winit::window::WindowId,
                        _: winit::event::WindowEvent) {}

        fn about_to_wait(&mut self, el: &ActiveEventLoop) {
            if let Tick::Quit = self.app.tick() {
                el.exit();
                return;
            }
            el.set_control_flow(ControlFlow::WaitUntil(
                std::time::Instant::now() + Duration::from_millis(200)));
        }
    }
}

#[cfg(all(target_os = "linux", feature = "tray"))]
mod linux {
    use std::cell::RefCell;
    use std::rc::Rc;
    use std::sync::mpsc;

    use super::app::{self, App, Command, Tick, Update};

    pub fn main() {
        let args: Vec<String> = std::env::args().skip(1).collect();
        if app::handle_early_args(&args) {
            return;
        }

        if gtk::init().is_err() {
            eprintln!("hypermnesia: could not initialize GTK -- is a display / Wayland or X11 \
                       session available?");
            std::process::exit(1);
        }

        let (tx, rx) = mpsc::channel::<Update>();
        let (cmd_tx, cmd_rx) = mpsc::channel::<Command>();
        app::spawn_worker(tx, cmd_rx);
        let _ = cmd_tx.send(Command::Refresh);

        let app = Rc::new(RefCell::new(App::new(rx, cmd_tx)));
        // The first tick builds the tray immediately: an empty menu bar at startup looks like a
        // broken console, same reasoning as `resumed()` on macOS.
        let _ = app.borrow_mut().tick();

        glib::source::timeout_add_local(std::time::Duration::from_millis(200), move || {
            match app.borrow_mut().tick() {
                Tick::Quit => {
                    gtk::main_quit();
                    glib::ControlFlow::Break
                }
                Tick::Continue => glib::ControlFlow::Continue,
            }
        });

        gtk::main();
    }
}

#[cfg(target_os = "macos")]
fn main() { mac::main() }

#[cfg(all(target_os = "linux", feature = "tray"))]
fn main() { linux::main() }

#[cfg(all(target_os = "linux", not(feature = "tray")))]
fn main() {
    eprintln!("hypermnesia: this build has no tray -- rebuild console with `--features tray` \
               (needs GTK3 and libayatana-appindicator development headers). \
               hypermnesia-stats, -jobs, -settings and -setup work as built.");
    std::process::exit(1);
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn main() {
    eprintln!("hypermnesia: the tray is built for macOS and Linux only. \
               hypermnesia-stats, -jobs, -settings and -setup work here.");
    std::process::exit(1);
}
