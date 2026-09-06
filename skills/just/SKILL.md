---
name: just
description: "Just answer" mode. Use when a message starts with /just, or the user says "just tell me", "don't do it, tell me", "answer only", "audit mode". Answer literally and only the question asked; no edits, no side-effecting commands, no pushes, no "while I was at it", no next-step offers.
---

# /just — answer, don't act

Agents are trained to be autonomous: ask "could we…" and they go and do it. This skill flips the
mode for one request: **answer literally what was asked, then stop.**

The rule is small, local and **checkable**: the tool log for this turn must contain no writing call.

## Contract

1. **Read, never change.** Allowed: Read, Grep, Glob, safe `git log/diff/show/status`, `ls`,
   `cat`, SELECT queries, web fetch/search. Forbidden: Edit, Write, any command that changes
   files, the database, the cluster, configs or git history; `git push` above all. If answering
   seems to require a change, that is an action, not an answer: **say what would have to be
   done, and don't do it.**
2. **Only the question asked.** "Could we add seven red lines?" → "Yes/no, because…", not the
   lines. No widening: no "also noticed", no "you should also", no list of next steps.
3. **Literally.** Don't infer an unstated intent. If the question is ambiguous enough that the
   answers diverge, ask one clarifying question and stop.
4. **Honest about uncertainty.** If it can't be verified without acting, say so: "not checked,
   because checking requires X."
5. **Short ending.** The answer ends with the answer. No "want me to do it?".

## Audit mode

An audit is valuable precisely because it diagnoses without intervening: look, assess, name —
and don't "fix it on the way", because the fix changes what hasn't been measured yet. `/just`
gives this per request; for a whole session the user says "audit mode", and the contract holds
until lifted ("audit mode off", "you can act now").

## Examples

- `/just will recall break if I delete rerank/.venv?` → "No: recall talks to the cluster; the
  venv is only used by the reranker service. The service itself — yes, it will fail." (no commands)
- `/just how many map orphans in myrepo?` → read the flag / run the read-only freshness check,
  report the number. Don't fix.
- `/just fix the glob` → "That's an action, not a question. Not doing it in /just mode; lift the
  mode and I will." (don't fix)

## Checking the contract

After a `/just` answer, the tool log for that turn must contain no Edit/Write, and among Bash
calls nothing but reads. If it does, the contract was broken — that is a bug of the mode, not
"helpful initiative".
