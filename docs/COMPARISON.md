# Where HyperMnesia fits (and where it doesn't)

HyperMnesia sits in a narrow slot: **not chat memory** — self-hosted knowledge for a *coding*
agent. A hand-authored map of file→component→rules, plus doc-RAG, plus bi-temporal personal
memory, all in your own Postgres. The neighbours below solve adjacent problems well; almost none
of them do the deterministic **Tier 1** (path → component → `must`/`should`, no model in the loop).

Facts below were checked against each project's own repo/site (links inline). External projects
change — treated as **"as of 2026-08"**, and self-reported benchmark claims are labelled as such.

## Same shelf

| Project | Bet | Store | License | Not what HyperMnesia is |
|---|---|---|---|---|
| **HyperMnesia** | file rules before an edit + docs + personal memory | Postgres + pgvector | MIT | doesn't index code; map is hand-authored |
| [agentmemory](https://github.com/rohitg00/agentmemory) | auto-capture coding-agent sessions | SQLite + [iii-engine](https://github.com/iii-hq/iii) | Apache-2.0 | no deterministic component/constraint map |
| [Vestige](https://github.com/samvallad33/vestige) | "find the cause of a bug, not the lookalike" | SQLite + FTS5 + HNSW | AGPL-3.0 | FSRS decay, single 25MB binary |
| [Hindsight](https://github.com/vectorize-io/hindsight) | an agent that learns (retain/recall/reflect) | Postgres+pgvector / cloud | MIT | conversational/enterprise memory at its core |
| [mem0](https://github.com/mem0ai/mem0) | memory API for any agent | library / self-host / cloud | Apache-2.0 | not about repo invariants |
| [supermemory](https://github.com/supermemoryai/supermemory) | "the memory API" | cloud (backend closed) | SDKs open; backend proprietary | hosted API, not your SQL |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | memory as Markdown on disk | Markdown files (MCP) | AGPL-3.0 | plain-text transparency, weaker search |
| [Claude memory](https://www.anthropic.com/news/memory) | managed chat memory | Anthropic-hosted | proprietary | not your SQL, not a code map |

Notes on the neighbours worth stating plainly:

- **agentmemory** is the closest market neighbour: hooks, hybrid search, SessionStart injection,
  "don't re-explain." It deliberately does **not** index code either — it *recommends pairing with
  external code-graph tools* (e.g. [codegraph](https://github.com/colbymchenry/codegraph), a
  separate third-party project it doesn't ship). That's the same split HyperMnesia makes with an
  LSP/Serena. Its headline efficiency claim is self-reported (~1.9k tokens/session per its README).
- **Vestige** is closest in philosophy ("this isn't RAG") but bets differently: novelty-gated
  writes and FSRS-6 decay. HyperMnesia rejects decay on purpose (see below).
- **Hindsight** is a heavier animal — fact/experience/observation/mental-model networks, RRF + a
  cross-encoder, "knowledge pages" as a living wiki, and an `npx` installer for coding agents. It
  **claims** state-of-the-art on the [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
  benchmark; that's a vendor claim, and supermemory claims #1 on the same benchmark, so read
  "SOTA" as contested. HyperMnesia doesn't play on that field — and shouldn't, if the niche is
  "control over a repo," not "SOTA chat memory."
- **Name collision (unverified):** a separate "Hypermnesia" (SQLite, local-first, "keep session
  decisions, drop stale ones") has reportedly been announced by Taylor Weibley. We could not
  verify it from a primary source (LinkedIn is auth-gated) and found no matching public GitHub
  repo — treat it as unconfirmed. Different project, possibly the same word.

## Where HyperMnesia is ahead

Deterministic `path → component → must/should` plus a 1-hop graph. Elsewhere "rules" are a
recovered snippet or an LLM-recalled fact. Asked *"what may not be imported into
`src/api/routes.py`?"*, HyperMnesia answers **without a model**; similarity-based stores retrieve
something that looks related. Beyond that: abstention instead of top-k noise, supersede-with-
history instead of overwrite, a review queue gating merges, fail-open, and an explicit refusal to
embed code. The map lives in SQL — readable and editable, not trapped in a model's head.

## Non-goals and honest limitations

Being straight about the edges, because a deterministic map that has gone stale lies more
confidently than search does:

- **The map is hand-authored — that's the value *and* the liability.** While the seed tracks the
  tree, Tier 1 is excellent. When files move and globs don't, it silently stops resolving. This is
  why [`ci/freshness.py`](../ci/freshness.py) exists (orphan-glob / stale-doc / dangling-source
  detection) — run it in CI or on a schedule so decay is *loud*.
- **Ingest is markdown-only.** Decisions that live in PRs, commits, or chat don't reach the map by
  themselves — someone has to write them into docs or the seed.
- **No forgetting.** Personal memory accumulates in `active_memories`; there's no decay curve, so
  quality rests on extraction + the review queue, not on eviction. Deliberate (an agent's "never
  do X here" shouldn't fade), but it means volume is managed by consolidation, not time.
- **Consolidation is pairwise cosine over active memories** — simple, and O(n²) as memory grows.
  Fine for a personal/homelab store; a larger deployment would want blocking/ANN before this bites.
- **Operationally heavier** than `npx`-a-single-binary neighbours: Postgres + an embedder
  (Ollama/TEI) + the Rust MCP server. Good for "own your store," not for "install and forget."

## Summary

Nearest by product is **agentmemory**; by local-first spirit, **Vestige**; by memory-as-a-system,
**Hindsight**. HyperMnesia's distinctive bet isn't RAG and isn't hooks — it's that **a file's
rules are data, not a retrieval result.** While the map is alive that's an advantage; the day the
globs fall behind, the others still return "something similar" and HyperMnesia would return a
confident wrong answer — which is exactly why the freshness check and the hand on the seed matter.
