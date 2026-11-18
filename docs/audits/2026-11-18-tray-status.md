# Tray status audit — 18 November 2026

The tray now distinguishes pending review work from operational failures and lists the
reasons at the top of the menu. This review used the code-audit and logic-audit evidence
contracts and included fixes requested with the audit.

## Scope and evidence

Base revision: 969a2fed75711a16cef931e0640ac54bfccd2f05. Tracked files were clean before
the investigation; existing machine-local untracked launchers were left untouched.
The examined paths are the shared presentation layer, macOS job collection, the tray worker
protocol, menu refresh/tracking and the stats fields used by those consumers. Database
ingestion, review decisions, scheduling mutations and server deployments are outside this
change. The Linux GUI and systemd runtime were not exercised.

Data flows from the read-only store query and local service-manager collection through the
worker channel into one cached UI state. Typed status reasons now determine both the menu
and icon. Slow autostart inspection also runs on the worker. Menu replacement remains deferred
while AppKit reports an open menu.

| Condition | Result |
| --- | --- |
| Observed healthy reading, no issues | Green |
| Review queue, embedding backlog or uncertain job timing | Amber, with reasons |
| Failed reading, nonzero job exit, overdue job or configuration fault | Red, with reasons |
| Review items and errors together | Red; both remain visible |
| No store reading yet | Amber until a reading or explicit failure arrives |
| Request pending | No duplicate request admitted; original watchdog deadline retained |

The original running executable had outlived its on-disk build artifact. The source review
therefore does not establish the exact implementation in that old process. Runtime claims
below refer to the rebuilt artifact.

## Confirmed findings and corrections

### 1. Incomplete health aggregation could hide operational errors — Medium

**Evidence:** reproduced on the base presentation code. A job with a recent log, no
configuration fault and exit 1 produced a marked job line but a calm icon: the old
warned() checked broken/overdue, not last_exit. The regression
recent_failure_also_marks_the_icon failed with exit 101 before the fix.

A related proven path omitted autostart faults from the icon predicate and hid job controls
until a successful store snapshot, although the worker deliberately read jobs first.
These are bounded diagnostic failures, not evidence of database loss.

**Counterevidence checked:** job_line did display exit codes, so the failure was in aggregation,
not loss of service-manager data. A running rerun intentionally takes precedence over its
previous exit status; it is not classified as an overdue idle job.

**Correction:** view::health and job_status share typed status lines with the menu.
Job and autostart errors work without a store snapshot. Review items are Attention.
Tests cover error precedence, missing snapshots, unknown timing and autostart/action reasons.

Locations: console/src/view.rs (health, job_status);
console/src/bin/tray.rs (App::health, App::rebuild).

### 2. Periodic refresh reset the watchdog and queued redundant work — Medium

**Evidence:** reproduced using the real App::ask path with a controlled pending timestamp.
At t=0 a read begins; at t=60 the automatic refresh resets its start time; at t=90 the
90-second watchdog sees only 30 seconds. Repetition prevents the warning and adds requests
behind the stalled worker. The baseline regression failed with exit 101 because the original
timestamp was replaced. Jobs-only requests had no watchdog deadline at all.

**Counterevidence checked:** normal store fetches have a shorter timeout, but the configured
timeout can exceed the refresh period and local service-manager calls can also block.
A short default timeout does not establish liveness for the whole worker.

**Correction:** one command is admitted at a time. An explicit Done message is emitted only
after the command and all its follow-up reads finish; intermediate Data/Note/Jobs replies
cannot release another command. The jobs-only path gets its own deadline. Matching completion
clears a timeout warning and restores periodic reads.

Tests cover the 61/91-second boundaries without sleeping, mismatched replies, partial action
replies, jobs-only hangs and recovery. The tests invoke the production channel handler.

Locations: console/src/bin/tray.rs (spawn_worker, ask, queue_periodic, check_watchdog,
receive_updates).

### 3. launchctl failure was converted into an empty state map — Medium

**Evidence:** proven path in the base code. Failure to spawn launchctl returned an empty map;
a nonzero exit was not checked before parsing stdout. list() and run_now() consumed this as
service-manager data, losing the cause of an unavailable listing.

**Counterevidence checked:** plist parsing reports its own failures, but cannot detect the
independent launchctl collection failure. runs_of() may return None without preserving why.

**Correction:** launchctl_states returns Result and checks process status. Both callers
propagate the error. A local subprocess fixture exits 1 with diagnostic stderr, which the
parser now preserves as an error; a valid exit-code row is the positive control.

Location: console/src/jobs/launchd.rs (launchctl_states, parse_launchctl_states and callers).

### 4. Failed store state could be erased by an unrelated action — Medium

**Evidence:** proven path in the base code. Update::Failed stored the database failure in
status. A button press then assigned “running…” to that same field. A successful job action
did not constitute a successful store read, but the only failure marker was gone.

**Counterevidence checked:** the previous Snapshot was retained, so this was an observability
and freshness error, not replacement of data with zeros.

**Correction:** store_error is independent of the activity/status message and survives actions
and job refreshes. Only a successful store reading clears it. The regression exercises
Failed → action → Note/Done → Data through the actual receiver.

Location: console/src/bin/tray.rs (store_error, receive_updates, health, status_line).

### 5. The displayed age of a failed reading stopped advancing — Low

**Evidence:** proven path in the base handler. Update::Failed appended the current age to a
stored string, which status_line subsequently returned unchanged. During a continuing failure
the displayed age described the failure moment instead of the retained snapshot.

**Correction:** age is calculated from Snapshot.at when rendering. A controlled timestamp test
checks the same failed state at four and six minutes.

Location: console/src/bin/tray.rs (status_line).

## Clarifications, not additional defect claims

- Review age alone is not an operational error. Queue items still require a substantive review;
  no queue item was approved, rejected or deleted for this UI change.
- The stats “stale” field counts qualifying old, unused personal memories. The menu now calls
  it “Old unused memories (180+ days)”; it is not a document freshness measurement.
- At the first audit snapshot, freshness exit 1 combined discrepancies with checker failures.
  The maintenance follow-up below introduces a distinct completed-with-findings outcome;
  ordinary nonzero failures still remain errors.
- A failed button action remains explicitly visible until a subsequent button press. This is
  intentional history, documented in README, not proof of a continuing store outage.
- Corpus sums use i128 for accumulation so multiple valid i64 counts cannot wrap in the UI.
  This has a boundary test; no claim is made that a real corpus approached that boundary.

## Validation and limits

- Full console test suite: 82 passed, 0 failed; serialized to respect environment fixtures.
- Release build: all console binaries built successfully with the locked dependency graph.
- Release --status smoke: healthy, review-only, review plus model error, and failed-store
  fixtures all returned the expected classification.
- Live read-only --status showed the review queue separately from three nonzero job exits.
- Only the existing local tray launchd job was restarted. The new PID was checked against the
  rebuilt executable path. No backend, timer, queue item or server workload was changed.
- Native AppKit notification and deferred-menu tests passed. Visual inspection of the live
  menu was unavailable because the computer-use native host could not connect; icon decoding
  and process health do not substitute for that check.
- README and diagnostics now describe the three states and the limits of exit/freshness data.

Commands (run from the repository root with the installed Rust toolchain on PATH):

    cargo test --locked --manifest-path console/Cargo.toml -- --test-threads=1
    cargo build --locked --release --manifest-path console/Cargo.toml --bins
    console/target/release/hypermnesia --status

The --status command performs reads only. It prints the observed severity; its exit code is
not a monitoring-severity API. Logs from this local validation are under console/target/
(status-tests-final.log and status-build.log), excluded from version control.

## Audited source hashes

- console/src/view.rs: cd876379cc67d3c7f1c333bbf3ccc2edf9a7e3c3dfa5b02602eb221cb9702435
- console/src/jobs/launchd.rs: dae94b062ae5bcb67662f4c71c57a0fdfb5c0e23e095c81717ca0ba3de60830d
- console/src/bin/tray.rs: 10e0fbc7d2724e801dce4f95a79e2e4a6d0233ae04dab7a51cdb45e480390735


## Contrast follow-up

A screenshot of the deployed menu showed that macOS rendered informational rows as dimmed
disabled items against the translucent menu background. Those rows now use the normal
theme-aware text appearance. Their IDs are tracked separately, and selecting one returns
without issuing a command or displaying the stale-button message. No fixed white/black
palette or system-wide appearance setting is used.

The tray's 10 existing tests and the release build passed after this correction.
The preceding source hashes describe the first audit snapshot. Updated tray source SHA256:
695238ed2fc19c46505c574a5318741591cdfe8dbdf211cad9cea1b179963c7c


## Maintenance outcome follow-up

The private scheduled freshness sweep now returns exit 10 only for a completed
check with review findings. The tray recognizes that outcome for the freshness
job and shows amber with “review findings”. Exit 1, unreadable observations and
other failed jobs remain red. A regression check covers these distinctions.
The installed tray was rebuilt and restarted after the full serialized Rust suite:
83 tests passed, and the release build succeeded.
