//! Pipeline settings: what can be tuned, and what a setting actually reaches.
//!
//! The shared file `~/.claude/hypermnesia.env` is the source of truth: the hooks load it when
//! `_mem_common` is imported, before any module computes its constants, and it does not override
//! what the environment already holds.
//!
//! ONE LIMIT WORTH STATING PLAINLY, because it is invisible otherwise. This file sets the
//! environment of processes that start ON THIS MACHINE. The loader is called on import by both
//! `hooks/_mem_common.py` and `ingest/_common.py`, which between them are imported by every
//! reader in the list below -- but a script started inside a container or a pod reads that
//! container's environment, and this file never gets there. The console prints that caveat
//! rather than guessing which deployment you have: guessing wrong in either direction produces a
//! confident lie, and the wrong direction is the one where a setting looks applied.
//!
//! The other limit is the file's own permissions. The loader ignores the file ENTIRELY unless it
//! is the owner's private file in the owner's private directory, so a mode the console does not
//! check is a screenful of values that are not in force. `fault()` below is the same verdict,
//! reached the same way.

use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct Knob {
    pub key: &'static str,
    pub default: &'static str,
    pub what: &'static str,
    pub read_by: &'static str,
    /// What a valid value looks like. Every numeric knob is parsed at module level in its
    /// reader, outside any try, so a value that is not a number is not a degraded setting -- it
    /// is an import-time crash in a hook that promises to fail open.
    pub kind: Kind,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Kind {
    Int,
    Float,
    /// One of a fixed set of words.
    OneOf(&'static [&'static str]),
    /// Anything non-empty on one line (a model name, a URL).
    Text,
}

impl Kind {
    /// Why this value is not acceptable for this knob, if it is not.
    pub fn fault(&self, v: &str) -> Option<String> {
        if v.trim().is_empty() {
            return Some("an empty value is not a setting; use `unset` to go back to the default"
                        .into());
        }
        if v.contains(['\n', '\r']) {
            // One line per setting, and the file is parsed line by line: a newline in a value
            // would appear on the next read as a second KEY=value line, walking straight around
            // the unknown-key refusal that exists so a key with no reader cannot land in the file.
            return Some("a value cannot contain a line break".into());
        }
        match self {
            Kind::Int => v.parse::<i64>().err().map(|_| format!("{v:?} is not a whole number")),
            Kind::Float => v.parse::<f64>().err().map(|_| format!("{v:?} is not a number")),
            Kind::OneOf(opts) => (!opts.contains(&v))
                .then(|| format!("{v:?} is not one of: {}", opts.join(", "))),
            Kind::Text => None,
        }
    }
}

/// Every knob of the pipeline, with the place where it is read. The list is written by hand and
/// must match the code; the check for that lives in the repository's tests, not in someone's head.
pub const KNOBS: &[Knob] = &[
    Knob { key: "HM_LLM_MODEL", default: "qwen2.5:7b", read_by: "hooks/_llm.py",
           what: "the model that distils transcripts into memories and merges near-duplicates",
           kind: Kind::Text },
    Knob { key: "HM_LLM_BACKEND", default: "auto", read_by: "hooks/_llm.py",
           what: "openai | ollama | cli; auto picks by which of HM_LLM_URL / HM_LLM_CMD is set",
           kind: Kind::OneOf(&["auto", "openai", "ollama", "cli"]) },
    Knob { key: "MEM_NOVELTY_MAXDIST", default: "0.12", read_by: "hooks/mem_extract.py",
           what: "novelty threshold on write: closer than this counts as the same fact",
           kind: Kind::Float },
    Knob { key: "MEM_REVIEW_THRESHOLD", default: "0.8", read_by: "hooks/mem_consolidate.py",
           what: "below this confidence a merge waits in the review queue instead of being applied",
           kind: Kind::Float },
    Knob { key: "MEM_REFLECT_MIN", default: "5", read_by: "hooks/mem_reflect.py",
           what: "the fewest records a project needs before it gets a knowledge page",
           kind: Kind::Int },
    Knob { key: "MEM_REFLECT_MAX", default: "80", read_by: "hooks/mem_reflect.py",
           what: "the most records handed to the model in one pass",
           kind: Kind::Int },
    Knob { key: "MEM_STALE_DAYS", default: "180", read_by: "hooks/mem_profile.py",
           what: "the age after which an unconfirmed fact lands in the stale list",
           kind: Kind::Int },
    Knob { key: "MEM_SEM_MAXDIST", default: "0.5", read_by: "ingest/mem_ops.py",
           what: "abstention gate: past this distance memory search returns nothing at all",
           kind: Kind::Float },
    Knob { key: "MEM_LEX_MAXDIST", default: "= MEM_SEM_MAXDIST", read_by: "ingest/mem_ops.py",
           what: "lexical floor: unset, it follows MEM_SEM_MAXDIST; set, it stands on its own",
           kind: Kind::Float },
    Knob { key: "EMBED_BATCH", default: "16", read_by: "ingest/embed_chunks.py",
           what: "how many chunks go to the embedder at once during a bulk embed",
           kind: Kind::Int },
    Knob { key: "EMBED_MODEL", default: "bge-m3", read_by: "ingest/_common.py",
           what: "model name for the Ollama backend, and the string stamped into embedding_model",
           kind: Kind::Text },
    Knob { key: "EMBED_BACKEND", default: "ollama", read_by: "ingest/_common.py",
           what: "ollama | tei",
           kind: Kind::OneOf(&["ollama", "tei"]) },
];

pub fn env_file() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    match std::env::var("HYPERMNESIA_ENV_FILE") {
        Ok(p) if !p.is_empty() => PathBuf::from(p),
        _ => PathBuf::from(home).join(".claude/hypermnesia.env"),
    }
}

/// Why the hooks will ignore this file, if they will.
///
/// The same verdict `hooks/_mem_common.py` reaches, by the same rule: the file has to be the
/// owner's own, unwritable by anyone else, in a directory with the same property. The console
/// never measured it before, so a file the pipeline ignored ENTIRELY was displayed knob by knob
/// as "file" -- a screenful of values that were not in force anywhere.
#[cfg(unix)]
pub fn fault() -> Option<String> {
    use std::os::unix::fs::MetadataExt;
    extern "C" { fn getuid() -> u32; }
    let path = env_file();
    let md = std::fs::metadata(&path).ok()?;   // no file: nothing is in force, and that is fine
    let me = unsafe { getuid() };
    let mode = md.mode() & 0o777;
    if mode & 0o022 != 0 {
        return Some(format!("mode {mode:o}: another account can write it"));
    }
    if md.uid() != me {
        return Some(format!("owned by uid {}, not by you ({me})", md.uid()));
    }
    let dir = path.parent()?;
    let dmd = std::fs::metadata(dir).ok()?;
    if dmd.mode() & 0o022 != 0 || dmd.uid() != me {
        return Some(format!("{} can be written by others, so the file can be replaced",
                            dir.display()));
    }
    None
}

#[cfg(not(unix))]
pub fn fault() -> Option<String> { None }

/// What is in the settings file. The order is not preserved: the file is rewritten whole.
pub fn read() -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    let Ok(text) = std::fs::read_to_string(env_file()) else { return out };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') { continue; }
        if let Some((k, v)) = line.split_once('=') {
            out.insert(k.trim().to_string(),
                       v.trim().trim_matches('"').trim_matches('\'').to_string());
        }
    }
    out
}

#[derive(Debug, Clone, PartialEq)]
pub enum Source {
    /// A value from the settings file.
    File(String),
    /// The variable is set in THIS SHELL's environment -- stronger than the file, but only for
    /// what this shell starts. launchd gives its jobs a minimal environment with none of a login
    /// shell's exports, so for the scheduled half of the pipeline the file's value is the one in
    /// force. Saying "environment" without that was telling people to disregard the value the
    /// extract, consolidate and reflect jobs actually use.
    Env(String),
    /// Nothing is set; the default value from the code is in effect.
    Default,
}

/// A knob's effective value and where that value came from. Showing the source is mandatory: a
/// setting written in the file and overridden by the environment otherwise looks as if it applied.
pub fn effective(k: &Knob, file: &BTreeMap<String, String>) -> (String, Source) {
    if let Ok(v) = std::env::var(k.key) {
        if !v.is_empty() {
            return (v.clone(), Source::Env(v));
        }
    }
    match file.get(k.key) {
        Some(v) => (v.clone(), Source::File(v.clone())),
        None => (k.default.to_string(), Source::Default),
    }
}

/// Write a knob into the settings file. The file is rewritten whole, with mode 600.
pub fn set(key: &str, value: &str) -> Result<(), String> {
    // Refuse an unknown key rather than write it: a typo that lands in the file silently does
    // nothing, and the screen would then show a setting nobody reads.
    let knob = KNOBS.iter().find(|k| k.key == key)
        .ok_or_else(|| format!("unknown knob: {key}"))?;
    // And refuse a value the reader cannot parse. Every numeric knob is parsed at module level
    // in its hook, outside any try: a bad value is not a weaker setting, it is an import-time
    // crash in a hook whose whole contract is to fail open.
    if let Some(why) = knob.kind.fault(value) {
        return Err(format!("{key}: {why}"));
    }
    let mut file = read();
    file.insert(key.to_string(), value.to_string());
    write_all(&file)
}

/// Remove a knob from the file -- go back to the default value.
pub fn unset(key: &str) -> Result<(), String> {
    let mut file = read();
    if file.remove(key).is_none() {
        return Err(format!("{key} is not in the settings file in the first place"));
    }
    write_all(&file)
}

fn write_all(map: &BTreeMap<String, String>) -> Result<(), String> {
    let path = env_file();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    }
    let mut text = String::from(
        "# Shared settings of the memory pipeline. The hooks read this file at startup.\n\
         # A variable set in the environment is stronger than a value from here.\n\
         # Edited from the console: hypermnesia-settings set <KEY> <value>\n");
    for (k, v) in map {
        text.push_str(&format!("{k}={v}\n"));
    }
    // Written to a temporary file and renamed over the target, not truncated in place. A hook
    // importing at the wrong moment would otherwise read a half-written file -- and this module
    // and the loader both state, in so many words, that a partly-applied file is worse than none.
    // The rename is atomic; it also means the file never exists at an open mode, not even briefly.
    //
    // 600 rather than the default: this file sets the environment of the jobs launchd starts by
    // itself, and one writable by others would be a way to change their behaviour. The loader in
    // the hooks ignores such a file entirely, so the mode here is not decoration but a condition
    // of the thing working at all -- which is why a failure to set it is an error, not a shrug.
    let tmp = path.with_extension("env.tmp");
    #[cfg(unix)]
    {
        use std::io::Write as _;
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new()
            .write(true).create(true).truncate(true).mode(0o600)
            .open(&tmp).map_err(|e| format!("{}: {e}", tmp.display()))?;
        f.write_all(text.as_bytes()).map_err(|e| format!("{}: {e}", tmp.display()))?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("{}: could not make it private ({e}); the hooks would ignore \
                                  a file others can write", tmp.display()))?;
    }
    #[cfg(not(unix))]
    std::fs::write(&tmp, &text).map_err(|e| format!("{}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, &path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("{}: {e}", path.display())
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A key nobody reads must be refused rather than written. A typo that lands in the file
    /// does nothing, and the screen would then show a setting with no reader as if it applied.
    #[test]
    fn an_unknown_key_is_refused() {
        assert!(set("NOT_A_KNOB_AT_ALL", "1").is_err());
        assert!(set("MEM_REFLECT_MINN", "9").is_err(), "a typo must not be written");
    }

    /// The list is written by hand, so it can drift away from the code it describes. This is the
    /// cheap half of not drifting: every knob must at least name a file that exists in this
    /// repository. Whether that file really reads the variable is checked by
    /// tests/test_settings_knobs.py, which greps for it.
    #[test]
    fn every_knob_points_at_a_file_in_this_repo() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf();
        for k in KNOBS {
            let f = root.join(k.read_by);
            assert!(f.exists(), "{}: {} does not exist", k.key, k.read_by);
        }
    }

    #[test]
    fn env_beats_file_and_file_beats_default() {
        let knob = KNOBS.iter().find(|k| k.key == "MEM_REFLECT_MIN").unwrap();
        let mut file = BTreeMap::new();
        assert_eq!(effective(knob, &file), ("5".into(), Source::Default));
        file.insert("MEM_REFLECT_MIN".into(), "9".into());
        assert_eq!(effective(knob, &file), ("9".into(), Source::File("9".into())));
    }

    /// A value the reader cannot parse is not a weaker setting: every numeric knob is parsed at
    /// module level in its hook, outside any try, so a bad value is an import-time crash in a
    /// hook whose whole contract is to fail open.
    #[test]
    fn a_value_the_reader_would_choke_on_is_refused() {
        let int = KNOBS.iter().find(|k| k.key == "MEM_REFLECT_MIN").unwrap();
        assert!(int.kind.fault("5").is_none());
        assert!(int.kind.fault("soon").is_some());
        assert!(int.kind.fault("5.5").is_some(), "a whole number means a whole number");

        let float = KNOBS.iter().find(|k| k.key == "MEM_SEM_MAXDIST").unwrap();
        assert!(float.kind.fault("0.42").is_none());
        assert!(float.kind.fault("close").is_some());

        let word = KNOBS.iter().find(|k| k.key == "EMBED_BACKEND").unwrap();
        assert!(word.kind.fault("tei").is_none());
        assert!(word.kind.fault("Tei").is_some(), "the reader lowercases nothing on the way in");

        // An empty value is not a setting, and `unset` is the way back to the default.
        assert!(KNOBS[0].kind.fault("").is_some());
        // A line break would appear on the next read as a second KEY=value line, walking around
        // the unknown-key refusal that keeps a key with no reader out of the file.
        assert!(KNOBS[0].kind.fault("qwen\nEVIL=1").is_some());
        assert!(set("MEM_REFLECT_MIN", "not-a-number").is_err());
    }

    /// Every knob's stated default has to be one its own kind accepts, or the screen is showing
    /// a value `set` would refuse.
    #[test]
    fn every_default_is_valid_for_its_kind() {
        for k in KNOBS {
            if k.default.starts_with('=') {
                continue;                     // "= MEM_SEM_MAXDIST": follows another knob
            }
            assert!(k.kind.fault(k.default).is_none(),
                    "{}: the default {:?} is not valid for its own kind", k.key, k.default);
        }
    }

    #[test]
    fn every_knob_names_a_file_that_reads_it() {
        for k in KNOBS {
            assert!(!k.read_by.is_empty(), "{} with no place where it is read", k.key);
            assert!(!k.what.is_empty(), "{} with no description", k.key);
            assert!(k.read_by.contains(".py"), "{}: expected a file, not {}", k.key, k.read_by);
        }
    }
}
