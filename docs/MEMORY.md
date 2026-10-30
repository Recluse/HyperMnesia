# Personal memory

The `mem.*` schema stores durable, non-derivable facts about you and your work — preferences,
decisions and their reasons, standing constraints, open intentions — so the agent stops
re-learning them every session. Free text is the primary representation; structure is optional.

"Personal" is about the kind of fact, not the number of people: more than one author can share
one store, and what each may read is part of the model rather than a deployment detail.

## Model

- **Types:** `preference | semantic | episodic | prospective | procedural | summary`.
- **Two time axes, kept apart.** `valid_from`/`valid_to` are *event time* — when the fact holds
  in the world. `created_at` and `retired_at` are *belief time* — when the store learned the row
  and when it stopped believing it (merge, `mark`, supersede set `retired_at`; `mark
  status=active` clears it, and records the interval it closes in
  `metadata.retired_intervals`, so history before the reactivation stays answerable). Retiring a row never touches its event window: merging two
  duplicates does not claim the fact stopped holding today, and correcting a mistake does not
  claim the mistake was true until today. `valid_to` changes only when a caller states event
  time. A write whose `valid_to` precedes its `valid_from` is refused (in code and by a
  constraint). Retrieval reads `mem.active_memories`, which hides anything retired, outside its
  validity window, **or belonging to another author** (see "Author and audience" below).
- **Supersede, don't overwrite.** A correction writes a new memory with `supersedes_id` pointing
  at the old one and retires the old one. With an explicit `valid_from` the old row's event
  window is closed at that instant; a `valid_from` in the future is refused (nothing would be
  active until then — write the new fact without `supersedes_id` and give the old one a
  `valid_to` with `mark` instead), and so is one earlier than the old row's own `valid_from`.
  History is never destroyed, and `search` with `as_of` answers "what did I believe then":
  rows created at or before that instant, not retired by it and not inside an earlier
  retirement that was later reversed, whatever their status now. The upgrade migration
  backfills `retired_at` for rows retired before it existed — from a row written to replace
  it (its `supersedes_id`), else from the `valid_to` the old code wrote at retirement, else
  from a newer row named in `superseded_by`, else from the row's last update — never earlier
  than the row's own `created_at`. Malformed stored values are skipped, never fatal.
- **Importance / confidence**, not decay. Volume is controlled by an importance score at write
  time and by consolidation, not by a numeric forgetting curve (which throws away correct facts
  to save space).
- **Abstention.** Semantic recall has a distance floor: an irrelevant query returns *nothing*,
  not the top-k least-bad rows. Injecting noise is worse than injecting nothing. This covers
  search, and therefore the query-relevant part of `memory_context`; the profile rows that
  `memory_context` adds by design are not filtered by the query (see `memory_ask` below for how
  abstention is decided there).
- **Provenance.** Every memory records where it came from (`user_message` vs `assistant_inference`
  vs `consolidation`) in `mem.sources`.
- **Perspective.** A memory can name an `observer` and an `observed` entity. `subject` remains a
  compatibility alias for `observed`. Entities are author-owned, so one person's alias or
  mutable attributes cannot silently rewrite another person's identity graph.
- **Evidence.** `mem.derivations` records directed support edges such as `supports`,
  `inferred_from`, `summarizes`, `supersedes`, and `contradicts`. A database trigger rejects
  cycles across the full graph while RLS requires both endpoints to be visible to the reader.
- **Derived rows do not outlive their premises.** A deductive or inductive conclusion is served
  — in search results (marked `derived:<level>`), the `derived` section of `memory_context`
  and `memory_ask` citations — only while every `inferred_from`/`supports` premise, and every
  premise id stamped on it at approval (backfilled from its edges for conclusions approved
  before the upgrade), is active and readable by the caller (`mem.premises_live`). The pinned
  session profile holds explicit rows only: it is cached for minutes, too long to vouch for a
  premise, so conclusions reach the agent only through the paths that check it per call. Its
  cache file carries a version line, and a cache written by an older version is ignored. This is decided at query time, not by cascading status changes:
  a conclusion whose premise was superseded, retracted, merged away, deleted or made
  unreadable to you is left out and named in `scope.stale_premises` of `memory_context`, and it
  becomes servable again if the premise comes back. History reads (`include_inactive`,
  `as_of`) still show it, with its level.
- **Author and audience.** Every row records `author` (who learned it) and `scope`
  (`private` | `project`); `mem.project_members` says which projects an author may read the
  shared memory of. `preference` defaults to private, anything else with a project tag is
  shared with that project, and a row with no project stays private whatever its type.
  Identity comes from `MEM_AUTHOR` and reaches Postgres as the connection option `mem.reader`.
  **Writes are refused without it**, on a one-person store too: a memory with no author cannot
  be scoped, shared or revoked. See "Two people, one store" in `INSTALL.md` for the rule, the
  role and the database-level enforcement.

## Retrieval

`mem_ops.py search` runs the same hybrid RRF as doc search (bge-m3 + composite FTS) over
`active_memories`, with a distance floor on the dense leg and the SAME floor applied to
lexical-only hits — the lexical leg exists to rescue near-misses, not to admit a memory that
shares one incidental token with the query. Importance and recency are true tiebreakers: their
combined weight is below the smallest difference between two reachable RRF scores at K=60 with
30 ranks per leg (about 2.2e-8, computed in `mem_ops.py`), so they order exact ties and can
never swap two differently ranked hits. `write` / `supersede` / `get` / `mark` round out the
CRUD; all take JSON on stdin, and all (plus `search`) answer in JSON when the payload says
`"format": "json"` — the contract scripts must use instead of parsing the human-readable lines.
`search` also takes `as_of` (a timestamp; see "Two time axes") and `track_access: false` for a
caller that looks without recalling (the extractor's novelty gate), so probing does not hide
stale facts from `mem_review.py stale`.

`memory_context` composes prompt-ready memory under a strict token budget. Its `depth` can be
`minimal`, `low`, `medium`, `high`, `max`, or `auto`. Fixed levels progressively add derived
conclusions, direct premises, and bounded premise ancestry. `auto` uses retrieval signals and
returns the selected level and reason; it does not ask a model to choose. Routing is either the
server's project or every RLS-visible workspace. Global routing reports ranked
observer-to-observed routes backed by materialized peer cards.

**Reading cheaply.** Every body an agent is shown costs tokens, and most search hits are not
worth reading in full. `memory_search` with `brief: true` returns one line per hit -- id, type,
status when not active, project, date, an estimate of the tokens the whole row costs, and its
first hundred characters -- followed by the ids to fetch. `memory_get` takes `ids` (up to 20)
as well as `id`, so the hits worth reading come back in one call; as JSON a batch is a list, with
`null` for a row the reader may not see, the same answer as a missing id. `memory_timeline` shows
the same author's memories just before and after one row in the same project, oldest first, in
the same compact lines with that row marked: the neighbours of a hit are often what explains it.
Knowledge pages stay out of it, history only on request, and a row the reader may not see is
"(not found)", so its existence is not disclosed.

`memory_ask` is the synchronous Dialectic. It gives a model only the bounded context, then checks
the reply as data: each atomic claim needs one or more IDs from that exact context, and every
cited row must still be active. One unsupported claim rejects the whole answer. Returned prose
and citations are built by code; a failed check becomes an explicit abstention.

Two words there mean less than they might suggest, so precisely:

- **Grounded** means every cited ID exists in the context that was given to the model and is
  active there (a derived conclusion additionally still has all its premises — the context does
  not serve one that does not). It does **not** mean the claim follows from the cited text;
  nothing checks entailment.
- **Abstention** is decided in code, before any model call, when nothing in the context matched
  the query through search (no item carries `query_match`). The context still contains profile
  rows chosen without the query — by design, so a hit can be read correctly — but those alone
  are never treated as an answer. When the query did match something, the model may still
  abstain, and an answer that fails the grounding check becomes an abstention.

`memory_ask` uses the identity the MCP server resolved and passed to it, rather than resolving
its own a second time.

The read-only graph tools are useful without a model:

- `memory_perspectives` lists visible observer-to-observed edges;
- `memory_evidence` walks incoming or outgoing provenance edges;
- `memory_peer_card` returns a materialized navigation summary for one peer;
- `memory_context` returns selected sections, IDs, token estimate, routes, and depth reason.

## Dreamer: derived memory behind review

`hooks/mem_dream.py` groups explicit memories by perspective and asks the configured LLM for
new deductive or inductive conclusions. Candidates must cite only IDs from the bounded input;
inductive conclusions need at least two independent premises. The process records each run, but
it cannot create a memory. Every candidate enters `mem.review_queue` with action `derive`, and
approval rechecks ownership, visibility, perspective, active premises, duplicates, and scope in
one transaction before writing the conclusion and its evidence edges. Approval also stamps the
premise ids on the conclusion; from then on whether it may be served is decided at query time
(see "Derived rows do not outlive their premises"). Dreamer conclusions are never consolidated:
they are excluded from grouping and refused by the merge path.

The Dreamer and Dialectic use the same pluggable backend as extraction and consolidation
(`HM_LLM_BACKEND`, `HM_LLM_URL`, `HM_LLM_MODEL`, or `HM_LLM_CMD`). Their prompts are English;
the Dialectic asks the model to answer in the query's language.

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
python hooks/mem_review.py stale [days]   # active facts nothing has recalled in that long
```

So a wrong merge can never silently drop a memory.

**One merge path.** A confident verdict is not applied by the consolidator itself: it goes to
`mem_ops.py apply_verdict`, which runs the same `apply_proposal` that review approval runs, in
one transaction. Either way the merged row takes its scope and project from the group — private
if any member was private, never from the model's output — and records `merged_from`,
`merged_created_range` (the members' first and last `created_at`; the merged row itself is a
new belief with `created_at = now()`), `summarizes` edges to every member, and the members'
shared perspective. A group spanning two authors, or containing a derived conclusion or a
page, is refused. Every member — a supersede's winner included — is locked and rechecked
(`FOR NO KEY UPDATE`, which does not block foreign-key checks from concurrent evidence writes)
before anything changes, so two concurrent verdicts on the same group cannot both land, and
every member must actually be retired (still active, still the applier's) or the whole verdict
is rolled back and nothing changes. Groups are built from the
running author's own explicit rows only. Every scheduled pass — extract, consolidate, reflect,
dream — refuses to start without a valid `MEM_AUTHOR`.

## Reflect: per-project knowledge pages

A scheduled reflect pass (`hooks/mem_reflect.py`) synthesizes each project's active
**project-scoped** memories into one coherent **knowledge page** (`metadata.kind='page'`), so
recall can surface a single overview
instead of N scattered fragments. Private rows are deliberately excluded: the page is written back
project-scoped and read by everyone on the project, so a page summarising private memories would
publish them in paraphrase. **Anti-staleness by construction:** every run *rebuilds* the page
from the project's current shared memories and supersedes the author's prior page (`page_upsert`) — a page is
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

## Growth over time: the trade-offs, stated

The store has no decay, by choice; correctness comes from validity windows, supersede and
consolidation. What that costs over a year of use:

- **Nothing fades.** A fact nobody contradicts stays active until someone retires it.
  `mem_review.py stale` lists old facts nothing has recalled in a long time
  (`MEM_STALE_DAYS`, default 180) so a person can confirm or retire them.
- **Consolidation drains slowly.** At most `MEM_CONSOLIDATE_MAX_GROUPS` (default 10) groups are
  examined per run, each an LLM call.
- **Importance is capped for automatic writers.** The extractor, consolidator and Dreamer clamp
  importance to 0.7; a manual `memory_write` may set up to 1.0. The pinned profile orders by
  importance, then recency — so manually written facts stay pinned above automatic ones
  indefinitely, and automatic ones compete on recency among themselves.
- **Merged rows are new.** A merge gets `created_at = now()` and therefore rises in
  recency-ordered views; when its content was originally learned is in
  `metadata.merged_created_range`. The profile ordering itself is unchanged.
- **Reflect reads a bounded, deterministic slice.** It takes the first `MEM_REFLECT_MAX`
  (default 80) project-scoped rows by importance, then newest, then id, so with many rows at
  the 0.7 clamp the page covers the newest of them, the same ones on every run.
- **Pages are per author.** A project with N authors has N active knowledge pages.

## Capture (Claude Code hooks)

Optional, and the only Claude-Code-specific part. Register in Claude Code settings:

- **SessionStart** -> `mem_profile.py` injects the pinned profile (top preferences/facts/plans).
- **UserPromptSubmit** -> `mem_recall.py` injects memories relevant to the prompt.
- **SessionEnd / PreCompact** -> `mem_capture.py` enqueues the transcript path.
- On a schedule (cron/systemd/launchd): `mem_extract.py` distills queued transcripts into
  memories via an LLM; `mem_consolidate.py` runs the consolidation pass.

The extractor removes every memory block this system injected from the transcript in code
before the model sees it, so that memory — and the agent's paraphrase sitting inside it — is not
learned a second time; the prompt says the same. It matches exactly what the hooks emit: an
opening `<personal-memory-<kind> nonce=<hex>>` and the closing tag of the same kind and nonce,
paired innermost-first in one linear pass, so a nested block goes as a whole (even one that
reuses a nonce); an opening tag whose close is missing (a turn cut
mid-block) is removed to the end of that turn. A tag written without a nonce — someone talking
about the tag — is not injected output and is left as it is. Its novelty gate probes with
`track_access: false`, and a probe that fails is a failure, not "not a duplicate": the item is
skipped and the transcript stays queued. `mem_extract.py` exits 1 when anything failed (a write,
a duplicate check, an extraction) and 2 without a valid `MEM_AUTHOR`, so a scheduler sees a
stalled pipeline instead of a green run.

**What consolidation actually acts on.** A candidate group is a set of memories where *every*
member is within `MEM_CONSOLIDATE_MAXDIST` of *every* other -- a maximal clique, not a chain.
The distinction is the whole safety of the pass: linking any two close memories and letting that
spread transitively collapsed a 528-memory store into one group of 369, and a single `merge`
verdict on such a group replaces every member with one text. Three bounds hold independently of
what the model says: no group above `MEM_CONSOLIDATE_MAX_GROUP` is ever acted on, at most
`MEM_CONSOLIDATE_MAX_GROUPS` are examined per run (each is an LLM call), and a merge below
`MEM_REVIEW_THRESHOLD` confidence is parked for you in the review queue instead of applied. The
replacement inherits the scope and project of its sources (see "One merge path" above), and
each retired memory records which one displaced it.

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

**Keeping a span out.** `<private>...</private>` in a prompt or a message keeps that span out of
everything: the extractor drops it from each turn before the model sees the transcript (and once
more right before the model call), and recall drops it from the prompt before the prompt becomes
a search query. The tag is case-insensitive and may span lines; an opening tag without its close
hides the rest of that turn.

**The LLM step is pluggable** (see `hooks/_llm.py`): set `HM_LLM_BACKEND` to `openai`
(`HM_LLM_URL`/`HM_LLM_KEY`/`HM_LLM_MODEL`), `ollama` (`HM_LLM_MODEL`), or `cli` (`HM_LLM_CMD`).
Auto-selected if unset: openai when `HM_LLM_URL` is set, else cli when `HM_LLM_CMD` is set, else ollama. It only needs to turn text into a small JSON array of memory items; nothing about the
store depends on which model you use. If you don't want auto-capture, skip the hooks entirely and
write memories yourself via the `memory_write` MCP tool or `mem_ops.py write` — both still need
`MEM_AUTHOR` set, since every write records its author.
