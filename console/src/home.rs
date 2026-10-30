//! Where this machine keeps HyperMnesia: the directory `ci/install.py` creates and the hooks and
//! the MCP server read (`HOME_DIR` in hooks/_mem_common.py).
//!
//! ```text
//! <home>/hypermnesia.env    the pipeline's settings file (see `settings::env_file`)
//! <home>/current            -> releases/<sha>, the installed release
//! <home>/releases/<sha>/    one commit, with its binaries in bin/
//! <home>/logs/              what the schedulers write, the tray's own log included
//! ```
//!
//! `HYPERMNESIA_HOME` moves it; empty counts as unset; the default is `~/.HyperMnesia`. Resolved
//! by the loader's rule, `abspath(expanduser(...))`, because a console that names a different
//! directory than the hooks read is the failure this crate keeps refusing to have.

use std::ffi::OsStr;
use std::path::{Component, Path, PathBuf};

/// The home directory, resolved like `HOME_DIR` in hooks/_mem_common.py.
pub fn hm_home() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    let raw = std::env::var("HYPERMNESIA_HOME").ok().filter(|s| !s.is_empty())
        .unwrap_or_else(|| "~/.HyperMnesia".into());
    abs_path(&crate::settings::expand_home(&raw, &home))
}

/// Python's os.path.abspath: a relative path joins the working directory, then `.` and `..` are
/// folded lexically, without resolving symlinks.
fn abs_path(p: &str) -> PathBuf {
    let joined = if Path::new(p).is_absolute() { PathBuf::from(p) }
                 else { std::env::current_dir().unwrap_or_default().join(p) };
    let mut out = PathBuf::new();
    for c in joined.components() {
        match c {
            Component::ParentDir => { out.pop(); }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// `<home>/logs`, created if it is not there -- private, like the home `ci/install.py` makes:
/// the settings loader refuses a file in a directory anyone else can write.
pub fn log_dir() -> PathBuf {
    let dir = hm_home().join("logs");
    let _ = private_dir_all(&dir);
    dir
}

/// `create_dir_all`, with every directory it creates made 0700 on unix.
pub fn private_dir_all(dir: &Path) -> std::io::Result<()> {
    let mut b = std::fs::DirBuilder::new();
    b.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        b.mode(0o700);
    }
    b.create(dir)
}

/// The path an autostart entry should name for this executable.
///
/// A console started from an installed release runs from `<home>/releases/<sha>/bin/` -- on
/// Linux `current_exe` resolves the `current` link to exactly that. Baked into an autostart
/// entry, that path pins the tray to one release: the installer keeps only the newest few, so the
/// entry outlives its binary and the tray silently stops starting at login. The stable name is
/// `<home>/current/bin/<name>`, the one the installer switches. Anything else -- a checkout's
/// `console/target/release/`, a copy somewhere on PATH -- is named as it is.
pub fn autostart_exe(exe: &Path) -> PathBuf {
    autostart_exe_in(exe, &hm_home())
}

fn autostart_exe_in(exe: &Path, home: &Path) -> PathBuf {
    let keep = || exe.to_path_buf();
    let (Some(name), Some(bin)) = (exe.file_name(), exe.parent()) else { return keep() };
    let Some(releases) = bin.parent().and_then(Path::parent) else { return keep() };
    if bin.file_name() != Some(OsStr::new("bin"))
        || releases.file_name() != Some(OsStr::new("releases")) {
        return keep();
    }
    // Compared canonically: /home may itself be a link (/var/home on some distributions), and
    // the executable's path has had every link resolved.
    let canon = |p: &Path| std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    if canon(releases) != canon(&home.join("releases")) {
        return keep();
    }
    let stable = home.join("current").join("bin").join(name);
    // Only when `current` has it: a release installed without the console would turn the entry
    // into a path that does not exist.
    if stable.exists() { stable } else { keep() }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_release_binary_is_named_through_current() {
        let d = std::env::temp_dir().join(format!("hm-console-home-{}", std::process::id()));
        let home = d.join("hm");
        let rel = home.join("releases/abc123/bin");
        std::fs::create_dir_all(&rel).unwrap();
        std::fs::write(rel.join("hypermnesia"), "").unwrap();
        let exe = rel.join("hypermnesia");
        assert_eq!(autostart_exe_in(&exe, &home), exe, "no current/ yet: named as it is");
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink("releases/abc123", home.join("current")).unwrap();
            assert_eq!(autostart_exe_in(&exe, &home), home.join("current/bin/hypermnesia"));
            // What current_exe reports on Linux: the link already resolved.
            let real = std::fs::canonicalize(&exe).unwrap();
            assert_eq!(autostart_exe_in(&real, &home), home.join("current/bin/hypermnesia"));
            let through = home.join("current/bin/hypermnesia");
            assert_eq!(autostart_exe_in(&through, &home), through, "already stable");
            assert_eq!(autostart_exe_in(&rel.join("hypermnesia-stats"), &home), rel.join("hypermnesia-stats"),
                       "a binary current does not have is not renamed into one that is not there");
        }
        let checkout = d.join("repo/console/target/release/hypermnesia");
        assert_eq!(autostart_exe_in(&checkout, &home), checkout, "a checkout build is left alone");
        let other = d.join("elsewhere/releases/abc123/bin/hypermnesia");
        assert_eq!(autostart_exe_in(&other, &home), other, "another home's release is not ours");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn the_home_is_resolved_like_the_loader() {
        assert_eq!(abs_path("/a/./b/../c"), PathBuf::from("/a/c"));
        assert_eq!(abs_path("x/../y"), std::env::current_dir().unwrap().join("y"));
    }
}
