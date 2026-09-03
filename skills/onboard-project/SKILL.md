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
psql "$DATABASE_URL" -c 'select 1'                  # store reachable
psql "$DATABASE_URL" -c 'select count(*) from components'   # schema loaded
curl -s localhost:11434/api/tags | head -c 80        # embedder alive (Ollama; TEI: /health)
```

If the schema is missing: `psql "$DATABASE_URL" -f sql/schema.sql -f sql/schema_mem.sql`.

Pick the repo tag now and use it everywhere — it is the scoping key for the whole tier and
is matched **case-sensitively**. `~/Work/Infra` ingested as `infra` will not resolve unless
`HM_REPO=infra` is set explicitly; the hook falls back to the cwd basename, which would be
`Infra`.

## 1. Ingest the docs

```bash
python ingest/ingest_repo.py /path/to/repo <repo-tag> out.sql
psql "$DATABASE_URL" -f out.sql
```

Markdown only, chunked by heading. Two things to know before you trust the result:

- A full re-ingest **deletes and re-inserts** the repo's documents, so `constraints.source_doc_id`
  is reset to NULL (the FK is `ON DELETE SET NULL`). Re-apply the seed afterwards if you care
  about those links.
- The chunker reads `# ` at line start as a heading even inside a ``` fence, so a runbook full
  of YAML comments can pick up a phantom heading path. Check a few chunks of your most
  code-heavy doc before assuming the corpus is clean.

## 2. Fill in the embeddings

```bash
python ingest/embed_chunks.py            # only touches rows where embedding IS NULL
```

Resumable — safe to interrupt and re-run. Rows are stamped with `embedding_model`; if you
ever change embedder, re-embed only the rows whose stamp differs rather than the whole corpus.

## 3. Author the Tier 0/1 map — the part that matters

Everything else is plumbing. This is the payload: a hand-written component graph with the
rules that apply to each area. Start from `examples/seed_example.sql`.

Guidance that comes from getting it wrong:

- **Write rules that change behaviour, not descriptions.** "No component other than the data
  layer may import the Postgres driver" is a rule. "The API layer handles HTTP" is a label; it
  costs tokens on every edit and changes nothing.
- **`must` is injected before every edit; `should` is not.** Only what genuinely blocks a
  change belongs at `must`, or the injection becomes noise people learn to skim.
- **`key_paths` globs decide everything.** `**` crosses `/`, `*` does not, and matching is
  exact-beats-longest-prefix-beats-priority. Dot-prefixed paths (`.gitlab-ci.yml`,
  `.claude/**`) are ordinary paths here and must be listed explicitly if you want them mapped.
- **Relationships pull one hop.** Declaring `api depends_on db` means editing an API file also
  surfaces the data layer's rules. That is usually what you want, and it is also how a sloppy
  graph floods the agent with irrelevant rules.

Apply it and confirm the map answers for a real file:

```bash
psql "$DATABASE_URL" -f your_seed.sql
printf '{"tool_name":"Edit","cwd":"/path/to/repo","tool_input":{"file_path":"/path/to/repo/src/api/routes.py"}}' \
  | HM_REPO=<repo-tag> python hooks/arch_invariants.py
```

Empty output means the path matched no component — fix the globs now, because a silent Tier 1
is worse than none: it looks like "no rules apply".

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
python tests/test_hook_contract.py        # hook I/O shape (hooks are fail-open = silent when broken)
python eval/mem_probes.py                 # staleness / abstention / temporal / recall
python ci/freshness.py /path/to/repo <repo-tag>   # globs that match no file; docs behind HEAD
python ingest/search.py "how does X work" 5       # a real question you know the answer to
```

`freshness.py` is the one to schedule. The map is the asset and it rots silently: when a
directory moves and its glob does not, Tier 1 stops resolving and says "no rules" with total
confidence. Run it in the repo's CI.

## Adapting to your deployment

Only *how the scripts reach the store* changes; the six steps do not.

| Deployment | What changes |
|---|---|
| **Laptop** (Ollama + local Postgres) | Nothing. `DATABASE_URL` + `EMBED_BACKEND=ollama` as above. |
| **Single server** (compose + TEI) | `EMBED_BACKEND=tei`, `TEI_URL`. Run ingest/embed on the box, or over a tunnel. |
| **Kubernetes** | The store is not reachable from your laptop. Either port-forward for ingest, or ship the scripts into the cluster and run them there (`kubectl exec`). Hooks then need a transport wrapper instead of a direct `DATABASE_URL`; keep it fail-open. If you multiplex ssh, put `%r@%h:%p` in the `ControlPath` — a fixed socket path silently reuses one host's connection for every destination. |
| **CPU-only / no reranker** | Skip `rerank/`. Bulk embedding is the only heavy step; do it once, on the best machine you have, and let the query path embed short strings. |

Whatever the transport, keep every hook fail-open: a memory system that can block an edit is
worse than no memory system. But make an outage *visible* at session start — otherwise "the
store is down" is indistinguishable from "nothing relevant is stored", and a machine can run
with zero memory for weeks without anyone noticing.
