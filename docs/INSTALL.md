# Installing HyperMnesia

HyperMnesia is a set of small, boring parts around **one Postgres database**:

- **Postgres 16 + pgvector >= 0.8.3** — the only required datastore (vectors + full-text + the
  structural model all live here). Per pgvector's own CHANGELOG: 0.8.2 fixed a buffer overflow in
  parallel HNSW index builds, **0.8.3 fixed possible index corruption with HNSW vacuuming**, and
  0.8.4 fixed an `hnsw graph not repaired` error. This project's index is HNSW, so 0.8.3 is the
  real floor. (0.8.6 is current; its fixes are IVFFlat and cast issues, which do not affect this
  store. The compose file pins 0.8.5.)
- **An embedder** producing `bge-m3` (1024-dim) vectors — via **Ollama** (`bge-m3`, GPU/Apple-Silicon
  friendly) or **TEI** (HuggingFace text-embeddings-inference, CPU-capable). Both are cosine-compatible.
- **(Optional) a reranker** — `bge-reranker-v2-m3` cross-encoder for a precision boost on search.
- **(Optional) a small LLM** for distilling session transcripts into memories — any
  OpenAI-compatible endpoint, a local Ollama model, or a CLI like `codex`/`claude`.
- **An MCP client** — Claude Code (also drives the memory hooks), or any Model Context Protocol client.

Pick a deployment below. All four use the same schema and code; they differ only in **where the
Postgres and the embedder run**.

## The short version

If you have Docker and want the single-box stack, the wrapper runs the documented steps in the
order that works:

```bash
./hm init                        # .env with a generated password, compose up, both schemas loaded
export DATABASE_URL=...          # init prints the exact line
./hm ingest ~/code/myrepo myrepo # ingest -> embed -> build the ANN index -> check
./hm doctor                      # any time you suspect the store is answering worse than it should
```

`hm` is not a different way to install anything — it shells the same scripts as the manual steps
below. It exists because the *order* is load-bearing and getting it wrong is silent: the ANN index
must be built after the first bulk embed, and a re-ingest without a known-hashes snapshot deletes
the scope's documents and every embedding with them. `hm ingest` takes that snapshot for you when
the scope already has documents.

Read on for the manual steps, for the non-Docker paths, and for Kubernetes.

## Hardware / OS / software requirements

| | **A. Laptop / local** | **B. Single server** | **C. Kubernetes** | **D. CPU-only minimal** |
|---|---|---|---|---|
| **Use case** | dev, personal, 1 user | homelab / small team | existing cluster, HA-ish | cheapest, no GPU |
| **CPU** | 4+ cores | 4-8 cores | per-node | 2-4 cores |
| **RAM** | 8 GB (16 w/ reranker) | 8-16 GB | 8-10 GB for the embedder + 1 GB for the rest | 8 GB |
| **Disk** | ~5 GB + your corpus | ~10 GB | PV ~20 GB | ~3 GB |
| **GPU / accel** | Apple MPS or NVIDIA (nice, not required) | optional | optional | none |
| **OS** | macOS 13+ / Linux | Linux (Docker) | any k8s 1.27+ | Linux |
| **Embedder** | Ollama `bge-m3` | TEI or Ollama (compose) | TEI Deployment | TEI on CPU (~3-5 s/chunk) |
| **Reranker** | local (`rerank/server.py`, ~4 GB RAM when active) | optional compose service | optional Deployment | **off** (RRF only) |
| **Postgres** | docker (pgvector image) | docker-compose | in-cluster, local PV | docker |

> **The RAM is the embedder's, not the system's.** Measured on a live install holding 1387
> documents, 24975 chunks and 750 memories: Postgres sits at **172 MiB** resident and the pod
> that runs the Python tools at **9 MiB**. The embedder next to it — text-embeddings-inference
> serving `bge-m3` on CPU, with `--max-batch-tokens 4096 --max-client-batch-size 32` — sits at
> **6.5 GiB**. That is the whole hardware story, and it is why the row above got it wrong until
> someone measured: the store is cheap, the model is not. The model also does not have to live
> next to the store. Embed on a Mac or a GPU box, or point `OLLAMA_URL`/`TEI_URL` at something
> else; a query only ever embeds its own short text.

> **Bulk embedding is the only heavy step.** On CPU, `bge-m3` is ~3-5 s/chunk; on a GPU or Apple
> Silicon (Ollama) it's ~10-100x  faster. Embed once; queries only embed the (short) query text.
> If you have a GPU/Mac, do bulk embedding there even if you serve from a small CPU box.

---

## A. Laptop / local (Apple Silicon or Linux)

```bash
# 1. Postgres + pgvector (docker) — or a native install with the pgvector extension
# Bound to loopback and with a generated password, deliberately. `-p 5432:5432` is shorthand
# for 0.0.0.0 -- on a laptop that means every network you ever join can reach a Postgres whose
# `postgres` account is the cluster SUPERUSER, and this database holds your personal memory.
PGPW=$(openssl rand -hex 16)
docker run -d --name hm-pg -e POSTGRES_PASSWORD="$PGPW" -e POSTGRES_DB=hypermnesia \
  -p 127.0.0.1:5432:5432 pgvector/pgvector:0.8.5-pg16
export DATABASE_URL="postgresql://postgres:$PGPW@localhost:5432/hypermnesia"
echo "$DATABASE_URL"   # put this in your shell profile and your MCP client's env

# 2. Schema
# -v ON_ERROR_STOP=1 on every psql call here and below, deliberately: without it psql prints
# the error, keeps going and exits 0, so a failed statement is indistinguishable from success.
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/schema.sql -f sql/schema_mem.sql

# 2b. More than one person? See "Two people, one store" below and apply
#     sql/mem_multiuser.sql + sql/mem_app_role.sql BEFORE anyone writes anything.
#     One person: skip it, and add it later -- the migration backfills.

# 3. Embedder: Ollama
ollama pull bge-m3            # 1.2 GB
export EMBED_BACKEND=ollama   # http://localhost:11434

# 4. Ingest a repo's markdown, then embed
python ingest/ingest_repo.py ~/code/myrepo myrepo /tmp/myrepo.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f /tmp/myrepo.sql
python ingest/embed_chunks.py               # fills chunks.embedding

# 5. Build the ANN index — AFTER the first bulk embed, and do not skip it.
#    schema.sql leaves this commented on purpose: building HNSW before the rows exist is far
#    slower than building it once they do. Skip it and search still WORKS, so nothing complains
#    — the dense leg just sequential-scans every vector for the rest of the install's life.
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw \
  ON chunks USING hnsw (embedding vector_cosine_ops)"

# 6. (optional) reranker on your GPU/Mac
python -m venv rerank/.venv && rerank/.venv/bin/pip install torch transformers sentencepiece
rerank/.venv/bin/python rerank/server.py &  # 127.0.0.1:8091, lazy-loads, idle-unloads
                                            # HM_RERANK_BIND=0.0.0.0 to serve it beyond localhost

# 7. Point your MCP client at mcp-server (see "MCP client" below)

# 8. Check it. Every fault this catches leaves a system that still answers -- an unbuilt index,
#    unembedded chunks, two embedding models in one table, a scope name off by case.
python3 ci/doctor.py
```

## B. Single server (docker-compose) — recommended for a homelab

Everything (Postgres, TEI embedder, optional reranker) in one compose stack:

```bash
cp deploy/docker/.env.example deploy/docker/.env    # POSTGRES_PASSWORD has no default:
                                                   # openssl rand -hex 24  (hex, not base64 --
                                                   # a `/` breaks the postgresql:// URI)
docker compose -f deploy/docker/docker-compose.yml up -d          # + --profile rerank for the reranker
docker compose -f deploy/docker/docker-compose.yml exec postgres \
  psql -v ON_ERROR_STOP=1 -U hm -d hypermnesia -f /sql/schema.sql -f /sql/schema_mem.sql
```

Then ingest/embed as in A steps 4 and 5 — including the ANN index, which is easy to miss and
costs you a sequential scan on every query if you do. Point the embedder at TEI first, since this
stack has no Ollama: `export EMBED_BACKEND=tei TEI_URL=http://localhost:8080`. See
`deploy/docker/docker-compose.yml` for the services and ports. TEI serves `bge-m3` on CPU; add a
GPU runtime to the compose service for speed.

Every published port binds `${BIND_ADDR:-127.0.0.1}`, so out of the box the stack is reachable
from this box only. That is the intended default: Postgres holds the personal-memory store, and
neither TEI nor the reranker has any authentication. Set `BIND_ADDR` in `.env` only when you mean
to serve the stack to another machine, and put something in front of it when you do.

## C. Kubernetes

The stack is small; adapt the compose services into your own manifests. **[deploy/k8s/README.md](../deploy/k8s/README.md)**
describes the shape: a Postgres+pgvector Deployment with a PVC (pin to a node if the PV is
node-local), a TEI `bge-m3` Deployment, ingest/embed as one-shot Jobs, and the MCP server
running client-side reaching Postgres via a port-forward or routable Service. Load the schema
with `kubectl exec ... psql -v ON_ERROR_STOP=1 < sql/schema.sql`.

## D. CPU-only minimal (no GPU, no reranker)

Same as B but skip the reranker (`HM_RERANK` unset -> search returns plain RRF, which already
fuses vector + full-text). Use TEI on CPU for embeddings. This is the cheapest, fully-functional
setup; you lose the reranker's precision bump but nothing else.

---

## MCP client

Add HyperMnesia's MCP server to your client. For Claude Code, in the repo's `.mcp.json`
(absolute paths; the server shells to `psql` and the bundled python scripts):

```json
{ "mcpServers": { "hypermnesia": {
  "command": "/opt/hypermnesia/mcp-server/target/release/hypermnesia-mcp",
  "env": {
    "HM_REPO": "myrepo",
    "DATABASE_URL": "postgresql://hm:pass@localhost:5432/hypermnesia",
    "EMBED_BACKEND": "ollama",
    "HM_SEARCH":  "/opt/hypermnesia/ingest/search.py",
    "HM_MEM_OPS": "/opt/hypermnesia/ingest/mem_ops.py",
    "HM_RERANK":  "/opt/hypermnesia/rerank/search_reranked.py"
  } } } }
```

Build the server once: `cd mcp-server && cargo build --release`. Needs the `psql` client on
PATH (Tier 0/1 map/constraints/get_document read the DB via `psql "$DATABASE_URL"`); the
`HM_SEARCH`/`HM_MEM_OPS` scripts need `psycopg2` on `HM_PYTHON` (default `python3`). Omit
`HM_RERANK` to skip reranking (plain RRF).

### Other MCP clients

The server is a plain **stdio** MCP server. It has no client-specific behaviour, reads its whole
configuration from the environment above, and speaks newline-delimited JSON-RPC — so any client
that can launch a stdio MCP server runs it with the same `command` and `env` shown above. Only the
file the block goes in differs; check your client's own MCP documentation for that path, since
those move and a list here would go stale without anyone noticing.

It answers `initialize` with `protocolVersion` `2024-11-05` whatever the client asked for, because
JSON-RPC batching is mandatory from `2025-03-26` and this server does not implement it — echoing a
later revision would promise something the code does not do. A client that sends a batch anyway
gets an explicit refusal rather than silence.

Verify a client is really talking to it by calling the `status` tool: it answers from the store,
so a reply proves the whole chain, not just that the process started.

**What does not port: the hooks.** `PreToolUse` injection, per-prompt recall and the session
profile are Claude Code features, and they are the part that makes this more than a search index —
they deliver without being asked. In a client with no hook mechanism you get the same data through
the same tools, but only when the agent decides to call one, which is exactly the weakness the
hooks exist to remove. If your client supports any pre-edit or pre-prompt extension point, wire
`hooks/arch_invariants.py` into it: it reads one JSON object on stdin (`tool_name`, `cwd`,
`tool_input.file_path`) and prints one JSON object, so adapting it is a matter of renaming fields.
If it supports none, tell the agent in its system prompt to call `get_constraints` before editing
and accept that it will sometimes forget.

## Personal-memory hooks (optional, Claude Code)

To auto-capture/inject personal memory, register the hooks in Claude Code settings — see
[docs/MEMORY.md](MEMORY.md) for the SessionStart / UserPromptSubmit / SessionEnd wiring and the
`mem_extract` / `mem_consolidate` schedule. Configure the distiller via `HM_LLM_BACKEND` + `HM_LLM_URL`/`HM_LLM_MODEL` (OpenAI-compatible),
`HM_LLM_MODEL` (Ollama), or `HM_LLM_CMD` (a CLI) — see `hooks/_llm.py`. Everything is fail-open: if the store is unreachable, the agent keeps working.

## Keeping the index current

`./hm ingest <dir> <scope>` is the update command as well as the first one. It is incremental when
the scope already holds documents: it takes a content-hash snapshot from the database it is about
to write to, re-emits only what changed, and leaves every other document's embeddings alone. So
"refresh" is the same line you ran the first time.

```bash
./hm ingest ~/code/myrepo myrepo            # tracked files, via git ls-files
./hm ingest ~/notes mynotes --walk          # a folder git does not track, or is told to ignore
```

`--walk` matters more than it looks: without it a git-ignored directory enumerates to nothing, and
the ingester refuses to write an empty corpus rather than deleting the scope it was asked to
refresh.

Three ways to run it without remembering to:

| When | How |
|------|-----|
| on every commit / pull | a `post-commit` and `post-merge` hook in the repo, calling the line above |
| on a timer | cron, a systemd timer, or a launchd agent; `hypermnesia-jobs` lists them on both macOS and Linux, and reschedules them on macOS |
| in CI | a step on push, if the runner can reach the database |

A timer is the simplest and a git hook is the better one: it runs when something actually changed,
and costs nothing when nothing did.

**How you learn it has drifted** rather than assuming: `ci/freshness.py <repo> <scope>` reports
documents whose ingested commit is behind HEAD, and component globs that match no file. `hm
doctor` covers the other half -- a missing index, unembedded chunks, two embedding models in one
store. Neither needs a schedule to be useful, but both are worth one.

## The console (optional, menu bar / system tray + CLI)

`console/` is the operator's side: what the store holds, whether the scheduled passes are running,
and what the tunables are set to.

```bash
cd console && cargo build --release
./target/release/hypermnesia-setup          # the walkthrough, including the connection
./target/release/hypermnesia-stats          # the numbers, once
./target/release/hypermnesia --install      # the tray, at login (launchd on macOS, a systemd user unit on Linux)
```

The four command-line tools build the same way everywhere; the data layer has no dependencies at
all. The tray itself needs a GUI toolkit, declared per platform so a machine with none of it
installed still builds everything else:

```bash
# macOS: the tray is part of the default build.
cargo build --release

# Linux: behind a feature, since it needs GTK3 and libayatana-appindicator development headers
# that a plain `cargo build` should not have to assume are present.
sudo apt install libgtk-3-dev libayatana-appindicator3-dev libxdo-dev   # Debian/Ubuntu
sudo dnf install gtk3-devel libayatana-appindicator-gtk3-devel libxdo-devel  # Fedora
cargo build --release --features tray
```

**Linux backend:** systemd user units (`~/.config/systemd/user/*.{timer,service}`), read through
`systemctl --user show` with no date-parsing crate. Both platforms now read, run, reschedule and
arm/disarm jobs, and install the tray itself into autostart:

| | macOS (launchd) | Linux (systemd) |
|---|---|---|
| run a job now | `launchctl kickstart -k` | `systemctl --user start --no-block` |
| change a schedule | edit the plist, `plutil -lint`, bootout/bootstrap | edit the unit, `systemd-analyze --user verify`, daemon-reload + restart |
| arm / disarm | n/a — a loaded job is armed, full stop | `systemctl --user enable\|disable --now` (`hypermnesia-jobs enable\|disable`, and the tray's **Timers** submenu) |
| install the tray | `~/Library/LaunchAgents/com.hypermnesia.tray.plist`, `RunAtLoad` | `~/.config/systemd/user/hypermnesia-tray.service`, `WantedBy=default.target`, enabled but not started (avoids running a second tray beside the one already open) |

Every write is backed up, validated before it is reloaded, and read back afterward to confirm what
actually landed rather than trusting the exit code of the command that asked for it; any failure
along the way restores the backup and says, in words, whether the restore itself worked. See
[the console message table](DIAGNOSTICS.md#what-the-console-tells-you-about-itself) for the exact
Linux-side lines, and `HM_JOB_PREFIX` below for how job names differ (`hypermnesia-extract.timer`,
not `com.hypermnesia.extract.plist`).

It reaches the database through ONE setting: a command that receives SQL on stdin and prints
unaligned rows. The wizard offers the four usual shapes — direct psql, `docker exec`,
`kubectl exec`, ssh to a machine that has kubectl — and tries the command before saving it.

| File | What |
|------|------|
| `~/.config/hypermnesia/console.conf` | the console's own settings: `HM_PSQL_CMD`, `HM_JOB_PREFIX`. Created mode 600 |
| `~/.claude/hypermnesia.env` | the pipeline's shared tunables, the file `hypermnesia-settings` writes |

Both are **refused entirely** — loudly, falling back to defaults — unless the file is yours, is
unwritable by any other account, and sits in a directory with the same property. The first holds a
command run through `sh -c` at every refresh and at login with nobody present; the second sets the
environment of the jobs the service manager starts. `HM_PSQL_CMD` in the environment beats the
config file.

A password inside the psql command ends up in psql's argv, where any process running as you can
read it. `~/.pgpass` or `PGPASSWORD` in the command's own environment avoids that.

`~/.claude/hypermnesia.env` may set only the tunables in the table below plus the endpoint
variables; names that decide what gets EXECUTED (`HM_PYTHON`, `HM_MEM_OPS`, `HM_SEARCH`,
`HM_RERANK`, `HM_LLM_CMD`, `PATH`, `PYTHONPATH`) are refused by name. A variable already set in the
environment beats the file — but "the environment" means the shell that started the process, and
neither launchd nor a systemd user unit gives its jobs any of yours, so for the scheduled passes
the file is what is in force.

## Configuration reference

| Env | Default | Meaning |
|-----|---------|---------|
| `DATABASE_URL` | `postgresql://hm@localhost:5432/hypermnesia` | Postgres connection string. `hm ingest` refuses to run without it; the Python tools, the hooks and the MCP server fall back to that default |
| `EMBED_BACKEND` | `ollama` | `ollama` or `tei` |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `TEI_URL` | `http://localhost:8080` | TEI endpoint |
| `HM_RERANK` | unset | path to `rerank/search_reranked.py` (unset = RRF only) |
| `HM_RERANK_URL` | `http://127.0.0.1:8091` | reranker service; `ci/doctor.py` (and `status`) check it whenever either this or `HM_RERANK` is set, and say so when neither is |
| `HM_LLM_BACKEND` | auto | `openai` \| `ollama` \| `cli` (auto: openai if HM_LLM_URL set, else cli if HM_LLM_CMD, else ollama) |
| `HM_LLM_URL`/`HM_LLM_KEY` | — | OpenAI-compatible endpoint (memory extraction/consolidation) |
| `HM_LLM_MODEL` | `qwen2.5:7b` | model name for the `openai` and `ollama` backends |
| `HM_LLM_CMD` | — | CLI distiller (prompt appended as arg, text on stdin) |
| `HM_RERANK_BIND` | `127.0.0.1` | address the reranker listens on (the endpoint has no auth) |
| `HM_DB_TIMEOUT_SECS` | `30` | MCP server: ceiling on one `psql` call |
| `HM_SEARCH_TIMEOUT_SECS` | `60` | MCP server: ceiling on one search |
| `HM_MEMOPS_TIMEOUT_SECS` | `60` | MCP server: ceiling on one memory operation |
| `HM_STATUS_TIMEOUT_SECS` | `120` | MCP server: ceiling on `status` (every check inside is itself bounded) |
| `HM_GRAPH_TTL_SECS` | `300` | how long the MCP server may serve a cached component map before re-reading it |
| `HM_DOC_MAX_CHARS` | `60000` | `get_document` cap; past it the text is cut and the cut is announced |
| `HM_REPO` | cwd basename | which ingested scope this workspace is — an **exact**, case-sensitive match. Unset, the MCP server guesses it from the directory name and strips anything outside `[A-Za-z0-9._-]`; in both cases the scoped tools (`get_project_map`, `get_document`, `search_docs`) open their answer with a `(!) SCOPE:` line saying so |
| `HM_FTS_LANG` | `english` | the Postgres text-search configuration for the memory query; change it and the two search legs must agree |
| `MEM_EMBED_MAX_CHARS` | `12000` | how much of a memory's text is embedded (`ingest/mem_ops.py`) |
| `EMBED_QUERY_MAX_CHARS` / `EMBED_QUERY_TIMEOUT` | `12000` / `20` s | caps on embedding one query (`ingest/_common.py`) |

### The memory pipeline's tunables

These are the knobs `hypermnesia-settings` offers. They come from the environment, or from the
shared settings file below, or from the default — in that order.

| Env | Default | Read by | Meaning |
|-----|---------|---------|---------|
| `MEM_NOVELTY_MAXDIST` | `0.12` | `hooks/mem_extract.py` | novelty gate on write: closer than this counts as the same fact |
| `MEM_REVIEW_THRESHOLD` | `0.8` | `hooks/mem_consolidate.py` | below this confidence a merge waits in the review queue instead of being applied |
| `MEM_REFLECT_MIN` | `5` | `hooks/mem_reflect.py` | the fewest memories a project needs before it gets a knowledge page |
| `MEM_REFLECT_MAX` | `80` | `hooks/mem_reflect.py` | the most memories handed to the model in one pass |
| `MEM_STALE_DAYS` | `180` | `hooks/mem_profile.py` | the age past which an unconfirmed, unrecalled fact is listed as stale |
| `MEM_SEM_MAXDIST` | `0.5` | `ingest/mem_ops.py` | abstention gate: past this distance memory search returns nothing at all |
| `MEM_LEX_MAXDIST` | = `MEM_SEM_MAXDIST` | `ingest/mem_ops.py` | lexical floor; unset it follows the gate above |
| `EMBED_BATCH` | `16` | `ingest/embed_chunks.py` | chunks per request during a bulk embed |
| `EMBED_MODEL` | `bge-m3` | `ingest/_common.py` | model name for Ollama, and the string stamped into `embedding_model` |
| `MEM_CONSOLIDATE_MAXDIST` | `0.20` | `hooks/mem_consolidate.py` | how close two memories must be to be candidates for merging. Above ~0.25 the candidate graph starts collapsing into one blob; 0.35 meant "same topic" rather than "same fact" |
| `MEM_CONSOLIDATE_MAX_GROUP` | `6` | `hooks/mem_consolidate.py` | the most memories one verdict may act on. A merge replaces every member, so this bounds the damage a wrong verdict can do, independently of the model's confidence |
| `MEM_CONSOLIDATE_MAX_GROUPS` | `10` | `hooks/mem_consolidate.py` | groups examined per run; each one is an LLM call, and the rest wait for the next pass |
| `MEM_CONSOLIDATE_MODEL` | `haiku` | `hooks/mem_consolidate.py` | the model the consolidation verdict is asked of |

### The shared settings file

`~/.claude/hypermnesia.env` — one `KEY=value` file for the tunables above, written by
`hypermnesia-settings set <KEY> <value>`, loaded on import by `hooks/_mem_common.py` and
`ingest/_common.py`. `HYPERMNESIA_ENV_FILE` moves it. The rules it is read under are in
[The console](#the-console-optional-menu-bar--system-tray--cli) above: refused whole unless it is
your own private file, and only the exact names listed here are accepted.

### The console

| Env | Default | Meaning |
|-----|---------|---------|
| `HM_PSQL_CMD` | `psql "$DATABASE_URL" -tAX -v ON_ERROR_STOP=1` | the command the console sends SQL to on stdin. The one setting that differs between deployments |
| `HM_JOB_PREFIX` | `com.hypermnesia` (macOS) / `hypermnesia-` (Linux) | the label/unit-name prefix the job tools manage |
| `HM_CONSOLE_CONFIG` | `~/.config/hypermnesia/console.conf` | where the two settings above are stored. Refused whole if another account can write it, if it is not owned by you, or if it is not a plain file: it holds a command the tray runs at login. A refusal STOPS the tool — it does not fall back to the default command, because that reaches a different database and its numbers look exactly like the ones you asked for. A file that is simply absent is fine; the default applies then |
| `HM_TIMEOUT_SECS` | `30` | ceiling on one reading of the store, covering the whole exchange — writing the query, waiting, and collecting the answer. Capped at 3600; a value outside 1..3600 is ignored and the default applies |

## Two people, one store

A personal memory store stops being one person's the moment a second author can write to it.
`sql/mem_multiuser.sql` + `sql/mem_app_role.sql` make that safe. Apply them after
`schema_mem.sql` and before anyone else connects.

### Why not a namespace per person

The rows in this store are not all of one kind with respect to sharing. A `preference` is about
ONE person and is actively harmful injected into somebody else's session — they take another
person's habits for project rules. A `semantic` or `procedural` fact is about the PROJECT, and
a second engineer having memory is worth something precisely because what they learn reaches
everyone. A namespace each gives the worst of both: their findings reach nobody, and personal
preferences still need hiding by other means.

So every row records **`author`** (who learned it) and **`scope`** (`private` | `project`), and
**`mem.project_members`** says which projects an author may read the shared memory of.

*(Every agent-memory product we compared against does namespace isolation and none has a
visibility model: mem0 scopes by `user_id`/`agent_id`/`run_id`, Zep/Graphiti by `group_id`.
Zep's own documentation makes the point this design rests on — a namespace filter is not
authorization, and the identifier must never come from an untrusted request.)*

### Identity

`MEM_AUTHOR`, from `~/.claude/hypermnesia.env` (read by both the Python hooks and the Rust MCP
server, so one machine has one identity) or from the environment. It reaches Postgres as the
connection option `mem.reader`.

Without it, **reads** return only what is shared with projects you belong to and **writes** are
refused. Both fail closed, and the read failure is loud — your own preferences stop appearing in
your profile, which you notice immediately.

### Defaults, and what is refused

| | |
|---|---|
| `preference` | private |
| anything else with a project tag | shared with that project |
| anything with no project | private, whatever its type |
| supersede, `mark`, consolidation across authors | refused |
| publishing into a project you are not in | refused |
| a merge | inherits the group's author, project and *narrowest* scope |

Grant membership explicitly — there is no "all projects" row, so a new project is unshared
until someone says otherwise:

```sql
INSERT INTO mem.project_members (author, project) VALUES ('someone', 'the-tag');
```

The tag is matched exactly, so a case variant grants nothing and reports success. Copy it from
`SELECT DISTINCT project FROM mem.memories`.

### The rule is not only in a view

`mem_multiuser.sql` puts it in the function `mem.may_read`, called by the views **and** by the
callers that cannot use a view. That is necessary and not sufficient, which we established the
expensive way: an adversarial audit of the first version found four callers that reached the
base tables instead — a history flag swapping the view out, a fetch-by-id that checked the scope
but forgot membership, a write that accepted any project, and a retract that checked nothing.

`mem_app_role.sql` is what makes it hold anyway. `hm_app` is an ordinary role, so the row-level
policies bind it: a query that forgets the rule comes back with **no rows** instead of every
row. Point the application's `DATABASE_URL` at `hm_app`; keep the owning role for migrations,
restores, and the one view with no rule on it (`mem.all_active_memories`, which a post-restore
row count must use — `mem.active_memories` returns 0 for a connection with no identity, which
is correct and looks exactly like an empty restore).

If the application connects as the role that OWNS the tables, RLS is inert — owners bypass
their own policies and a superuser bypasses everything.

### Check that the boundary is up

```bash
# must print 0: no identity, no rows
psql "$DATABASE_URL" -tAXc "SELECT count(*) FROM mem.memories;"

# and the probe that fails if the rule is removed — it writes as one identity and reads as a
# second, which is the only arrangement that can tell an isolating store from one that is not
python3 eval/mem_probes.py
```
