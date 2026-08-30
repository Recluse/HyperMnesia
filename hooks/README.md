# Personal-memory hooks (Claude Code)

Optional. These wire auto-capture and auto-injection of personal memory into Claude Code. The
store works without them (write memories via the `memory_write` MCP tool or `ingest/mem_ops.py`);
the hooks just make it hands-free. All are **fail-open** — any error and they output nothing.

| Hook | Claude Code event | What it does |
|------|-------------------|--------------|
| `mem_profile.py` | SessionStart (`startup\|resume\|clear\|compact`) | inject the pinned profile |
| `mem_recall.py`  | UserPromptSubmit | inject memories relevant to the prompt |
| `mem_capture.py` | SessionEnd, PreCompact | enqueue the transcript path |
| `mem_extract.py` | schedule (cron/systemd/launchd) | distill queued transcripts -> memories |
| `mem_consolidate.py` | schedule (daily) | merge/supersede near-duplicates (gated) |
| `mem_review.py`  | you, manually | `list \| approve <id> \| reject <id>` the review queue |

## Wire the event hooks

In Claude Code settings (`~/.claude/settings.json`), point each event at the script with your
env (`DATABASE_URL`, `EMBED_BACKEND`, and `HM_LLM*` for the schedule jobs). Example:

```json
{
  "hooks": {
    "SessionStart":    [{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_profile.py"}]}],
    "UserPromptSubmit":[{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_recall.py"}]}],
    "SessionEnd":      [{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_capture.py"}]}],
    "PreCompact":      [{"hooks": [{"type": "command", "command": "python3 /opt/hypermnesia/hooks/mem_capture.py"}]}]
  }
}
```

The hooks read `DATABASE_URL` / `EMBED_BACKEND` from their environment (Claude Code passes the
shell env through). Keep those exported, or set them in the hook command.

## Schedule the background jobs

`mem_extract.py` and `mem_consolidate.py` are NOT hooks — run them out of band so they never
block a session. They need `HM_LLM*` (see `_llm.py`: OpenAI-compatible endpoint, Ollama, or a CLI).

cron:
```cron
0 */4 * * *  cd /opt/hypermnesia && DATABASE_URL=... HM_LLM_BACKEND=ollama HM_LLM_MODEL=qwen2.5:7b python3 hooks/mem_extract.py
30 5 * * *   cd /opt/hypermnesia && DATABASE_URL=... HM_LLM_BACKEND=ollama HM_LLM_MODEL=qwen2.5:7b python3 hooks/mem_consolidate.py
```

systemd timer, launchd agent, or any scheduler works equally — they just invoke the two scripts.

## Review queue

Low-confidence consolidation proposals wait for you:

```bash
python3 hooks/mem_review.py list
python3 hooks/mem_review.py approve <id>
python3 hooks/mem_review.py reject  <id>
```
