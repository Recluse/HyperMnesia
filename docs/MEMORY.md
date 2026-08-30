# Personal memory

The `mem.*` schema stores durable, non-derivable facts about you and your work — preferences,
decisions and their reasons, standing constraints, open intentions — so the agent stops
re-learning them every session. Free text is the primary representation; structure is optional.

## Model

- **Types:** `preference | semantic | episodic | prospective | procedural | summary`.
- **Bi-temporal.** `valid_from`/`valid_to` are *event time* (when the fact holds in the world);
  `created_at` is *ingestion time* (when we learned it). Retrieval reads `mem.active_memories`,
  which hides anything superseded or outside its validity window.
- **Supersede, don't overwrite.** A correction writes a new memory with `supersedes_id` pointing
  at the old one and closes the old one's validity window. History is never destroyed, so "what
  did I believe last month, and why did it change?" is answerable.
- **Importance / confidence**, not decay. Volume is controlled by an importance score at write
  time and by consolidation, not by a numeric forgetting curve (which throws away correct facts
  to save space).
- **Abstention.** Semantic recall has a distance floor: an irrelevant query returns *nothing*,
  not the top-k least-bad rows. Injecting noise is worse than injecting nothing.
- **Provenance.** Every memory records where it came from (`user_message` vs `assistant_inference`
  vs `consolidation`) in `mem.sources`.

## Retrieval

`mem_ops.py search` runs the same hybrid RRF as doc search (bge-m3 + composite FTS) over
`active_memories`, with importance and recency as tiebreakers and the abstention floor on the
dense leg. `write` / `supersede` / `get` / `mark` round out the CRUD; all take JSON on stdin.

## Consolidation and the review queue

A scheduled pass finds near-duplicate active memories (pairwise cosine below a threshold) and
asks an LLM per group to `keep` | `merge` (one canonical text replacing the group) | `supersede`
(one member is current, the rest outdated). The key safety rule: **detection is automatic,
mutation is gated.** The LLM returns a confidence; at or above `MEM_REVIEW_THRESHOLD` (default
0.8) the change is applied, below it the proposal is parked in `mem.review_queue` for you:

```bash
python hooks/mem_review.py list
python hooks/mem_review.py approve <id>   # applies the proposed merge/supersede
python hooks/mem_review.py reject  <id>
```

So a wrong merge can never silently drop a memory.

## Reflect: per-project knowledge pages

A scheduled reflect pass (`hooks/mem_reflect.py`) synthesizes each project's active memories into
one coherent **knowledge page** (`metadata.kind='page'`), so recall can surface a single overview
instead of N scattered fragments. **Anti-staleness by construction:** every run *rebuilds* the page
from the project's current active memories and supersedes the prior page (`page_upsert`) — a page is
never edited in place and can't drift from its sources; if the memories change, the next run
regenerates it. Only projects with at least `MEM_REFLECT_MIN` (default 5) active memories get one.

Pages are deliberately excluded from two places so they don't corrupt the store they summarize:
the **novelty gate** (`nearest` skips pages — otherwise a page, sitting close to each source, would
suppress capture of the very memories it's built from) and the **consolidator** (never merges a
page with its sources). Run it out of band like consolidation:

```bash
python hooks/mem_reflect.py --dry-run            # synthesize + print, no write
python hooks/mem_reflect.py                       # write/refresh all project pages
python hooks/mem_reflect.py --project myrepo      # just one
```

## Capture (Claude Code hooks)

Optional, and the only Claude-Code-specific part. Register in Claude Code settings:

- **SessionStart** -> `mem_profile.py` injects the pinned profile (top preferences/facts/plans).
- **UserPromptSubmit** -> `mem_recall.py` injects memories relevant to the prompt.
- **SessionEnd / PreCompact** -> `mem_capture.py` enqueues the transcript path.
- On a schedule (cron/systemd/launchd): `mem_extract.py` distills queued transcripts into
  memories via an LLM; `mem_consolidate.py` runs the consolidation pass.

Injected memory is wrapped in a nonce-fenced block marked as *data, not instructions*, and the
content is defanged, so a poisoned memory can't smuggle directives into the agent.

**Secret redaction on capture.** Memory is distilled from raw coding-session transcripts, which
routinely hold API keys, tokens and connection strings. Every write path — hook extract, the
`memory_write` MCP tool, supersede, and consolidation merges — funnels through `do_write`, which
runs `ingest/_redact.py` over the content, title, and source excerpt *before* they are stored or
embedded. Structured secrets (OpenAI/Anthropic/GitHub/GitLab/AWS/Google/Slack keys, JWTs, private
keys, `name=value` credentials, and passwords inside connection URLs) become `[REDACTED:<kind>]` —
the fact that a secret existed survives, the secret does not. Best-effort (known shapes, not a
guarantee); ordinary prose like "the password reset flow" is left untouched.

**The LLM step is pluggable** (see `hooks/_llm.py`): set `HM_LLM_BACKEND` to `openai`
(`HM_LLM_URL`/`HM_LLM_KEY`/`HM_LLM_MODEL`), `ollama` (`HM_LLM_MODEL`), or `cli` (`HM_LLM_CMD`).
Auto-selected if unset: openai when `HM_LLM_URL` is set, else cli when `HM_LLM_CMD` is set, else ollama. It only needs to turn text into a small JSON array of memory items; nothing about the
store depends on which model you use. If you don't want auto-capture, skip the hooks entirely and
write memories yourself via the `memory_write` MCP tool or `mem_ops.py write`.
