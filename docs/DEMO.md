# Two minutes, and the point is visible

Everything below is real output, captured from the code in this repo against
`examples/seed_example.sql`. Nothing here is illustrative.

You need a store with the schema loaded (`./hm init`) and `DATABASE_URL` exported.

## 1. Load the example map (10 seconds)

```bash
psql "$DATABASE_URL" -f examples/seed_example.sql
```

Four components for a fictional `myapp`, one dependency edge (`api depends_on db`), and two
rules — one `must` that holds everywhere, one `should` that applies to the API. That is the whole
fixture. Note what it is *not*: a description of the repo. It is the set of statements that change
what someone may write.

## 2. Ask what applies to a file

```bash
export HM_REPO=myapp
```

Through the MCP tool, which is what an agent calls when it thinks to ask. Build the server once,
staying at the repo root (every path below is relative to it), then call the tool directly — one
JSON-RPC request per line, no `initialize` handshake needed for a one-shot:

```bash
cargo build --release --manifest-path mcp-server/Cargo.toml
```

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_constraints","arguments":{"paths":["src/api/users.py"]}}}' \
  | mcp-server/target/release/hypermnesia-mcp
```

The reply is one JSON-RPC line. The `result.content[0].text` inside it — the part an MCP client
hands to the model — is:

```
src/api/users.py -> myapp-api
(1-hop graph also pulled: myapp-db)

Applicable constraints:
  [must] All DB access goes through the data layer (global)
      No component other than myapp-db may import the Postgres driver or run raw SQL.
  [should] Handlers return the response envelope (myapp-api)
      API handlers must return the shared {data, error} envelope, never a bare value.
```

Two things are happening. The path resolved to a component by glob, not by a search and not by
the model's guess. And the dependency edge pulled the data layer in as well, so a change to the
API sees the rules of what it depends on.

A third thing is *supposed* to happen and does not here. Each constraint line can end with
`[src: <scope>:<path>]` naming the document that states the rule, so when that document changes
you can find the rules it invalidated. The fixture sets `source_doc_id` by looking the document
up at `myapp:docs/architecture.md`, and this demo ingests no documents at all — the lookup
matches nothing, stores NULL, and the attribution is simply absent from every line above. Ingest
a document at that path under scope `myapp` and re-run the seed (it is idempotent) and the
`[src: ...]` suffix appears.

Three other lines can appear in that block, none of them with this fixture:

- `(!) FLAGGED (no component -- extend the map)` — a path that resolves to nothing.
- `(!) UNDELIVERABLE constraints in this repo` — a rule whose `scope` is not `component` at all,
  or whose `component_id` is NULL. The schema permits both, and such a rule is counted by every
  "how many active rules" query and returned by nothing. (A well-formed component-scoped rule
  whose component simply did not match this path is not undeliverable; it just does not apply
  here.)
- `(!) STALE MAP` — the server could not re-read the map and is answering from a cached copy,
  with the error and the copy's age.

The scope, however, is *not* announced by this tool: run it with `HM_REPO` unset or naming a
scope nothing was seeded under and every path comes back FLAGGED with `(none)` for constraints,
which looks exactly like an unmapped file. Keep the export above in the same shell.

## 3. Now stop asking

The step above still depends on the agent deciding to call a tool. The one below does not.

```bash
printf '{"hook_event_name":"PreToolUse","tool_name":"Edit","cwd":"/home/you/code/myapp",
        "tool_input":{"file_path":"/home/you/code/myapp/src/api/users.py"}}' \
  | python3 hooks/arch_invariants.py
```

It is a Claude Code hook, so what it prints is a JSON object, on one line:

```
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "HyperMnesia -- applicable architecture invariants for src/api/users.py:\n  - [global] All DB access goes through the data layer: No component other than myapp-db may import the Postgres driver or run raw SQL."}}
```

Registered as a `PreToolUse` hook with matcher `Edit|Write|MultiEdit`, that is what the client
reads: it takes `hookSpecificOutput.additionalContext` and puts it into the model's context
*before the edit runs*, on every edit, whether or not the agent thought to ask. So what the model
actually sees is the two lines inside that string:

```
HyperMnesia -- applicable architecture invariants for src/api/users.py:
  - [global] All DB access goes through the data layer: No component other than myapp-db may import the Postgres driver or run raw SQL.
```

The hook deliberately emits no `permissionDecision`, so it injects context and never approves an
edit. This is the whole product. Storage is solved; delivery is not.

Note what it did **not** inject: the `should`. Only `must` goes into every-edit context, because a
block that appears on every edit and mostly does not matter is a block people learn to skip. Ask
the tool when you want the rest.

## 4. Watch it tell you when the map is wrong

Edit a file no component claims:

```bash
printf '{"hook_event_name":"PreToolUse","tool_name":"Edit","cwd":"/home/you/code/myapp",
        "tool_input":{"file_path":"/home/you/code/myapp/src/lib/util.py"}}' \
  | python3 hooks/arch_invariants.py
```

Same JSON envelope; this time the `additionalContext` string carries a third line:

```
HyperMnesia -- applicable architecture invariants for src/lib/util.py:
  - [global] All DB access goes through the data layer: No component other than myapp-db may import the Postgres driver or run raw SQL.
  (note: src/lib/util.py maps to no component -- consider adding it to the map)
```

The global rule still applies, and the gap is stated rather than silently producing a thinner
answer. Point the same hook at a store that is down, or at an `HM_REPO` no component was ingested
under, and it says that too — once, not on every edit. A map that has quietly stopped resolving
looks exactly like a project with no rules, and that is the failure this project is built around.

## 5. Check the install rather than trusting it

```bash
./hm doctor
```

Reports the faults that leave a system which still answers: an ANN index that was never built,
chunks with no embedding, two embedding models in one table, a scope name off by case. None of
those raises an error anywhere.

## What you would do next

The map in step 1 is a fixture. Yours is the work, and it is the part that pays — see
`skills/onboard-project` for the six steps, of which five are plumbing and one decides whether any
of this is useful. The short version: write rules that **change what someone may write**, not
labels that describe what a directory is. "SQL only in the data layer" is a rule. "The API layer
handles HTTP" costs tokens on every edit and changes nothing.
