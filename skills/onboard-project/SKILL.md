---
name: onboard-project
description: Connect a repository to HyperMnesia (doc search + a Tier 0/1 rule map) and pair it with an LSP symbol server such as Serena. Use when adding a new project to the memory system, when an agent says it has no map for a repo, or when someone asks how to wire memory into a codebase.
---

# Onboard a repository to HyperMnesia + Serena

Six steps. Five are mechanical; **step 3 is the one that decides whether any of this is
worth it**, so budget your attention there.

The end state: before the agent edits `src/api/routes.py` it already knows the rules for that
file (no search, no model), it can find the doc that explains the auth flow, it remembers what
you decided last month — and it asks a language server, not a vector index, where `foo` is
defined.

## 0. Check the prerequisites

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c 'select 1'                  # store reachable
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c 'select count(*) from components'   # schema loaded
curl -s localhost:11434/api/tags | head -c 80        # embedder alive (Ollama; TEI: /health)
```

`-v ON_ERROR_STOP=1` on **every** psql call here, including the ones below: without it psql
prints the error to stderr and still exits 0, so a failed statement reads as an empty result —
the exact shape of failure this whole system exists to avoid.

If the schema is missing:
`psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/schema.sql -f sql/schema_mem.sql`.
If there is no store at all, `./hm init` brings up the Docker stack (Postgres + TEI, every
published port bound to `127.0.0.1`, a generated password in `deploy/docker/.env`), loads both
schemas, and prints the `DATABASE_URL` / `EMBED_BACKEND` lines to export.

Pick the repo tag now and use it everywhere — it is the scoping key for the whole tier and is
matched as an **exact string, case included**. Ingest rejects a tag outside `[A-Za-z0-9._-]`;
the hook and the MCP server strip every other character from `HM_REPO` (or from the cwd
basename, when `HM_REPO` is unset) but never change its case. So ingest under the tag you will
set as `HM_REPO`, and set `HM_REPO` in both places that resolve — the hook env (step 5) and the
MCP server env (step 4). A repo at `~/code/MyRepo` ingested as `myrepo` resolves only with
`HM_REPO=myrepo` set; without it the fallback is the directory basename `MyRepo`, which matches
nothing, forever, quietly. Both sides now say so: the MCP server prefixes its answer with a
`(!) SCOPE:` line when it guessed the name from the directory or had to rewrite it, and the hook
announces once per episode that nothing is mapped under the scope, listing the scopes that exist.
The server's note appears on `get_project_map`, `get_document` and `search_docs` — the three
whose answers the scope decides. `get_constraints` and `locate` do not carry it, so a wrong
scope there still looks like a repo with no rules.

## 1. Ingest the docs

```bash
./hm ingest /path/to/repo <repo-tag>
```

Prefer the wrapper. It shells the same scripts as the manual steps, in the order that works,
and it does two things whose failure mode is silent when you drive them by hand: when the scope
already holds documents it takes the known-hashes snapshot **from the database it is about to write to**
(otherwise a re-ingest deletes the scope's documents and every embedding with them), and it
creates the HNSW index after the first bulk embed rather than before. It then runs
`EMBED_REPO=<repo-tag> ingest/embed_chunks.py` and finishes with `ci/doctor.py`, so steps 1 and
2 are one command. It needs `DATABASE_URL` and `psql` on PATH, and refuses a scope outside
`[A-Za-z0-9._-]` before it touches the database.

Do it by hand when the store is reachable only from somewhere else — `ingest_repo.py` never
connects to a database, so it runs where the files are and the SQL is applied where the store is:

```bash
python ingest/ingest_repo.py /path/to/repo <repo-tag> out.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f out.sql
```

Markdown only, chunked by heading. Things to know before you trust the result:

- **A git-ignored corpus needs `--walk`.** `git ls-files` is the default enumeration, and it
  returns nothing for a directory that is inside a repo but `.gitignore`d — working notes and
  scratch docs, typically. The ingester refuses to write an empty corpus rather than emitting
  one that would delete what is already stored; pass `--walk` to index the tree directly.
  `./hm ingest` has no `--walk`, so that corpus has to go through the manual path.
- A full re-ingest **deletes and re-inserts** the repo's documents, so `constraints.source_doc_id`
  is reset to NULL (the FK is `ON DELETE SET NULL`). Re-apply the seed afterwards if you care
  about those links. It also drops every chunk (that FK cascades) and chunks carry the
  embeddings — so a re-ingest for one edited file re-embeds the whole corpus.
- **Re-ingesting by hand? Use `--known-hashes`** (`./hm ingest` already does this for you).
  Hand the ingester what the DB already holds and
  only new and changed files are rewritten; unchanged documents keep their chunks, their
  embeddings, and their `source_doc_id` links. The ingester still needs no DB connection —
  the hashes arrive as a file, so this works over `kubectl exec` or a tunnel like everything
  else here:

  ```bash
  psql "$DATABASE_URL" -tAX -v ON_ERROR_STOP=1 -F$'\t' \
    -c "SELECT path, content_hash FROM documents WHERE repo='<repo-tag>'" > known.tsv
  python ingest/ingest_repo.py /path/to/repo <repo-tag> out.sql --known-hashes known.tsv
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f out.sql
  ```

  Take the snapshot from the database you are about to load into. The SQL checks that the repo
  still holds as many documents as the snapshot describes and aborts if it doesn't — a snapshot
  from another environment would silently skip documents that database never had, leaving a
  corpus with holes that reads as "search finds nothing".
- **A file git lists but that is not on disk is treated as deleted**, and said so on stderr
  ("listed by git but not on disk"). The usual cause is a working-tree deletion that has not
  been staged: `git ls-files` reads the index, so it still names the file. Its stored document
  is pruned, which is what you want — but if you did not mean to delete it, restore it before
  ingesting rather than after.

## 2. Fill in the embeddings

(`./hm ingest` already ran this for the scope it ingested; do it separately for the manual path.)

```bash
EMBED_REPO=<repo-tag> python ingest/embed_chunks.py   # only rows where embedding IS NULL
```

Resumable — safe to interrupt and re-run. There is no "re-embed what already has a vector"
switch: the selection is always `embedding IS NULL`, narrowed by the environment below. To
recompute existing vectors you clear them first, as the model-change recipe does. What it reads
from the environment is `EMBED_REPO`
(comma-separated scopes; all repos when unset — worth setting, since the chunk text is the
request body sent to the embedder), `EMBED_BATCH` (16) and `SHARD_N`/`SHARD_I` to split one pass
across replicas, plus the usual `EMBED_BACKEND` / `OLLAMA_URL` / `TEI_URL` / `EMBED_MODEL`.

Rows are stamped with `embedding_model`, so changing embedder means clearing the differently
stamped rows yourself and re-running:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "UPDATE chunks c SET embedding=NULL FROM documents d
    WHERE d.id=c.document_id AND d.repo='<repo-tag>'
      AND c.embedding_model IS DISTINCT FROM '<new-model>'"
EMBED_MODEL=<new-model> EMBED_REPO=<repo-tag> python ingest/embed_chunks.py
```

`EMBED_MODEL` selects the model only on the Ollama backend. With `EMBED_BACKEND=tei` the request
carries no model name at all — the model is whichever one that TEI instance was started with, so
switching there means starting a different TEI, and `EMBED_MODEL` then only controls the string
stamped into `embedding_model`. Stamping a name the server is not serving is worse than not
stamping one, because the tripwire that detects a mixed store is exactly that column.

`chunks.embedding` is `vector(1024)` (`sql/schema.sql`), so only another 1024-dim model is a
drop-in; a different dimension needs a schema change, not a re-embed.

## 3. Author the Tier 0/1 map — the part that matters

Everything else is plumbing. This is the payload: a hand-written component graph with the
rules that apply to each area. Start from `examples/seed_example.sql`.

Guidance that comes from getting it wrong:

- **Write rules that change behaviour, not descriptions.** "No component other than the data
  layer may import the Postgres driver" is a rule. "The API layer handles HTTP" is a label; it
  costs tokens on every edit and changes nothing.
- **`must` is injected before every edit; `should` is not.** Only what genuinely blocks a
  change belongs at `must`, or the injection becomes noise people learn to skim.
- **`key_paths` globs decide everything.** `**` crosses `/` (and `**/` also matches zero
  segments, so `src/**/*.py` matches `src/main.py`), `*` and `?` do not cross `/`, and matching
  is exact-beats-longest-literal-prefix-beats-priority. Dot-prefixed paths (`.gitlab-ci.yml`,
  `.claude/**`) are ordinary paths here: unlike shell globbing, `*` and `**` match a leading
  dot, so no special-casing is needed. Only a leading `./` is stripped from the path being
  resolved.
- **Relationships pull one hop, in both directions.** Declaring `api depends_on db` means
  editing an API file also surfaces the data layer's rules — and editing a data-layer file
  surfaces the API's, because the expansion follows the edge either way regardless of `kind`.
  That is usually what you want, and it is also how a sloppy graph floods the agent with
  irrelevant rules.

Apply it and confirm the map answers for a real file:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f your_seed.sql
printf '{"tool_name":"Edit","cwd":"/path/to/repo","tool_input":{"file_path":"/path/to/repo/src/api/routes.py"}}' \
  | HM_REPO=<repo-tag> python hooks/arch_invariants.py
```

Empty output means no `must` was selected for that path — either the globs match nothing, or
what they match carries only `should`/`info`. Fix the globs now, because a silent Tier 1 is
worse than none: it looks like "no rules apply". Faults are announced instead of injected, but
only on their first occurrence: the flip is a marker file at `~/.claude/hypermnesia-<fault>`
(`store-down`, `graph-unparseable`, `no-repo-<tag>`), so delete it to see the notice again.

## 4. Wire both MCP servers

HyperMnesia answers "what are the rules / where is the doc / what do I know". An LSP server
answers "where is this symbol". Do not make either do the other's job — see
`docs/ARCHITECTURE.md`.

```json
{
  "mcpServers": {
    "hypermnesia": {
      "command": "/opt/hypermnesia/mcp-server/target/release/hypermnesia-mcp",
      "env": {
        "HM_REPO": "<repo-tag>",
        "DATABASE_URL": "postgresql://hm:pass@localhost:5432/hypermnesia",
        "EMBED_BACKEND": "ollama",
        "HM_SEARCH": "/opt/hypermnesia/ingest/search.py",
        "HM_MEM_OPS": "/opt/hypermnesia/ingest/mem_ops.py"
      }
    },
    "serena": {
      "command": "serena",
      "args": ["start-mcp-server", "--context", "claude-code", "--project", "/path/to/repo"]
    }
  }
}
```

## 5. Register the hooks

In the project's `.claude/settings.json` (see `hooks/README.md` for the full set and for
Codex CLI, which uses the same contract):

```json
{"hooks": {
  "PreToolUse":      [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": "HM_REPO=<repo-tag> python3 /opt/hypermnesia/hooks/arch_invariants.py", "timeout": 20}]}],
  "SessionStart":    [{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_profile.py"}]}],
  "UserPromptSubmit":[{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_recall.py"}]}]
}}
```

The constraint hook injects context and **must not** return a `permissionDecision` — emitting
`allow` there silently auto-approves every edit that happens to have a `must`.

## 6. Verify, then walk away

```bash
HM_REPO=<repo-tag> ./hm doctor            # unbuilt ANN index, unembedded chunks, unmapped scope
python tests/test_hook_contract.py        # hook I/O shape (hooks are fail-open = silent when broken)
python eval/mem_probes.py                 # staleness / abstention / temporal / recall
python ci/freshness.py /path/to/repo <repo-tag>   # globs that match no file; docs behind HEAD
python ingest/search.py "how does X work" 5 <repo-tag>   # a real question you know the answer to
```

`freshness.py` exits 1 on any map orphan, and also when the scope has no components at all —
the case where every other count would read as a clean zero. Everything it does without
`--mark` is a SELECT; `--mark` writes (`documents.status='stale'`). `freshness.py` is the one
to schedule. The map is the asset and it rots silently: when a
directory moves and its glob does not, Tier 1 stops resolving and says "no rules" with total
confidence. Run it in the repo's CI.

## Adapting to your deployment

Only *how the scripts reach the store* changes; the six steps do not.

| Deployment | What changes |
|---|---|
| **Laptop** (Ollama + local Postgres) | Nothing. `DATABASE_URL` + `EMBED_BACKEND=ollama` as above. (`./hm init` instead brings up the Docker stack, whose embedder is TEI — export the `EMBED_BACKEND=tei TEI_URL=...` line it prints.) |
| **Single server** (compose + TEI) | `EMBED_BACKEND=tei`, `TEI_URL`. Run ingest/embed on the box, or over a tunnel. |
| **Kubernetes** | The store is not reachable from your laptop. Either port-forward for ingest, or ship the scripts into the cluster and run them there (`kubectl exec`). Hooks then need a transport wrapper instead of a direct `DATABASE_URL`; keep it fail-open. If you multiplex ssh, put `%r@%h:%p` in the `ControlPath` — a fixed socket path silently reuses one host's connection for every destination. |
| **CPU-only / no reranker** | Skip `rerank/`. Bulk embedding is the only heavy step; do it once, on the best machine you have, and let the query path embed short strings. |

Whatever the transport, keep every hook fail-open: a memory system that can block an edit is
worse than no memory system. But make an outage *visible* at session start — otherwise "the
store is down" is indistinguishable from "nothing relevant is stored", and a machine can run
with zero memory for weeks without anyone noticing.
