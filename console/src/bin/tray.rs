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
    use hypermnesia_console::view::{self, Severity, StatusLine};
    use hypermnesia_console::{fetch, Stats, Target};

    use tray_icon::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
    use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

    /// How often to refresh the store's own numbers on its own. A minute: they move slowly and
    /// every reading is a round trip over whatever transport the store is configured with.
    pub const REFRESH: Duration = Duration::from_secs(60);

    /// How often to re-read the jobs on their own, apart from the store. A local `systemctl`/
    /// `launchctl` call is cheap -- cheap enough that a person watching a job they just started
    /// should not be stuck behind the store's minute-long cadence to see it finish.
    const JOBS_REFRESH: Duration = Duration::from_secs(5);

    /// How long after a write command (run, schedule, enable/disable) to keep polling the jobs at
    /// the faster `BURST_REFRESH` cadence, so the result of pressing a button shows up in a
    /// couple of seconds rather than waiting out `JOBS_REFRESH`.
    const BURST_WINDOW: Duration = Duration::from_secs(10);
    const BURST_REFRESH: Duration = Duration::from_secs(1);

    /// How long the worker may be silent after being asked something before the menu says so. The
    /// store's own timeout is 30 s by default, so this is well past any normal answer: the point
    /// is to notice a worker that will never answer at all, not to hurry a slow one.
    const WORKER_PATIENCE: Duration = Duration::from_secs(90);

    /// Ready-made schedules offered in the menu. Exactly what is usually wanted and nothing more
    /// -- the rare case belongs on the command line, on every platform this tray runs on now.
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
    hypermnesia --status     print the current status and reasons without opening a tray
    hypermnesia --install    add to autostart
    hypermnesia --uninstall  remove from autostart

The numbers and the buttons are the same ones hypermnesia-stats and hypermnesia-jobs give: the
tray calls the same functions. Connection settings come from hypermnesia-setup. The store's own
numbers refresh every minute; the jobs -- a local, cheap read -- refresh every few seconds, faster
still for a little while after a button is pressed.
";

    /// Handle `--install` / `--uninstall` / `--help` before any window or GTK loop exists: all
    /// three finish immediately and ask for no window. Returns `true` if one of them was handled
    /// (the caller should return without building the tray), `false` to proceed.
    pub fn handle_early_args(args: &[String]) -> bool {
        match args.first().map(String::as_str) {
            Some("--status") => { print_status(); true }
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

    fn print_status() {
        let (jobs, error) = read_jobs();
        let reading = Target::from_env().and_then(|t| fetch(&t));
        let mut health = view::health(reading.as_ref().ok(), &jobs, error.as_deref());
        if let Err(why) = &reading {
            health.push(StatusLine::new(Severity::Error, format!("Store reading failed: {why}")));
        }
        if let Some(why) = jobs::autostart_fault(jobs::DEFAULT_TRAY_LABEL) {
            health.push(StatusLine::new(Severity::Error, format!("Autostart: {why}")));
        }
        health.reasons.sort_by_key(|r| std::cmp::Reverse(r.severity));
        println!("Status: {}", health.title());
        for reason in health.reasons { println!("{}", reason.marked()); }
        if let Ok(s) = reading {
            for line in view::summary(&s) { println!("{line}"); }
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
        /// A jobs-only reading: local and cheap, refreshed on its own cadence from the store's.
        Jobs(Vec<Job>, Option<String>),
        Autostart(Option<String>),
        Done(Pending),
    }

    pub struct Snapshot {
        stats: Stats,
        /// Wall clock, not `Instant`: the age of the data has to survive the machine sleeping,
        /// and a monotonic clock stops while it does. That made a two-hour nap look like a fresh
        /// reading.
        at: SystemTime,
    }

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum Pending { Reading, Jobs, Action }

    pub enum Command {
        /// Both the store and the jobs.
        Refresh,
        /// The jobs alone, with their own watchdog deadline when no other request is pending.
        RefreshJobs,
        RunJob(String),
        SetSchedule(String, jobs::Schedule),
        SetEnabled(String, bool),
    }

    impl Command {
        fn kind(&self) -> Pending {
            match self {
                Self::Refresh => Pending::Reading,
                Self::RefreshJobs => Pending::Jobs,
                _ => Pending::Action,
            }
        }
    }

    fn read_jobs() -> (Vec<Job>, Option<String>) {
        // Through jobs::job_prefix rather than a cached value: the tray runs for weeks, and a
        // prefix fixed in the config file in the meantime has to reach it without a restart.
        // A refused config is reported in the same place a failed listing is, rather than
        // falling back to the default prefix: that prefix matches nothing in an installation
        // that renamed its jobs, and an empty list reads as "no jobs installed".
        match jobs::job_prefix().and_then(|p| jobs::list(&p)) {
            Ok(j) => (j, None),
            Err(e) => (Vec::new(), Some(e)),
        }
    }

    /// The worker: every slow thing lives here. The main thread only posts commands to it.
    pub fn spawn_worker(tx: mpsc::Sender<Update>, rx: mpsc::Receiver<Command>) {
        std::thread::spawn(move || {
            let read_store = |tx: &mpsc::Sender<Update>| {
                // Re-read the settings on every pass rather than once at startup. The tray runs
                // for weeks; a connection fixed with the setup wizard in the meantime has to
                // reach the running tray, or the fix looks like it did not work.
                match Target::from_env().and_then(|t| fetch(&t)) {
                    Ok(stats) => {
                        let _ = tx.send(Update::Data(Box::new(Snapshot { stats, at: SystemTime::now() })));
                    }
                    Err(e) => { let _ = tx.send(Update::Failed(e)); }
                }
            };
            let read_jobs_into = |tx: &mpsc::Sender<Update>| {
                let (jobs, err) = read_jobs();
                let _ = tx.send(Update::Jobs(jobs, err));
                let _ = tx.send(Update::Autostart(jobs::autostart_fault(jobs::DEFAULT_TRAY_LABEL)));
            };
            while let Ok(cmd) = rx.recv() {
                let kind = cmd.kind();
                match cmd {
                    Command::Refresh => {
                        // Jobs first: they are local and fast, and worth showing even if the
                        // store -- reached over whatever transport is configured, possibly slow
                        // -- does not answer.
                        read_jobs_into(&tx);
                        read_store(&tx);
                    }
                    Command::RefreshJobs => read_jobs_into(&tx),
                    Command::RunJob(label) => {
                        let msg = match jobs::run_now(&label) {
                            Ok(m) => m,
                            // Marked, like every other failure: the title reads the leading "!".
                            Err(e) => format!("! did not start: {e}"),
                        };
                        let _ = tx.send(Update::Note(msg));
                        read_jobs_into(&tx);
                    }
                    Command::SetSchedule(label, sched) => {
                        // Look the job up again: the list the menu was drawn from may be several
                        // seconds old, and editing a schedule from a stale record means editing
                        // something other than what the person saw.
                        let msg = match read_jobs().0.into_iter().find(|j| j.label == label) {
                            Some(j) => match jobs::set_schedule(&j, &sched) {
                                Ok(m) => m,
                                Err(e) => format!("! {e}"),
                            },
                            None => format!("! the job {label} is gone"),
                        };
                        let _ = tx.send(Update::Note(msg));
                        read_jobs_into(&tx);
                    }
                    Command::SetEnabled(label, enabled) => {
                        let msg = match read_jobs().0.into_iter().find(|j| j.label == label) {
                            Some(j) => match jobs::set_enabled(&j, enabled) {
                                Ok(m) => m,
                                Err(e) => format!("! {e}"),
                            },
                            None => format!("! the job {label} is gone"),
                        };
                        let _ = tx.send(Update::Note(msg));
                        read_jobs_into(&tx);
                    }
                }
                let _ = tx.send(Update::Done(kind));
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
        /// Informational rows use normal system text contrast, but never dispatch actions.
        info: Vec<MenuItem>,
        run: Vec<(MenuItem, String)>,
        sched: Vec<(MenuItem, String, jobs::Schedule)>,
        /// One item per job that can be armed/disarmed, carrying the label and the state a click
        /// on it should set (the opposite of what it currently is).
        enable: Vec<(MenuItem, String, bool)>,
    }

    impl Items {
        fn append_info(&mut self, menu: &Menu, text: impl AsRef<str>) {
            // Disabled NSMenuItems use dimmed text that is unreadable on translucent menus.
            // Keep the normal theme-aware foreground and explicitly ignore their selections.
            let item = MenuItem::new(text, true, None);
            let _ = menu.append(&item);
            self.info.push(item);
        }
    }

    pub struct App {
        tray: Option<TrayIcon>,
        items: Items,
        rx: mpsc::Receiver<Update>,
        cmd: mpsc::Sender<Command>,
        last: Option<Snapshot>,
        jobs: Vec<Job>,
        /// Why the job list is missing, if it is. `unwrap_or_default()` used to turn "could not
        /// read the job directory" into an empty list, which rendered as empty Run-now and
        /// Schedule submenus and a calm title -- "no jobs readable" and "every job healthy"
        /// looked identical.
        jobs_error: Option<String>,
        autostart_fault: Option<String>,
        status: String,
        store_error: Option<String>,
        /// The outcome of the last button press, on its own line and with its own age.
        ///
        /// It used to share one field with the status line, and the refresh that every button
        /// starts overwrote it a moment later -- so a "Run now" that failed ended up reading
        /// "updated just now". The outcome of something a person did is the last thing that
        /// should be overwritten.
        note: Option<(String, SystemTime)>,
        /// One admitted command at a time; only its matching reply clears the deadline.
        awaiting: Option<(String, Instant)>,
        awaiting_kind: Option<Pending>,
        /// The worker thread is gone: nothing will ever be read again.
        worker_dead: bool,
        last_refresh: Instant,
        last_jobs_refresh: Instant,
        /// Poll the jobs at `BURST_REFRESH` rather than `JOBS_REFRESH` until this instant --
        /// set after every write command, so its result shows up in a couple of seconds rather
        /// than waiting out the idle cadence.
        burst_until: Instant,
        /// The content last drawn, so an unchanged jobs-only reading at `JOBS_REFRESH` cadence
        /// does not tear down and rebuild the menu -- which can close one open under the cursor
        /// -- for no visible difference.
        last_render: Option<String>,
        render_pending: bool,
        icon_severity: Option<Severity>,
        #[cfg(target_os = "macos")]
        menu_tracking: super::menu_tracking::Tracking,
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
            let now = Instant::now();
            App {
                tray: None,
                items: Items::default(),
                rx,
                cmd,
                last: None,
                jobs: Vec::new(),
                jobs_error: None,
                autostart_fault: None,
                status: "loading…".to_string(),
                store_error: None,
                note: None,
                awaiting: Some(("the first reading".into(), now)),
                awaiting_kind: Some(Pending::Reading),
                worker_dead: false,
                last_refresh: now,
                last_jobs_refresh: now,
                burst_until: now,
                last_render: None,
                render_pending: false,
                icon_severity: None,
                #[cfg(target_os = "macos")]
                menu_tracking: super::menu_tracking::Tracking::new(),
            }
        }

        /// Post a command and remember that an answer is owed. Every path to the worker that the
        /// watchdog should track goes through here.
        fn ask(&mut self, cmd: Command, status: &str, what: &str) {
            if self.worker_dead || self.awaiting.is_some() {
                self.note = Some(("Reader busy or unavailable; command was not queued".into(),
                                  SystemTime::now()));
                return;
            }
            self.status = status.to_string();
            self.awaiting_kind = Some(cmd.kind());
            self.awaiting = Some((what.to_string(), Instant::now()));
            if cmd.kind() == Pending::Action {
                self.burst_until = Instant::now() + BURST_WINDOW;
            }
            if self.cmd.send(cmd).is_err() {
                self.worker_dead = true;
                self.status = "! the reader thread has died; restart the tray".into();
            }
        }

        fn answered(&mut self, kind: Pending) {
            if self.awaiting_kind == Some(kind) {
                self.awaiting = None;
                self.awaiting_kind = None;
                if self.status.starts_with("! no answer")
                    || (kind == Pending::Action && !self.status.starts_with('!')) {
                    self.status.clear();
                }
            }
        }

        fn queue_periodic(&mut self) {
            if self.worker_dead || self.awaiting.is_some() { return; }
            if self.last_refresh.elapsed() >= REFRESH {
                self.last_refresh = Instant::now();
                self.last_jobs_refresh = Instant::now();
                self.ask(Command::Refresh, &self.status.clone(), "a reading");
                return;
            }
            let cadence = if Instant::now() < self.burst_until { BURST_REFRESH }
                          else { JOBS_REFRESH };
            if self.last_jobs_refresh.elapsed() >= cadence {
                self.last_jobs_refresh = Instant::now();
                self.ask(Command::RefreshJobs, &self.status.clone(), "job status");
            }
        }

        fn check_watchdog(&mut self) -> bool {
            if let Some((what, since)) = &self.awaiting {
                if !self.worker_dead && since.elapsed() > WORKER_PATIENCE {
                    let status = format!("! no answer about {what} for {} -- the reader is stuck",
                                         view::ago(since.elapsed()));
                    if self.status != status {
                        self.status = status;
                        return true;
                    }
                }
            }
            false
        }

        fn receive_updates(&mut self) -> bool {
            let mut dirty = false;
            loop {
                match self.rx.try_recv() {
                    Ok(Update::Data(s)) => {
                        // Not a sentence: the sentence is rendered at rebuild time from `last.at`.
                        // Frozen, it said "updated just now" for the whole refresh cycle -- and
                        // after the machine slept, for as long as the nap lasted, because the
                        // refresh timer is monotonic and stops with it.
                        self.status = String::new();
                        self.last = Some(*s);
                        self.store_error = None;
                        dirty = true;
                    }
                    Ok(Update::Failed(e)) => {
                        self.store_error = Some(e);
                        self.status.clear();
                        dirty = true;
                    }
                    Ok(Update::Note(n)) => {
                        self.note = Some((n, SystemTime::now()));
                        dirty = true;
                    }
                    Ok(Update::Jobs(jobs, err)) => {
                        self.jobs = jobs;
                        self.jobs_error = err;
                        dirty = true;
                    }
                    Ok(Update::Autostart(fault)) => {
                        self.autostart_fault = fault;
                        dirty = true;
                    }
                    Ok(Update::Done(kind)) => {
                        self.answered(kind);
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

            dirty
        }

        /// Not one blocking call in here: a non-blocking channel read and a timer. Called on a
        /// ~200ms tick by whichever platform loop is driving this `App`.
        pub fn tick(&mut self) -> Tick {
            if self.tray.is_none() {
                self.rebuild();
                self.last_render = Some(self.content_fingerprint());
            }

            let mut dirty = self.receive_updates();

            dirty |= self.check_watchdog();

            while let Ok(ev) = MenuEvent::receiver().try_recv() {
                if self.items.info.iter().any(|item| item.id() == &ev.id) {
                    continue;
                }
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
                if let Some((_, label, enabled)) =
                    self.items.enable.iter().find(|(i, _, _)| i.id() == &ev.id) {
                    let (label, enabled) = (label.clone(), *enabled);
                    let what = format!("{} {label}", if enabled { "enabling" } else { "disabling" });
                    self.ask(Command::SetEnabled(label, enabled), &format!("{what}…"), &what);
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

            self.queue_periodic();
            self.render_pending |= dirty;
            // Replacing a tracking menu dismisses it, even when only an age label changed.
            #[cfg(target_os = "macos")]
            let menu_open = self.menu_tracking.is_open();
            #[cfg(not(target_os = "macos"))]
            let menu_open = false;
            if self.take_render_request(menu_open) {
                let fp = self.content_fingerprint();
                if self.last_render.as_ref() != Some(&fp) {
                    self.rebuild();
                    self.last_render = Some(fp);
                }
            }
            Tick::Continue
        }

        fn take_render_request(&mut self, menu_open: bool) -> bool {
            !menu_open && std::mem::take(&mut self.render_pending)
        }

        /// Everything that would change what is drawn, boiled down to one string -- compared
        /// against the last one drawn so a `RefreshJobs` tick that changed nothing does not tear
        /// down and rebuild the menu, which can close one open under the cursor, for no visible
        /// difference. Deliberately leaves out anything that only ages (the "(N ago)" suffixes):
        /// those are cosmetic, and `status_line`'s own coarse buckets already force a redraw when
        /// the words themselves would change.
        fn content_fingerprint(&self) -> String {
            let mut fp = self.status_line();
            fp.push_str(&self.status);
            fp.push('\n');
            if let Some((note, _)) = &self.note {
                fp.push_str(note);
                fp.push('\n');
            }
            if let Some(why) = &self.autostart_fault {
                fp.push_str(why);
                fp.push('\n');
            }
            if let Some(s) = &self.last {
                for line in view::summary(&s.stats) {
                    fp.push_str(&line);
                    fp.push('\n');
                }
            }
            if let Some(why) = &self.jobs_error {
                fp.push_str(why);
                fp.push('\n');
            }
            for j in &self.jobs {
                fp.push_str(&view::job_line(j));
                fp.push_str(&format!(" armed={:?}\n", j.armed));
            }
            fp
        }

        fn health(&self) -> view::Health {
            let mut out = view::health(self.last.as_ref().map(|s| &s.stats), &self.jobs,
                                       self.jobs_error.as_deref());
            if let Some(why) = &self.autostart_fault {
                out.push(StatusLine::new(Severity::Error, format!("Autostart: {why}")));
            }
            if let Some(why) = &self.store_error {
                out.push(StatusLine::new(Severity::Error, format!("Store reading failed: {why}")));
            }
            if let Some(why) = self.status.strip_prefix("! ") {
                out.push(StatusLine::new(Severity::Error, why));
            }
            if let Some((note, _)) = &self.note {
                if let Some(why) = note.strip_prefix("! ") {
                    out.push(StatusLine::new(Severity::Error, format!("Last action failed: {why}")));
                }
            }
            out.reasons.sort_by_key(|l| std::cmp::Reverse(l.severity));
            out
        }

        fn rebuild(&mut self) {
            let menu = Menu::new();
            let mut items = Items::default();

            items.append_info(&menu, self.status_line());
            // The outcome of the last button press keeps its own line, with its age, until
            // another press replaces it.
            if let Some((note, when)) = &self.note {
                items.append_info(&menu,
                    format!("{note}  ({} ago)", view::ago(view::age_of(*when))));
            }
            let health = self.health();
            items.append_info(&menu, format!("Status: {}", health.title()));
            for reason in &health.reasons {
                items.append_info(&menu, reason.marked());
            }
            let _ = menu.append(&PredefinedMenuItem::separator());

            if let Some(s) = &self.last {
                for line in view::summary(&s.stats) {
                    items.append_info(&menu, line);
                }
                let _ = menu.append(&PredefinedMenuItem::separator());

            }
            {
                let run = Submenu::new("Run now", true);
                for j in &self.jobs {
                    let item = MenuItem::new(view::job_line(j), true, None);
                    let _ = run.append(&item);
                    items.run.push((item, j.label.clone()));
                }
                let _ = menu.append(&run);

                let sched = Submenu::new("Schedule", true);
                for j in &self.jobs {
                    if matches!(j.schedule, jobs::Schedule::None) || j.unwritable() {
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

                // A backend that tracks "armed" at all (systemd; not launchd, where a loaded job
                // is armed by definition and there is nothing here to switch) gets a submenu to
                // fix the one fault the read side could already see but not act on.
                if jobs::SUPPORTS_ENABLE {
                    let timers = Submenu::new("Timers", true);
                    let mut any = false;
                    for j in &self.jobs {
                        // A job the backend could not load at all (`armed` is only ever `None`
                        // there) has nothing to arm; a merely disabled one -- `armed ==
                        // Some(false)`, which also carries a `fault` from `enablement_fault` --
                        // is exactly the case this submenu exists to fix, so it is NOT excluded
                        // here the way `j.broken()` alone would exclude it.
                        let Some(armed) = j.armed else { continue };
                        any = true;
                        let label = format!("{} ({})", j.short(),
                                            if armed { "enabled" } else { "disabled" });
                        let action = if armed { "Disable" } else { "Enable" };
                        let item = MenuItem::new(format!("{label} -- {action}"), true, None);
                        let _ = timers.append(&item);
                        items.enable.push((item, j.label.clone(), !armed));
                    }
                    if any {
                        let _ = menu.append(&timers);
                    }
                }

            }

            let _ = menu.append(&PredefinedMenuItem::separator());
            let refresh = MenuItem::new("Refresh now", true, None);
            let _ = menu.append(&refresh);
            items.refresh = Some(refresh);
            let quit = MenuItem::new("Quit", true, None);
            let _ = menu.append(&quit);
            items.quit = Some(quit);

            self.items = items;
            let tooltip = format!("HyperMnesia — {}", health.title());
            let severity = health.severity;
            match self.tray.as_ref() {
                Some(t) => {
                    t.set_menu(Some(Box::new(menu)));
                    let _ = t.set_tooltip(Some(&tooltip));
                    if self.icon_severity != Some(severity) {
                        match t.set_icon(Some(mark_icon(severity))) {
                            Ok(()) => self.icon_severity = Some(severity),
                            Err(e) => eprintln!("hypermnesia: could not update the tray icon: {e}"),
                        }
                    }
                }
                None => {
                    // Not `.ok()`: an icon that never appeared leaves a process running with no
                    // way to see anything, and the service manager counts it as healthy. Better
                    // to say so and stop.
                    match TrayIconBuilder::new()
                        .with_menu(Box::new(menu))
                        .with_icon(mark_icon(severity))
                        .with_tooltip(&tooltip)
                        .build() {
                        Ok(t) => {
                            self.tray = Some(t);
                            self.icon_severity = Some(severity);
                        }
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
            if let Some(why) = &self.store_error {
                let age = self.last.as_ref()
                    .map(|s| format!(", showing state from {} ago", view::ago(view::age_of(s.at))))
                    .unwrap_or_default();
                return format!("! not updated: {why}{age}");
            }
            if !self.status.is_empty() {
                let age = self.last.as_ref().filter(|_| self.status.starts_with('!'))
                    .map(|s| format!(", showing state from {} ago", view::ago(view::age_of(s.at))))
                    .unwrap_or_default();
                return format!("{}{age}", self.status);
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

    /// Embedded artwork: the installed binary needs no asset files beside it.
    fn mark_icon(severity: Severity) -> Icon {
        let bytes: &[u8] = if severity != Severity::Healthy { include_bytes!("../../assets/tray-warning.png") }
                          else { include_bytes!("../../assets/tray-ok.png") };
        let mut decoder = png::Decoder::new(std::io::Cursor::new(bytes));
        decoder.set_transformations(png::Transformations::normalize_to_color8());
        let mut reader = decoder.read_info().expect("embedded tray PNG header");
        let mut rgba = vec![0; reader.output_buffer_size().expect("embedded tray PNG size")];
        let info = reader.next_frame(&mut rgba).expect("embedded tray PNG pixels");
        assert_eq!(info.color_type, png::ColorType::Rgba, "tray artwork must have alpha");
        rgba.truncate(info.buffer_size());
        if severity == Severity::Attention {
            // Runtime palette preserves alpha and neutral lettering in the existing artwork.
            for pixel in rgba.chunks_exact_mut(4) {
                let [r, g, b, _] = [pixel[0], pixel[1], pixel[2], pixel[3]];
                if r > g && r > b {
                    pixel[1] = g.max(b.saturating_add(((u16::from(r) - u16::from(b)) * 3 / 4) as u8));
                    pixel[2] = b.min(g);
                }
            }
        }
        Icon::from_rgba(rgba, info.width, info.height).expect("embedded tray icon dimensions")
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn all_status_icons_decode() {
            let _ = mark_icon(Severity::Healthy);
            let _ = mark_icon(Severity::Error);
            let _ = mark_icon(Severity::Attention);
        }

        #[test]
        fn repeated_refresh_does_not_reset_a_pending_deadline() {
            let (_, rx) = mpsc::channel();
            let (cmd, commands) = mpsc::channel();
            let mut app = App::new(rx, cmd);
            let first = Instant::now() - Duration::from_secs(61);
            app.awaiting = Some(("the first reading".into(), first));
            app.ask(Command::Refresh, "refreshing…", "a reading");
            assert_eq!(app.awaiting.as_ref().unwrap().1, first);
            assert!(commands.try_recv().is_err(), "a pending read must not queue duplicates");
        }


        #[test]
        fn periodic_reads_leave_the_original_deadline_and_recover_after_reply() {
            let (_, rx) = mpsc::channel();
            let (cmd, commands) = mpsc::channel();
            let mut app = App::new(rx, cmd);
            let first = Instant::now() - Duration::from_secs(91);
            app.awaiting = Some(("the first reading".into(), first));
            app.last_refresh = Instant::now() - REFRESH;
            app.last_jobs_refresh = Instant::now() - JOBS_REFRESH;
            app.queue_periodic();
            assert!(commands.try_recv().is_err());
            app.answered(Pending::Jobs);
            assert_eq!(app.awaiting.as_ref().unwrap().1, first);
            assert!(app.check_watchdog());
            assert_eq!(app.health().severity, Severity::Error);
            app.answered(Pending::Reading);
            assert!(app.awaiting.is_none());
            assert!(!app.status.starts_with("! no answer"));
            app.queue_periodic();
            assert!(matches!(commands.try_recv(), Ok(Command::Refresh)));
            assert!(commands.try_recv().is_err());
        }

        #[test]
        fn a_hung_jobs_read_is_watched_too() {
            let (_, rx) = mpsc::channel();
            let (cmd, commands) = mpsc::channel();
            let mut app = App::new(rx, cmd);
            app.answered(Pending::Reading);
            app.last_jobs_refresh = Instant::now() - JOBS_REFRESH;
            app.queue_periodic();
            assert!(matches!(commands.try_recv(), Ok(Command::RefreshJobs)));
            app.awaiting.as_mut().unwrap().1 = Instant::now() - Duration::from_secs(91);
            assert!(app.check_watchdog());
            app.answered(Pending::Jobs);
            assert!(app.awaiting.is_none());
            assert!(!app.status.starts_with('!'));
        }

        #[test]
        fn autostart_and_last_action_errors_are_named_without_a_snapshot() {
            let (_, rx) = mpsc::channel();
            let (cmd, _) = mpsc::channel();
            let mut app = App::new(rx, cmd);
            app.autostart_fault = Some("binary missing".into());
            app.note = Some(("! job did not start".into(), SystemTime::now()));
            let h = app.health();
            assert_eq!(h.severity, Severity::Error);
            assert!(h.reasons.iter().any(|l| l.text == "Autostart: binary missing"));
            assert!(h.reasons.iter().any(|l| l.text == "Last action failed: job did not start"));
        }

        #[test]
        fn failed_reading_age_is_not_frozen_at_failure_time() {
            let (_, rx) = mpsc::channel();
            let (cmd, _) = mpsc::channel();
            let mut app = App::new(rx, cmd);
            app.status = "! not updated: unavailable".into();
            app.last = Some(Snapshot { stats: Stats::default(),
                at: SystemTime::now() - Duration::from_secs(240) });
            assert!(app.status_line().contains("showing state from 4m ago"));
            app.last.as_mut().unwrap().at -= Duration::from_secs(120);
            assert!(app.status_line().contains("showing state from 6m ago"));
        }


        #[test]
        fn partial_worker_replies_cannot_admit_another_command() {
            let (updates, rx) = mpsc::channel();
            let (cmd, commands) = mpsc::channel();
            let mut app = App::new(rx, cmd);
            app.answered(Pending::Reading);
            app.ask(Command::RunJob("fixture".into()), "running…", "fixture");
            assert!(matches!(commands.try_recv(), Ok(Command::RunJob(_))));
            updates.send(Update::Note("started".into())).unwrap();
            updates.send(Update::Jobs(vec![], None)).unwrap();
            app.receive_updates();
            assert_eq!(app.awaiting_kind, Some(Pending::Action));
            app.last_refresh = Instant::now() - REFRESH;
            app.queue_periodic();
            assert!(commands.try_recv().is_err());
            updates.send(Update::Done(Pending::Action)).unwrap();
            app.receive_updates();
            app.queue_periodic();
            assert!(matches!(commands.try_recv(), Ok(Command::Refresh)));
        }

        #[test]
        fn unrelated_actions_cannot_clear_a_failed_store_reading() {
            let (updates, rx) = mpsc::channel();
            let (cmd, _commands) = mpsc::channel();
            let mut app = App::new(rx, cmd);
            updates.send(Update::Failed("database unavailable".into())).unwrap();
            updates.send(Update::Done(Pending::Reading)).unwrap();
            app.receive_updates();
            app.ask(Command::RunJob("fixture".into()), "running…", "fixture");
            updates.send(Update::Note("started".into())).unwrap();
            updates.send(Update::Done(Pending::Action)).unwrap();
            app.receive_updates();
            assert_eq!(app.health().severity, Severity::Error);
            assert!(app.status_line().contains("database unavailable"));
            updates.send(Update::Data(Box::new(Snapshot { stats: Stats::default(),
                at: SystemTime::now() }))).unwrap();
            app.receive_updates();
            assert_eq!(app.health().severity, Severity::Healthy);
        }

        #[test]
        fn background_updates_wait_for_menu_close_and_render_once() {
            let (_, rx) = mpsc::channel();
            let (cmd, _) = mpsc::channel();
            let mut app = App::new(rx, cmd);
            for _ in 0..15 {
                app.render_pending = true;
                assert!(!app.take_render_request(true), "an open menu must stay attached");
            }
            assert!(app.take_render_request(false), "deferred updates must reach the next menu");
            assert!(!app.take_render_request(false), "unchanged data must not replace it again");
        }
    }
}

#[cfg(target_os = "macos")]
mod menu_tracking {
    use std::ptr::NonNull;
    use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};
    use block2::RcBlock;
    use objc2::{rc::Retained, runtime::ProtocolObject};
    use objc2_foundation::{NSNotification, NSNotificationCenter, NSObjectProtocol, NSString};

    const BEGIN: &str = "NSMenuDidBeginTrackingNotification";
    const END: &str = "NSMenuDidEndTrackingNotification";

    pub struct Tracking {
        depth: Arc<AtomicUsize>,
        observers: [Retained<ProtocolObject<dyn NSObjectProtocol>>; 2],
    }

    impl Tracking {
        pub fn new() -> Self {
            let depth = Arc::new(AtomicUsize::new(0));
            let center = NSNotificationCenter::defaultCenter();
            let observers = [(BEGIN, true), (END, false)].map(|(name, begin)| {
                let depth = depth.clone();
                let block = RcBlock::new(move |_: NonNull<NSNotification>| {
                    if begin {
                        depth.fetch_add(1, Ordering::Relaxed);
                    } else {
                        let _ = depth.fetch_update(Ordering::Relaxed, Ordering::Relaxed,
                                                   |d| Some(d.saturating_sub(1)));
                    }
                });
                // No queue: AppKit posts synchronously, before the next menu-render tick.
                // The block captures only an atomic, so it is safe on any posting thread.
                unsafe { center.addObserverForName_object_queue_usingBlock(
                    Some(&NSString::from_str(name)), None, None, &block) }
            });
            Self { depth, observers }
        }

        pub fn is_open(&self) -> bool { self.depth.load(Ordering::Relaxed) > 0 }
    }

    impl Drop for Tracking {
        fn drop(&mut self) {
            let center = NSNotificationCenter::defaultCenter();
            for observer in &self.observers {
                // These are the tokens returned by this center's registration calls.
                unsafe { center.removeObserver(ProtocolObject::as_ref(&**observer)); }
            }
        }
    }

    #[cfg(test)]
    #[test]
    fn native_notifications_keep_parent_menu_open_after_submenu_closes() {
        let tracking = Tracking::new();
        let center = NSNotificationCenter::defaultCenter();
        let post = |name| unsafe {
            center.postNotificationName_object(&NSString::from_str(name), None);
        };
        assert!(!tracking.is_open());
        post(BEGIN);
        post(BEGIN);
        post(END);
        assert!(tracking.is_open(), "closing a submenu must not release the parent menu");
        post(END);
        assert!(!tracking.is_open());
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
