# Hooks (Claude Code)

Optional. These wire auto-injection of HyperMnesia into Claude Code — the architectural map
(constraints) *and* personal memory. The store works without them (query via the MCP tools or
`ingest/mem_ops.py`); the hooks just make delivery hands-free so the agent doesn't have to think
to ask. All are **fail-open** — any error and they output nothing, never blocking the tool.

| Hook | Claude Code event | What it does |
|------|-------------------|--------------|
| `arch_invariants.py` | PreToolUse (`Edit\|Write\|MultiEdit`) | inject the `must` constraints for the file being edited (Tier 0/1) |
| `mem_profile.py` | SessionStart (`startup\|resume\|clear\|compact`) | inject the pinned profile |
| `mem_recall.py`  | UserPromptSubmit | inject memories relevant to the prompt |
| `mem_capture.py` | SessionEnd, PreCompact | enqueue the transcript path |
| `mem_extract.py` | schedule (cron/systemd/launchd) | distill queued transcripts -> memories |
| `mem_consolidate.py` | schedule (daily) | merge/supersede near-duplicates (gated) |
| `mem_reflect.py` | schedule (daily) | synthesize per-project knowledge pages (regenerated, never stale) |
| `mem_review.py`  | you, manually | `list \| approve <id> \| reject <id>` the review queue |

`arch_invariants.py` is what makes constraint delivery *deterministic and unprompted*: before an
edit, it resolves the file path to its component (Tier 1) and injects the applicable `must`
invariants as `additionalContext` — the architectural-memory counterpart to `mem_recall`. It reads
`DATABASE_URL` and `HM_REPO` (or the cwd basename), and shells `psql`; if `psql` or the DB is
absent it silently exits 0. It only ever `allow`s the edit — it surfaces rules, it never blocks.

It injects only `must`, on purpose — `should` constraints are kept out of the every-edit context
for token budget; call the `get_constraints` MCP tool to see `should`/`info` for a path. It fires
for `Edit`, `Write`, and `MultiEdit` alike (all carry the target at `tool_input.file_path`).

## Wire the event hooks

In Claude Code settings (`~/.claude/settings.json`), point each event at the script with your
env (`DATABASE_URL`, `EMBED_BACKEND`, and `HM_LLM*` for the schedule jobs). Example:

```json
{
  "hooks": {
    "PreToolUse":      [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/arch_invariants.py"}]}],
    "SessionStart":    [{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_profile.py"}]}],
    "UserPromptSubmit":[{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_recall.py"}]}],
    "SessionEnd":      [{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_capture.py"}]}],
    "PreCompact":      [{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_capture.py"}]}]
  }
}
```

Set `HM_REPO` in the hook env (or rely on the cwd basename) so the constraint hook scopes to the
right repo, matching the MCP server.

The hooks read `DATABASE_URL` / `EMBED_BACKEND` from their environment (Claude Code passes the
shell env through). Keep those exported, or set them in the hook command.

## Schedule the background jobs

`mem_extract.py` and `mem_consolidate.py` are NOT hooks — run them out of band so they never
block a session. They need `HM_LLM*` (see `_llm.py`: OpenAI-compatible endpoint, Ollama, or a CLI).

cron:
```cron
0 */4 * * *  cd /opt/hypermnesia && DATABASE_URL=... HM_LLM_BACKEND=ollama HM_LLM_MODEL=qwen2.5:7b python3 hooks/mem_extract.py
30 5 * * *   cd /opt/hypermnesia && DATABASE_URL=... HM_LLM_BACKEND=ollama HM_LLM_MODEL=qwen2.5:7b python3 hooks/mem_consolidate.py
50 5 * * *   cd /opt/hypermnesia && DATABASE_URL=... HM_LLM_BACKEND=ollama HM_LLM_MODEL=qwen2.5:7b python3 hooks/mem_reflect.py
```

systemd timer, launchd agent, or any scheduler works equally — they just invoke the two scripts.

## Review queue

Low-confidence consolidation proposals wait for you:

```bash
python3 hooks/mem_review.py list
python3 hooks/mem_review.py approve <id>
python3 hooks/mem_review.py reject  <id>
```
