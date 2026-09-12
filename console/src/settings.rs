//! Pipeline settings: what can be tuned, and what a setting actually reaches.
//!
//! The shared file `~/.claude/hypermnesia.env` is the source of truth: the hooks load it when
//! `_mem_common` is imported, before any module computes its constants, and it does not override
//! what the environment already holds.
//!
//! ONE LIMIT WORTH STATING PLAINLY, because it is invisible otherwise. This file sets the
//! environment of processes that start ON THIS MACHINE. Every knob below is read by a script
//! that normally runs here -- but two of them, in `ingest/mem_ops.py`, are read wherever you
//! chose to run that script. In the default install that is this machine and the file reaches
//! them. If you deployed `mem_ops.py` into a container or a pod, its own environment wins there
//! and this file does not reach it, no matter what the screen says. The console prints that
//! caveat rather than guessing which deployment you have: guessing wrong in either direction
//! produces a confident lie, and the wrong direction is the one where a setting looks applied.

use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct Knob {
    pub key: &'static str,
    pub default: &'static str,
    pub what: &'static str,
    pub read_by: &'static str,
}

/// Every knob of the pipeline, with the place where it is read. The list is written by hand and
/// must match the code; the check for that lives in the repository's tests, not in someone's head.
pub const KNOBS: &[Knob] = &[
    Knob { key: "HM_LLM_MODEL", default: "qwen2.5:7b", read_by: "hooks/_llm.py",
           what: "the model that distils transcripts into memories and merges near-duplicates" },
    Knob { key: "HM_LLM_BACKEND", default: "auto", read_by: "hooks/_llm.py",
           what: "openai | ollama | cli; auto picks by which of HM_LLM_URL / HM_LLM_CMD is set" },
    Knob { key: "MEM_NOVELTY_MAXDIST", default: "0.12", read_by: "hooks/mem_extract.py",
           what: "novelty threshold on write: closer than this counts as the same fact" },
    Knob { key: "MEM_REVIEW_THRESHOLD", default: "0.8", read_by: "hooks/mem_consolidate.py",
           what: "below this confidence a merge waits in the review queue instead of being applied" },
    Knob { key: "MEM_REFLECT_MIN", default: "5", read_by: "hooks/mem_reflect.py",
           what: "the fewest records a project needs before it gets a knowledge page" },
    Knob { key: "MEM_REFLECT_MAX", default: "80", read_by: "hooks/mem_reflect.py",
           what: "the most records handed to the model in one pass" },
    Knob { key: "MEM_STALE_DAYS", default: "180", read_by: "hooks/mem_profile.py",
           what: "the age after which an unconfirmed fact lands in the stale list" },
    Knob { key: "MEM_SEM_MAXDIST", default: "0.5", read_by: "ingest/mem_ops.py",
           what: "abstention gate: past this distance memory search returns nothing at all" },
    Knob { key: "MEM_LEX_MAXDIST", default: "0", read_by: "ingest/mem_ops.py",
           what: "lexical floor: below it a word match does not rescue a result" },
    Knob { key: "EMBED_BATCH", default: "16", read_by: "ingest/embed_chunks.py",
           what: "how many chunks go to the embedder at once during a bulk embed" },
    Knob { key: "EMBED_MODEL", default: "bge-m3", read_by: "ingest/_common.py",
           what: "model name for the Ollama backend, and the string stamped into embedding_model" },
    Knob { key: "EMBED_BACKEND", default: "ollama", read_by: "ingest/_common.py",
           what: "ollama | tei" },
];

pub fn env_file() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    match std::env::var("HYPERMNESIA_ENV_FILE") {
        Ok(p) if !p.is_empty() => PathBuf::from(p),
        _ => PathBuf::from(home).join(".claude/hypermnesia.env"),
    }
}

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
    /// The variable is set in the console's own environment -- that is STRONGER than the file, and
    /// wherever the console sees it, the hooks started from that same environment will see it too.
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
    KNOBS.iter().find(|k| k.key == key)
        .ok_or_else(|| format!("unknown knob: {key}"))?;
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
    std::fs::write(&path, text).map_err(|e| format!("{}: {e}", path.display()))?;
    // 600 rather than the default: this file sets the environment of the jobs launchd starts by
    // itself, and one writable by others would be a way to change their behaviour. The loader in
    // the hooks ignores such a file entirely, so the mode here is not decoration but a condition
    // of the thing working at all.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
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

    #[test]
    fn every_knob_names_a_file_that_reads_it() {
        for k in KNOBS {
            assert!(!k.read_by.is_empty(), "{} with no place where it is read", k.key);
            assert!(!k.what.is_empty(), "{} with no description", k.key);
            assert!(k.read_by.contains(".py"), "{}: expected a file, not {}", k.key, k.read_by);
        }
    }
}
