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
//! by the loader's rule, `abspath(expanduser(...))` with Python's expanduser reproduced exactly
//! (HOME, else the passwd home; `~user`; USERPROFILE on Windows), as the MCP server does --
//! because a console that names a different directory than the hooks read is the failure this
//! crate keeps refusing to have.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

/// Home directories from the passwd database (getpwuid_r / getpwnam_r), for `~` expansion.
/// Only where `struct passwd` has a layout known here; elsewhere every lookup is "unknown",
/// which leaves the `~` literal -- what Python does when it cannot look a user up.
#[cfg(unix)]
mod pw {
    use std::ffi::{c_char, CStr, CString};
    #[cfg(target_os = "linux")]
    #[repr(C)]
    struct Passwd {
        pw_name: *mut c_char, pw_passwd: *mut c_char, pw_uid: u32, pw_gid: u32,
        pw_gecos: *mut c_char, pw_dir: *mut c_char, pw_shell: *mut c_char,
        _spare: [u8; 64],
    }
    #[cfg(target_vendor = "apple")]
    #[repr(C)]
    struct Passwd {
        pw_name: *mut c_char, pw_passwd: *mut c_char, pw_uid: u32, pw_gid: u32,
        pw_change: std::ffi::c_long, pw_class: *mut c_char, pw_gecos: *mut c_char,
        pw_dir: *mut c_char, pw_shell: *mut c_char, pw_expire: std::ffi::c_long,
        _spare: [u8; 64],
    }
    #[cfg(any(target_os = "linux", target_vendor = "apple"))]
    extern "C" {
        fn getuid() -> u32;
        fn getpwuid_r(uid: u32, pwd: *mut Passwd, buf: *mut c_char, len: usize,
                      result: *mut *mut Passwd) -> i32;
        fn getpwnam_r(name: *const c_char, pwd: *mut Passwd, buf: *mut c_char, len: usize,
                      result: *mut *mut Passwd) -> i32;
    }

    /// One lookup, growing the buffer on ERANGE. None: no such entry, or the lookup failed.
    #[cfg(any(target_os = "linux", target_vendor = "apple"))]
    fn lookup(call: &dyn Fn(*mut Passwd, *mut c_char, usize, *mut *mut Passwd) -> i32)
        -> Option<(String, String)> {
        const ERANGE: i32 = 34;
        let mut len = 1024;
        while len <= 1 << 20 {
            let mut buf = vec![0 as c_char; len];
            let mut ent: Passwd = unsafe { std::mem::zeroed() };
            let mut out: *mut Passwd = std::ptr::null_mut();
            let rc = call(&mut ent, buf.as_mut_ptr(), len, &mut out);
            if rc == ERANGE { len *= 4; continue; }
            if rc != 0 || out.is_null() || ent.pw_dir.is_null() || ent.pw_name.is_null() {
                return None;
            }
            // Non-UTF-8 entries are refused rather than mangled: a lossy home is another path.
            let name = unsafe { CStr::from_ptr(ent.pw_name) }.to_str().ok()?.to_string();
            let dir = unsafe { CStr::from_ptr(ent.pw_dir) }.to_str().ok()?.to_string();
            return Some((name, dir));
        }
        None
    }

    /// (name, home) of the current uid.
    #[cfg(any(target_os = "linux", target_vendor = "apple"))]
    pub fn current() -> Option<(String, String)> {
        let uid = unsafe { getuid() };
        lookup(&|e, b, l, r| unsafe { getpwuid_r(uid, e, b, l, r) })
    }
    /// The home of user `name`.
    #[cfg(any(target_os = "linux", target_vendor = "apple"))]
    pub fn home_of(name: &str) -> Option<String> {
        let c = CString::new(name).ok()?;
        lookup(&|e, b, l, r| unsafe { getpwnam_r(c.as_ptr(), e, b, l, r) }).map(|(_, d)| d)
    }
    #[cfg(not(any(target_os = "linux", target_vendor = "apple")))]
    pub fn current() -> Option<(String, String)> { None }
    #[cfg(not(any(target_os = "linux", target_vendor = "apple")))]
    pub fn home_of(_name: &str) -> Option<String> { None }
}

/// Python's posixpath.expanduser, rule for rule, with `home` standing for $HOME (None = unset):
/// `~` and `~/...` take $HOME when it is SET -- even set to "" -- and the passwd home of the
/// current uid only when it is unset; `~user/...` takes that user's passwd home, and stays
/// literal for an unknown user, as does `~` when the lookup fails. The home loses its trailing
/// slashes, and an empty result is "/". The Python loader (hooks/_mem_common.py) runs exactly
/// this on HYPERMNESIA_ENV_FILE, so anything else here means two readers opening two files.
/// Copied from mcp-server/src/main.rs, rule for rule; the tests below pin the same cases.
#[cfg(unix)]
pub fn expand_user(path: &str, home: Option<&str>) -> String {
    if !path.starts_with('~') { return path.to_string(); }
    let i = path[1..].find('/').map_or(path.len(), |j| j + 1);
    let userhome = if i == 1 {
        match home {
            Some(h) => h.to_string(),
            None => match pw::current() { Some((_, d)) => d, None => return path.to_string() },
        }
    } else {
        match pw::home_of(&path[1..i]) { Some(d) => d, None => return path.to_string() }
    };
    let out = format!("{}{}", userhome.trim_end_matches('/'), &path[i..]);
    if out.is_empty() { "/".to_string() } else { out }
}

/// Elsewhere, Python's ntpath.expanduser: a leading `~` (alone, or before `/` or `\\`) is
/// USERPROFILE, else HOMEDRIVE+HOMEPATH; HOME is not consulted (Python stopped in 3.8). `home`
/// is ignored here -- it stands for the POSIX variable -- so that the hooks, the MCP server and
/// this console find one settings file on Windows too. `~user` is left literal.
#[cfg(not(unix))]
pub fn expand_user(path: &str, _home: Option<&str>) -> String {
    let rest = match path.strip_prefix('~') {
        Some(r) if r.is_empty() || r.starts_with('/') || r.starts_with('\\') => r,
        _ => return path.to_string(),
    };
    // Set counts, even set to "": Python checks `'USERPROFILE' in os.environ`, not its value.
    let home = std::env::var("USERPROFILE").ok().or_else(|| {
        let path = std::env::var("HOMEPATH").ok()?;
        Some(format!("{}{path}", std::env::var("HOMEDRIVE").unwrap_or_default()))
    });
    match home { Some(h) => format!("{h}{rest}"), None => path.to_string() }
}

/// Python's os.path.abspath: a relative path joins the working directory, then `.` and `..` are
/// folded lexically (symlinks are not resolved, as there).
fn abs_path(p: &str) -> String {
    use std::path::{Component, Path, PathBuf};
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
    out.to_string_lossy().into_owned()
}

pub fn posix_home() -> Option<String> {
    std::env::var_os("HOME").map(|h| h.to_string_lossy().into_owned())
}

/// This machine's HyperMnesia directory, as hooks/_mem_common.py resolves HOME_DIR and the MCP
/// server its hm_home(): `abspath(expanduser(HYPERMNESIA_HOME or "~/.HyperMnesia"))`; an empty
/// variable is unset.
pub fn hm_home() -> PathBuf {
    PathBuf::from(hm_home_from(std::env::var("HYPERMNESIA_HOME").ok().as_deref(),
                               posix_home().as_deref()))
}

fn hm_home_from(var: Option<&str>, home: Option<&str>) -> String {
    let raw = var.filter(|s| !s.is_empty()).unwrap_or("~/.HyperMnesia");
    abs_path(&expand_user(raw, home))
}

/// The home when it is not the default one -- what an autostart entry or a client config has to
/// carry, since the process it starts gets none of this shell's variables.
pub fn non_default_home() -> Option<PathBuf> {
    let home = hm_home();
    (home != PathBuf::from(hm_home_from(None, posix_home().as_deref()))).then_some(home)
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

    /// The same cases the MCP server's tests pin for its copy of these functions, and what
    /// Python's abspath(expanduser()) answers for each.
    #[test]
    fn the_home_is_resolved_like_the_loader() {
        assert_eq!(abs_path("/a/./b/../c"), "/a/c");
        assert_eq!(PathBuf::from(abs_path("x/../y")), std::env::current_dir().unwrap().join("y"));
        assert_eq!(hm_home_from(None, Some("/u/x")), "/u/x/.HyperMnesia");
        assert_eq!(hm_home_from(Some(""), Some("/u/x")), "/u/x/.HyperMnesia", "empty is unset");
        assert_eq!(hm_home_from(Some("~/y/./z"), Some("/u/x/")), "/u/x/y/z");
        assert_eq!(hm_home_from(Some("/abs/hm"), None), "/abs/hm");
        #[cfg(unix)]
        {
            // HOME unset: the passwd home of this uid, not "/".
            let (name, pw_home) = pw::current().expect("this uid has a passwd entry");
            let pw_home = pw_home.trim_end_matches('/').to_string();
            assert_eq!(hm_home_from(None, None), format!("{pw_home}/.HyperMnesia"));
            // `~user` is that user's passwd home; an unknown user stays literal (then abspath).
            assert_eq!(hm_home_from(Some(&format!("~{name}/hm")), Some("/elsewhere")),
                       format!("{pw_home}/hm"));
            assert_eq!(hm_home_from(Some("~no-such-user-hm/x"), Some("/u")),
                       format!("{}/~no-such-user-hm/x", std::env::current_dir().unwrap().display()));
            // HOME set but empty: `~` is "", as in posixpath.
            assert_eq!(expand_user("~/hypermnesia.env", Some("")), "/hypermnesia.env");
        }
    }
}
