# Hooks (Claude Code)

Optional. These wire auto-injection of HyperMnesia into Claude Code — the architectural map
(constraints) *and* personal memory. The store works without them (query via the MCP tools or
`ingest/mem_ops.py`); the hooks just make delivery hands-free so the agent doesn't have to think
to ask. All are **fail-open** — they never block a tool call — but none is fail-*silent*. On an unreachable store `mem_recall` announces the outage once (not on every prompt), `mem_profile` says the profile it served came from a stale cache, and `arch_invariants` announces a store that will not answer, a graph that will not parse, and a scope that maps to no component at all. Each of those is announced once per episode, because "this file has no rules" and "the rules never loaded" are otherwise the same silence — and the second one turns the feature off without telling anyone.

| Hook | Claude Code event | What it does |
|------|-------------------|--------------|
| `arch_invariants.py` | PreToolUse (`Edit\|Write\|MultiEdit`) | inject the `must` constraints for the file being edited (Tier 0/1) |
| `mem_profile.py` | SessionStart (`startup\|resume\|clear\|compact`) | inject the pinned profile |
| `mem_recall.py`  | UserPromptSubmit | inject memories relevant to the prompt (a `<private>...</private>` span is never part of the query) |
| `mem_capture.py` | SessionEnd, PreCompact | enqueue the transcript path |
| `mem_extract.py` | schedule (cron/systemd/launchd) | distill queued transcripts -> memories |
| `mem_consolidate.py` | schedule (daily) | merge/supersede near-duplicates: cliques only, bounded group size, low-confidence proposals parked for you |
| `mem_reflect.py` | schedule (daily) | synthesize per-project knowledge pages (regenerated, never stale) |
| `mem_dream.py` | schedule or manual | propose perspective-scoped derived memories; every candidate waits for explicit review |
| `mem_dialectic.py` | MCP `memory_ask` | return claims grounded in one bounded context, or abstain |
| `mem_review.py`  | you, manually | `list \| approve <id> \| reject <id>` the review queue; `stale [days]` lists active facts nothing has recalled in that long, to confirm or retire |

`arch_invariants.py` is what makes constraint delivery *deterministic and unprompted*: before an
edit, it resolves the file path to its component (Tier 1) and injects the applicable `must`
invariants as `additionalContext` — the architectural-memory counterpart to `mem_recall`. It reads
`DATABASE_URL` and `HM_REPO` (or the repo root's folder name), and shells `psql`. Paths are made
relative to the repo root — `CLAUDE_PROJECT_DIR` if the host sets it, else the git top level of
the session's cwd, else the cwd — so a session that has `cd`'d into `src/` still matches
`src/**` globs. A file outside that root (another checkout, `../x`, a `C:/...` path) gets none of
the repo's rules, and the hook says which root it resolved against. Every matching component applies (Tier 1 is additive; see
[ARCHITECTURE.md](../docs/ARCHITECTURE.md)), and the selection is the same as the
`get_constraints` tool's, checked against one shared fixture. If the store cannot be
reached, or the graph will not parse, or `HM_REPO` names a scope no component was ingested
under, it says so once and exits 0 — the edit is never blocked, but the agent is not left to
read the silence as "no rules apply". The scope case is worth stating plainly: the match is an
exact string, so a folder named `Infra` whose docs were ingested as `infra` resolves to nothing
on every edit, and the notice lists the scopes that do exist so the fix is obvious. It injects context and deliberately emits **no** `permissionDecision`: returning `allow` there would auto-approve every edit that happens to carry a `must`, bypassing the normal approval flow. "No opinion" is expressed by omitting the key, and a CI check pins that.

It injects only `must`, on purpose — `should` constraints are kept out of the every-edit context
for token budget; call the `get_constraints` MCP tool to see `should`/`info` for a path. It fires
for `Edit`, `Write`, and `MultiEdit` alike (all carry the target at `tool_input.file_path`).

## Wire the event hooks

In Claude Code settings (`~/.claude/settings.json`), point each event at the script with your
env (`DATABASE_URL`, `EMBED_BACKEND`, and `HM_LLM*` for the schedule jobs). The paths below are
the installed release (`./hm install`, [docs/INSTALL.md](../docs/INSTALL.md#installing-a-release)),
not a checkout: `./hm install --wire` rewrites hook commands that still name one. Example:

```json
{
  "hooks": {
    "PreToolUse":      [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": "python3 /home/you/.HyperMnesia/current/hooks/arch_invariants.py"}]}],
    "SessionStart":    [{"hooks": [{"type": "command", "command": "python3 /home/you/.HyperMnesia/current/hooks/mem_profile.py"}]}],
    "UserPromptSubmit":[{"hooks": [{"type": "command", "command": "python3 /home/you/.HyperMnesia/current/hooks/mem_recall.py"}]}],
    "SessionEnd":      [{"hooks": [{"type": "command", "command": "python3 /home/you/.HyperMnesia/current/hooks/mem_capture.py"}]}],
    "PreCompact":      [{"hooks": [{"type": "command", "command": "python3 /home/you/.HyperMnesia/current/hooks/mem_capture.py"}]}]
  }
}
```

Set `HM_REPO` in the hook env (or rely on the repo root's folder name) so the constraint hook
scopes to the right repo, matching the MCP server.

## Other clients (e.g. Codex CLI)

The hooks aren't Claude-specific — they read a JSON event on stdin and print injected context to
stdout. **Any agent that speaks the same hook contract can use them.** [Codex
CLI](https://github.com/openai/codex), for instance, uses the same event model (a `hooks.json`
with `SessionStart` / `UserPromptSubmit` / `PreToolUse` / `Stop`), so the memory-injection hooks
drop straight into `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "SessionStart":    [{"hooks": [{"type": "command", "command": "python3 /home/you/.HyperMnesia/current/hooks/mem_profile.py"}]}],
    "UserPromptSubmit":[{"hooks": [{"type": "command", "command": "python3 /home/you/.HyperMnesia/current/hooks/mem_recall.py"}]}]
  }
}
```

Two caveats when porting to a non-Claude client:

- **`mem_capture`** enqueues the client's *transcript path*; the extractor then parses that
  transcript. If the client stores sessions in a different format/location, capture needs a small
  adapter for that format — the injection hooks above don't.
- **`arch_invariants`** matches the `Edit|Write|MultiEdit` tool names. A client whose edit tool is
  named differently (Codex uses `apply_patch`/`shell`) needs its own matcher; the resolver itself
  is client-agnostic.

So profile + recall are portable as-is; capture and the constraint hook need a per-client touch.

For agent-directed memory search, graph navigation, bounded context, grounded Q&A, and project
document search, also register the MCP server: [Codex configuration](../docs/INSTALL.md#codex).
Hook injection and MCP discovery are separate connections; verify both with a real prompt and
tool call.

For OpenCode, use the [V2 MCP configuration](../docs/INSTALL.md#opencode-v2).
The profile/recall JSON above does not wire OpenCode events; automatic injection
and transcript capture require a separate V2 adapter. MCP search works independently.

The hooks read `DATABASE_URL` / `EMBED_BACKEND` from their environment (Claude Code passes the
shell env through). Keep those exported, or set them in the hook command.

The pipeline's tunables — thresholds, model names, batch sizes — come from one shared file
instead, `~/.HyperMnesia/hypermnesia.env`, which `_mem_common.py` loads on import, before any module
computes its constants. A variable already in the environment beats it, so a one-off run with a
different threshold needs no edit. That matters most for the scheduled passes below: launchd and
cron give them a minimal environment with none of your shell's exports, so for them the file is
the only place a setting can come from. `hypermnesia-settings` (in `console/`) reads and writes
it; [docs/INSTALL.md](../docs/INSTALL.md#the-shared-settings-file) lists the names it accepts and
the permissions it demands.

## Schedule the background jobs

`mem_extract.py`, `mem_consolidate.py`, `mem_reflect.py`, and `mem_dream.py` are NOT hooks — run
them out of band so they never block a session. The Dreamer has a PID lock and records each run;
it still cannot write a derived memory until `mem_review.py approve` revalidates the proposal.
They need `HM_LLM*` (see `_llm.py`: OpenAI-compatible endpoint, Ollama, or a CLI). The
[LLM backend guide](../docs/LLM_BACKENDS.md) includes vLLM, model sizing, security boundaries and
Kubernetes placement.

cron:
```cron
0 */4 * * *  cd ~/.HyperMnesia/current && DATABASE_URL=... HM_LLM_BACKEND=ollama HM_LLM_MODEL=qwen2.5:7b python3 hooks/mem_extract.py
30 5 * * *   cd ~/.HyperMnesia/current && DATABASE_URL=... HM_LLM_BACKEND=ollama HM_LLM_MODEL=qwen2.5:7b python3 hooks/mem_consolidate.py
50 5 * * *   cd ~/.HyperMnesia/current && DATABASE_URL=... HM_LLM_BACKEND=ollama HM_LLM_MODEL=qwen2.5:7b python3 hooks/mem_reflect.py
10 6 * * *   cd ~/.HyperMnesia/current && DATABASE_URL=... HM_LLM_BACKEND=ollama HM_LLM_MODEL=qwen2.5:7b python3 hooks/mem_dream.py
```

systemd timer, launchd agent, or any scheduler works equally — they just invoke the scripts.
Point it at `~/.HyperMnesia/current`, never at a checkout: a scheduled job runs whatever the tree
it names holds at that moment.

The jobs keep their state -- the transcript queue (`mem-queue.jsonl`) and its done list, the
profile cache, the fault markers and the locks -- in `~/.HyperMnesia/state/` once `./hm install`
has created it, and in `~/.claude/` until then (`state_path()` in `_mem_common.py`). Never in
both: the installer moves everything and creates `state/` last, and no hook moves anything
itself. A queue a hook of the old layout still appends to in `~/.claude` after the switch is
read by the extractor as a second source, and removed once it has been quiet for a day and
everything in it is done.
On macOS, `hypermnesia-jobs` (in `console/`) lists whatever launchd holds, runs one now, and
changes a schedule without hand-editing a plist.

## Review queue

Low-confidence consolidation proposals wait for you:

```bash
python3 hooks/mem_review.py list
python3 hooks/mem_review.py approve <id>
python3 hooks/mem_review.py reject  <id>
```
