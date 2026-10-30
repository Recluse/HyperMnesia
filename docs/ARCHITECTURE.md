# System architecture

HyperMnesia is one layer of a small stack. It owns **knowledge that persists** — docs, the
architectural map, and personal memory. It deliberately does **not** index code symbols; that
job belongs to a live language-server layer it sits next to. This page describes the whole
picture and how the pieces divide the work.

This is the logical architecture. For placement and operational tradeoffs, continue with
[deployment architectures](DEPLOYMENT.md); for one-process-at-a-time commands, use
[running components separately](COMPONENTS.md).

## The three layers

```mermaid
flowchart TB
    AGENT["🤖 AI coding agent<br/>(Claude Code / any MCP client)"]

    subgraph code["⚙️ Code layer · live, no index to maintain"]
        SER["Serena (or any LSP-backed symbol MCP)"]
        LSP["language servers<br/>pyright · gopls · rust-analyzer · tsserver · ..."]
        SER --- LSP
    end

    subgraph know["📚 Knowledge layer · HyperMnesia"]
        HM["MCP server"]
        PG[("Postgres + pgvector<br/>docs · map · mem.*")]
        HM --- PG
    end

    AGENT -->|"where is symbol X? who calls it?"| SER
    AGENT -->|"what rules apply here? where's the doc? what do I know?"| HM
    SER -->|"live symbols, defs, refs"| AGENT
    HM -->|"constraints · doc chunks · memories"| AGENT
```

- **Code layer (LSP symbols).** A language server already builds and maintains a precise index
  of your code — definitions, references, call hierarchy, types — and updates it as you type.
  Exposed to the agent through an MCP wrapper (we use [Serena](https://github.com/oraios/serena)),
  it answers *structural* questions about code with zero staleness. HyperMnesia does not duplicate
  this: re-embedding source on every edit would be wasteful and always a step behind the LSP.
- **Knowledge layer (HyperMnesia).** Everything that is *not* live code structure and that you
  want to persist and retrieve:
  - **docs** (markdown) → chunked, embedded (bge-m3), searched by hybrid RRF (Tier 2);
  - **architectural map** → a hand-authored component/constraint graph, resolved deterministically
    from a file path (Tier 0/1) — "what rules apply to this file, before I edit it";
  - **personal memory** → durable, bi-temporal facts/preferences/decisions distilled from sessions
    and injected back.
- **Agent.** The MCP client (Claude Code or any) talks to both servers. Neither knows about the
  other; they compose in the client.

## Division of labor

| Question the agent has | Answered by | Why there |
|---|---|---|
| "Where is `foo` defined? Who calls it? What's its type?" | **Serena / LSP** | live index, always fresh, no re-embed |
| "What rules / invariants apply to `src/api/routes.py`?" | **HyperMnesia** Tier 0/1 | deterministic map, no model in the loop |
| "Where's the doc explaining the auth flow?" | **HyperMnesia** Tier 2 | hybrid RRF over doc chunks |
| "What did the owner decide about deploys, and why?" | **HyperMnesia** memory | bi-temporal, supersede-not-overwrite |
| "What are this repo's components at a glance?" | **HyperMnesia** project map | Tier 0 |

Rule of thumb: **live code → the LSP layer; anything you want to remember or that lives in prose →
HyperMnesia.** Code that is *stable and worth explaining* (architecture, invariants) is captured
once in the map/docs; code that *changes constantly* stays with the LSP.

## Pairing them (one MCP client, two servers)

Both are just MCP servers. In Claude Code's `.mcp.json`:

```json
{
  "mcpServers": {
    "hypermnesia": {
      "command": "/home/you/.HyperMnesia/current/bin/hypermnesia-mcp",
      "env": {
        "HM_REPO": "myrepo",
        "DATABASE_URL": "postgresql://hm:pass@localhost:5432/hypermnesia",
        "EMBED_BACKEND": "ollama",
        "HM_SEARCH":  "/home/you/.HyperMnesia/current/ingest/search.py",
        "HM_MEM_OPS": "/home/you/.HyperMnesia/current/ingest/mem_ops.py",
        "HM_RERANK":  "/home/you/.HyperMnesia/current/rerank/search_reranked.py"
      }
    },
    "serena": {
      "command": "serena",
      "args": ["start-mcp-server", "--context", "claude-code", "--project", "/path/to/myrepo"]
    }
  }
}
```

Any LSP-backed symbol MCP works in place of Serena; HyperMnesia doesn't depend on which one.

## Data flow

**Ingest (offline, per doc change):** `ingest_repo.py` enumerates the repo's tracked markdown (`git ls-files`; `--walk` for a tree kept out of git), chunks by
heading, and emits SQL; `embed_chunks.py` fills `chunks.embedding` via Ollama/TEI and exits
non-zero if any batch could not be embedded (those chunks stay lexical-only, and every search over
the scope says how many there are). The
architectural map is seeded once by hand (`examples/seed_example.sql`), refreshed when the
architecture changes.

**Retrieval (per agent request):** the MCP server resolves a path to its components and returns
constraints deterministically (Tier 0/1), or embeds a query and fuses vector + full-text via RRF,
optionally reranked (Tier 2). Fails open: embedder down → lexical-only; DB down → the tool
surfaces the error, never blocks the agent. Each tool call runs on its own worker thread, so a
minutes-long `memory_ask` does not delay the `get_constraints` behind it, and an MCP
`notifications/cancelled` stops the call's child process group.

**Tier 1 semantics, exactly:** resolution is *additive*. A file gets the rules of every
component whose `key_paths` match it — `src/**` and `src/api/**` both apply to `src/api/x.py` —
plus the global rules, plus one hop along the relationships of the same repo. That is the
fail-safe reading for `must` rules: a broad rule is never switched off because a narrower glob
also matched, so write rules that hold together rather than overrides. Specificity — exact path,
then the number of literal characters in the glob, then `priority`, then slug — only orders the
output (`**/migrations/*.sql` lists before `src/**` for a migration file). The hop is undirected
and ignores `kind`: kinds are free text with no declared direction, and pulling a neighbour's
rules is the safer mistake. Edges are matched by component id, never by slug, so two repos that
reuse slugs cannot leak rules into each other; an edge whose endpoints sit in different repos is
never followed, and freshness reports it as an error. Paths are resolved against the repo root
(the hook: `CLAUDE_PROJECT_DIR`, else the git top level of the session's cwd, else the cwd; the
server: `HM_ROOT`, else its own git top level, else its cwd — whose folder name is also the default
scope on both sides). Resolution is physical: symlinks are followed and `..` steps out of what a
symlink points at, from the deepest existing ancestor, so a file not created yet still resolves.
A path outside the root — absolute elsewhere, `../other/x.py`, a drive-qualified path against a
POSIX root — is reported as outside, matches no component and brings none of the repo's global
rules; otherwise `**/*.py` would hand one repo's rules to another repo's files. Containment is
case-sensitive (on Linux `/work/App` and `/work/app` are different trees), except where the
differently-cased prefix is the very same directory on disk, which is what a case-insensitive
filesystem reports. The hook and the server are tested against one shared fixture,
`tests/fixtures/tier1_cases.json`.

**Constraint injection (per edit, unprompted):** a `PreToolUse` hook (`hooks/arch_invariants.py`,
matcher `Edit|Write|MultiEdit`) resolves the file about to be edited to its component and injects
the applicable `must` invariants as `additionalContext` — so Tier 1 reaches the agent
deterministically, without it having to think to call the `get_constraints` tool. This is the
architectural-memory counterpart to the personal-memory recall hook; both make delivery, not just
storage, the point. Fail-open: no DB / no match → the hook stays silent and the edit proceeds.

**Keeping the map honest:** the hand-authored map is only trustworthy while it tracks the tree.
`ci/freshness.py` makes decay mechanical. It fails on component globs matching no file (a moved
file that silently unhooked its constraints — the worst case, since the map then lies more
confidently than search would) and on edges between components of different repos. It reports
docs whose indexed content differs from disk; constraints whose source document changed since
the rule was written, or no longer exists (a constraint names its source by
`source_repo`/`source_path`/`source_hash`, which survives a re-ingest — the row-id link alone was
cleared by exactly the edit it should have flagged; `--accept-sources` records the new hash once
someone has re-read the rule); and, for information, components with no constraint, constraints
that can never be delivered, and tracked files inside a mapped directory that no component
matches. Run it in CI or on a schedule against each target repo; it refuses before any of those
checks if no component is mapped under the scope it was given — every check is scoped by `repo`,
so a mistyped or renamed scope would otherwise report zero of everything and pass.

**Personal memory:** capture hooks enqueue session transcripts; a scheduled job distills them to
memories via a pluggable LLM; a consolidator merges near-duplicates behind a review gate; and
reflect builds one project knowledge page from shared rows. Perspective edges state who observed
whom, evidence edges record why a derived row exists, and materialized peer cards make global
routing cheap. `memory_context` performs deterministic, budgeted depth selection. The optional
Dreamer can propose new deductions or patterns, but only review approval can write them. The
Dialectic takes one bounded context and returns cited claims or an abstention — see `MEMORY.md`.

**Configuration (shared):** the tunables the passes read — thresholds, model names, batch sizes —
come from one file, `~/.HyperMnesia/hypermnesia.env`, loaded on import by `hooks/_mem_common.py` and
`ingest/_common.py`. A variable already in the environment beats it, so a one-off run needs no
edit. The loader refuses the file entirely unless it is the owner's private file in a private
directory, and accepts only an exact list of names: the file must not be able to choose an
interpreter, a destination or a credential. See the table in [INSTALL.md](INSTALL.md).

**Operator's view (optional):** `console/` is a menu-bar tray and four command-line tools over the
same store — volumes, scheduled jobs, the settings above, and a first-run wizard. It reaches the
database through one setting, a command that receives SQL on stdin, so it works against a local
psql, a container, or a pod without knowing which. It is the surface where this system's own
failures are meant to become visible; [DIAGNOSTICS.md](DIAGNOSTICS.md) lists every complaint it
can print.

See [DESIGN.md](DESIGN.md) for the reasoning behind the tiers and [MEMORY.md](MEMORY.md) for the
personal-memory model.

## One model, two runtimes: the embedder

Worth spelling out, because it is the single easiest way to break a store like this silently.

Every vector in the database — document chunks and memories alike — must come from **the same
embedding model**. Vectors from two models do not live in the same space, so a query embedded by
one cannot find rows embedded by the other. Nothing errors. Search simply gets worse, in a way
that reads as "the corpus does not cover that" rather than as a bug, and it stays broken until
someone re-embeds everything.

A concrete deployment, as an example of how to satisfy that while still using the right hardware
for each job:

| | Serving (per query) | Bulk (re-index, onboarding a repo) |
|---|---|---|
| Model | `BAAI/bge-m3` | `BAAI/bge-m3` |
| Runtime | text-embeddings-inference 1.7, CPU, in-cluster | Ollama, Apple Silicon |
| Shape | float32, 1024 dims, 8192-token input | same model, F16 weights |
| Why here | one query at a time, must always be up | ~10 chunks/s; a bulk run on the CPU replica takes 8-9 cores and starves live queries |

The split exists because the CPU replica that answers queries is easily saturated: one bulk client
pushed it to 8-9 cores and ordinary queries stopped fitting their timeout. Moving bulk work to a
local GPU/Neural-Engine runtime keeps serving responsive.

**The split is only safe because both sides run the same model, and that is worth verifying rather
than assuming.** Take a few chunks that were embedded locally, re-embed their exact text through
the serving runtime, and compare:

```sql
SELECT embedding <=> '[...vector from the serving runtime...]'::vector FROM chunks WHERE id = 52396;
```

Cosine distance came back at ~1e-5 — float rounding, not a difference in meaning — so the vectors
are interchangeable. A different model would have shown a distance in the tenths.

`chunks.embedding_model` and `mem.memories.embedding_model` record which model produced each
vector, so a silent swap becomes a visible mixture:

```sql
SELECT embedding_model, count(*) FROM chunks GROUP BY 1;   -- expect exactly one row
```

If that query ever returns two rows, retrieval quality is already degraded for part of the store,
and the fix is to re-embed the minority — not to tune the ranking.

## Related work

HyperMnesia is self-hosted memory + architectural control for **coding agents**, where you own
the store. That's a different niche from managed consumer-chat memory. For context, Anthropic's
[Bringing memory to Claude](https://claude.com/blog/memory) (11 September 2025, updated 23 October
2025) gives Claude's own apps a memory that is **optional** rather than on by default, with a
memory summary you can read and edit, memories scoped per project, incognito chats that are never
remembered, and import/export. HyperMnesia differs on purpose: the store is your Postgres
(inspectable and editable in SQL), it runs on local models with no data leaving your box, it is
bi-temporal with supersede-not-overwrite + a review gate, and it is built around a coding agent's
needs (doc-RAG + a deterministic constraint map), not chat. Complementary philosophies, different
problems.

For the neighbours that solve this same problem, see [COMPARISON.md](COMPARISON.md).
