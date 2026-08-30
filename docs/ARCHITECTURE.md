# System architecture

HyperMnesia is one layer of a small stack. It owns **knowledge that persists** — docs, the
architectural map, and personal memory. It deliberately does **not** index code symbols; that
job belongs to a live language-server layer it sits next to. This page describes the whole
picture and how the pieces divide the work.

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
      "command": "/opt/hypermnesia/mcp-server/target/release/hypermnesia-mcp",
      "env": {
        "HM_REPO": "myrepo",
        "DATABASE_URL": "postgresql://hm:pass@localhost:5432/hypermnesia",
        "EMBED_BACKEND": "ollama",
        "HM_SEARCH":  "/opt/hypermnesia/ingest/search.py",
        "HM_MEM_OPS": "/opt/hypermnesia/ingest/mem_ops.py",
        "HM_RERANK":  "/opt/hypermnesia/rerank/search_reranked.py"
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

**Ingest (offline, per doc change):** `ingest_repo.py` walks the repo's markdown, chunks by
heading, and emits SQL; `embed_chunks.py` fills `chunks.embedding` via Ollama/TEI. The
architectural map is seeded once by hand (`examples/seed_example.sql`), refreshed when the
architecture changes.

**Retrieval (per agent request):** the MCP server resolves a path to its component and returns
constraints deterministically (Tier 0/1), or embeds a query and fuses vector + full-text via RRF,
optionally reranked (Tier 2). Fails open: embedder down → lexical-only; DB down → the tool
surfaces the error, never blocks the agent.

**Personal memory (background):** capture hooks enqueue session transcripts; a scheduled job
distills them to memories via a pluggable LLM; a daily consolidator merges near-duplicates behind
a confidence gate + review queue. Recall/profile hooks inject relevant memories into the prompt.

See [DESIGN.md](DESIGN.md) for the reasoning behind the tiers and [MEMORY.md](MEMORY.md) for the
personal-memory model.

## Related work

HyperMnesia is self-hosted memory + architectural control for **coding agents**, where you own
the store. That's a different niche from managed consumer-chat memory. For context, Anthropic's
[Claude memory update (Aug 2026)](https://www.anthropic.com/news) unified memory across Claude
chat and Cowork, on by default, with topic-by-topic view/edit/delete, real-time capture, and
sensitive-topic protection — a polished managed feature for end-user chat. HyperMnesia differs on
purpose: the store is your Postgres (inspectable and editable in SQL), it runs on local models
with no data leaving your box, it is bi-temporal with supersede-not-overwrite + a review gate, and
it is built around a coding agent's needs (doc-RAG + a deterministic constraint map), not chat.
Complementary philosophies, different problems.
