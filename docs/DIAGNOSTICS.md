# Diagnostics: the failures that still answer

A store like this has two kinds of fault. One kind is loud — the database is down, the binary is
missing, the query is malformed — and needs no document. The other kind returns a plausible
answer: search finds nothing because the embedder is down, the pre-edit hook injects nothing
because the map resolves to a scope that does not exist, an ingest indexes zero documents and
then prunes the corpus it should have refreshed. Nothing raises, exit codes are zero, and the
symptom is a system that looks fine and quietly answers worse.

This file lists what is checked, what you see when it trips, and why each check exists. It is a
reference, not a runbook: nothing here needs doing until something looks off.

## Check it on demand

```bash
./hm doctor           # or the `status` MCP tool, which runs this same script
```

| Check | What it catches | Why it is not obvious |
|---|---|---|
| Postgres reachable | the store is down | everything else would report zero, which reads as "empty" |
| Doc schema present | `sql/schema.sql` never loaded | ingest fails at load rather than at query time |
| Personal-memory schema | `sql/schema_mem.sql` never loaded | doc-RAG works fine without it, so the gap hides |
| **ANN index built** | the HNSW index was never created | search still returns correct results, by sequential-scanning every vector, forever |
| Every chunk embedded | a bulk embed that stopped halfway | unembedded chunks are still findable by the lexical leg, so recall drops without an error |
| **One embedding model** | two models' vectors in one table | vectors from different models are not comparable; a query embedded by one cannot find rows embedded by the other, and the symptom is "the corpus does not cover that" |
| The map's scopes | `HM_REPO` naming a scope nothing was ingested under | an exact, case-sensitive match: a folder called `Infra` ingested as `infra` resolves to nothing on every edit |
| That scope has `must` rules | a map with only `should`/`info` | the pre-edit hook injects `must` only, so it would stay silent by design |
| Embedder reachable | a down or unconfigured embedder | search degrades to lexical-only rather than failing |
| Reranker reachable | a down reranker, or one that idle-unloaded | search fails **open** to plain RRF, which looks like working search with worse ranking |

A non-zero exit means something in the FAIL column; warnings do not fail the command.

## Check the map against the tree

```bash
python3 ci/freshness.py <repo_dir> <scope>       # add --mark to flag stale docs in the DB
```

- **Orphan globs.** A component's `key_paths` glob that matches no file in the tree. The usual
  cause is a directory that moved: the glob stays, the component stops resolving, and every edit
  under the new path silently gets no invariants.
- **Stale documents.** Content on disk whose hash differs from what is stored. Search then
  answers from a version of the document that no longer exists.
- **A scope with no components at all.** Reported as `NO MAP` with the list of scopes that do
  exist, and a non-zero exit — because every other check in the file is `WHERE repo = <scope>`
  and would otherwise report a clean zero for a map that is entirely absent.

## What search tells you about itself

- `!! EMBEDDER UNREACHABLE (...) -- this search was LEXICAL-ONLY` — half the retrieval is gone.
  Few or no results does **not** mean the corpus lacks the topic.
- `!! RERANKER UNAVAILABLE (...) -- these results are in plain RRF order` — the cross-encoder did
  not run. Common cause is not an outage: the model unloads after `RERANK_IDLE_SEC` and the next
  cold request can exceed the timeout.
- `query_log` records every search and its result count from both entry points. `n_results = 0`
  is a corpus gap worth reading.

## What the pre-edit hook tells you

`hooks/arch_invariants.py` is fail-open: it never blocks an edit. It is not fail-*silent*. Each of
these is announced once per episode, not on every edit — a block that repeats on every edit is a
block people learn to skip:

- the store did not answer, so no invariants were injected (shared with `mem_recall`, so one
  outage produces one notice between them);
- the graph came back unparseable, which is a defect rather than an empty map;
- `HM_REPO` names a scope no component was ingested under, listed alongside the scopes that do
  exist.

And per call, when it applies: `(note: <path> maps to no component)` for a file the map does not
cover.

## What the MCP server tells you

- `(!) SCOPE: ...` on `get_project_map`, `get_document` and `search_docs` when the scope was
  guessed from the directory name or rewritten by the character filter. Both cases can produce a
  scope that matches nothing ingested. `get_constraints` and `locate` do not carry this line.
- `(!) STALE MAP: refresh failed (...)` when the component map could not be re-read and the
  answer comes from a cached copy, with the error and the copy's age. Past ten TTLs the cached
  copy stops being served at all.
- `(!) UNDELIVERABLE constraints in this repo ...` for a rule that is neither global nor attached
  to a component — the schema permits it, every "how many active rules" query counts it, and no
  path can ever receive it.
- A document over `HM_DOC_MAX_CHARS` comes back cut, with the cut stated in the returned text.

## What ingest refuses to do

- **Write an empty corpus.** A full ingest opens with `DELETE FROM documents WHERE repo = <tag>`,
  so an enumeration that returns nothing would mean "delete everything". It exits non-zero
  instead and names the way out (`--walk`, for a git-ignored tree).
- **Trust a snapshot from elsewhere.** The `--known-hashes` file is treated as ground truth about
  the database, so the emitted SQL asserts the scope still holds exactly as many documents as the
  snapshot describes and raises otherwise. That guard only reaches you if psql runs with
  `-v ON_ERROR_STOP=1`.
- **Silently drop what it could not read.** A file that exists but cannot be read this time is
  counted as present, so a transient error does not delete a good document. A path git lists that
  is *not* on disk is treated as deleted and said so — the usual cause is an unstaged deletion.

## Why psql always runs with `ON_ERROR_STOP`

Without it, psql prints the error, carries on to `COMMIT` (which becomes a rollback), and exits
**0**. A failed query then returns an empty string, indistinguishable from "nothing matched" — and
every guard inside the transaction, including the snapshot check above, is silenced along with it.
Every invocation in this repo's code and documentation sets it. If you write your own, set it too.

## What the console tells you about itself

The console is a tool for noticing silence, so silence in the console is the worst bug it can
have. What it says, and what each line means:

| On screen | What it means |
|-----------|---------------|
| `! not updated: <reason>, showing state from 4m ago` | the reading failed; these numbers are old and this is how old |
| `! no answer about a reading for 2m -- the reader is stuck` | the worker thread is alive and has stopped answering |
| `! the reader thread has died` | nothing will update again; restart the tray |
| `never ran (installed 3 d ago)` | launchd has started this job zero times and it has written no log |
| `! the period has already passed and launchd still never ran it` | a whole period and a half with no run — the complaint, as opposed to the observation above |
| `! it ran at some point, but the log has not moved for more than a period` | it worked once and stopped |
| `! unreadable plist: <plutil said>` | launchd refused this job too; it is not running |
| `! exit 2, 5m ago` | the last run failed. `freshness` exits 1 by design, meaning discrepancies were found |
| `! the hooks IGNORE this file entirely: <why>` | the settings file is out of force; the defaults are running |
| `file (IGNORED)` / `5 (file: 9)` | the file says 9, the default 5 is what is actually in effect |
| `this shell` as a source | set here, but launchd's jobs do not inherit it — the line below says what they use |
| `the answer parsed but has no "memories"` | something answered, but it was not this query's result. Not a store full of zeros |

Two readings that are NOT complaints, and are deliberately not marked: a job with no schedule
(`on demand`) never being "overdue", and a positive run counter with no log to date it by. The
run counter is per-bootstrap — launchd resets it at every login — so it can only ever prove that
something ran, never that nothing did.
