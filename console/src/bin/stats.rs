//! The console's data layer, as its own binary.
//!
//! Prints one picture of the store's state to the terminal. It exists before the tray and
//! independently of it: the numbers can be checked by eye, and they are not blocked by UI
//! fiddling. The tray will call the same functions.
//!
//!     hypermnesia-stats            human-readable
//!     hypermnesia-stats --json     as it came back from the database, unparsed
//!
//! Environment: HM_PSQL_CMD (or the console's settings file), HM_TIMEOUT_SECS,
//! HM_CONSOLE_CONFIG.

use hypermnesia_console::{fetch, Stats, Target};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "-h" || a == "--help") {
        print!("{}", HELP);
        return;
    }
    let t = Target::default();

    if args.iter().any(|a| a == "--json") {
        // The raw answer: if the parser loses something, comparing the two modes shows it.
        match hypermnesia_console::fetch_raw(&t) {
            Ok(raw) => println!("{}", raw.trim()),
            Err(e) => fail(&e),
        }
        return;
    }

    match fetch(&t) {
        Ok(s) => render(&s),
        Err(e) => fail(&e),
    }
}

fn fail(e: &str) -> ! {
    // On stderr and with a non-zero exit code: a console that prints zeroes when the store
    // cannot be reached is indistinguishable from one reporting an empty memory.
    eprintln!("hypermnesia-stats: {e}");
    std::process::exit(1)
}

fn render(s: &Stats) {
    println!("MEMORY            {} active of {} ({} knowledge pages)",
             s.memories_active, s.memories_total, s.pages);
    let mut types: Vec<_> = s.by_type.iter().collect();
    types.sort_by_key(|(_, n)| -**n);
    println!("  by type         {}",
             types.iter().map(|(t, n)| format!("{t} {n}")).collect::<Vec<_>>().join(", "));
    let shown = 8.min(s.by_project.len());
    println!("  by project      {}{}",
             s.by_project[..shown].iter().map(|(p, n)| format!("{p} {n}"))
                 .collect::<Vec<_>>().join(", "),
             if s.by_project.len() > shown {
                 format!(" … {} more", s.by_project.len() - shown)
             } else { String::new() });

    println!();
    let (docs, chunks, embedded) = totals(s, false);
    let (mdocs, _, _) = totals(s, true);
    println!("CORPUS            {docs} documents, {chunks} chunks, {embedded} with embeddings");
    if embedded < chunks {
        // Not an "error", but not a detail either: chunks left without an embedding are found by
        // the lexical leg alone.
        println!("  ! {} chunks with no embedding -- found only by the exact word",
                 chunks - embedded);
    }
    println!("  page mirrors    {mdocs} (<project>~mem tags; the document search sees them)");
    println!("  models          {}", if s.embedding_models.len() == 1 {
        s.embedding_models.keys().next().cloned().unwrap_or_default()
    } else {
        // Two models in one store means part of the corpus is unreachable: the vectors cannot
        // be compared with one another.
        format!("! {} DIFFERENT: {}", s.embedding_models.len(),
                s.embedding_models.iter().map(|(m, n)| format!("{m} {n}"))
                    .collect::<Vec<_>>().join(", "))
    });
    println!("  Tier 0/1 map    {}",
             s.components.iter().map(|(r, n)| format!("{r} {n}")).collect::<Vec<_>>().join(", "));

    println!();
    println!("REVIEW QUEUE      {}{}", s.review_pending,
             if s.review_pending > 0 {
                 format!(", oldest {} days", s.review_oldest_days)
             } else { String::new() });
    println!("STALE             {} (active, older than 180 days, confirmed by nobody)", s.stale);
    println!("STORE SIZE        {}", s.db_size);
    println!();
    println!("(the reading took {:.1}s)", s.took.as_secs_f32());
}

fn totals(s: &Stats, memory_pages: bool) -> (i64, i64, i64) {
    s.corpus.iter().filter(|r| r.is_memory_page() == memory_pages)
        .fold((0, 0, 0), |(d, c, e), r| (d + r.docs, c + r.chunks, e + r.embedded))
}

const HELP: &str = "\
hypermnesia-stats -- the state of the store in one query.

    hypermnesia-stats           human-readable
    hypermnesia-stats --json    the database's raw answer

Reaches the store by running the one configured psql command, the same way the hooks do.
Variables: HM_PSQL_CMD (otherwise the command recorded in the console's settings file),
HM_TIMEOUT_SECS, HM_CONSOLE_CONFIG.
";
