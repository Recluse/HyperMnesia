#!/usr/bin/env python3
"""Lexical-query hygiene: what must be stripped, and what must survive whole.

Three defects have passed through these two functions, and they pull in opposite directions:
  * a raw query let a leading '-' become a NEGATED lexeme, so the OR'd lexical leg matched every
    document lacking that word;
  * the `simple` config keeps stopwords, so one shared stopword OR-matched essentially every
    chunk and filled the RRF pool with noise;
  * the fix for those split on every non-word character -- which also split the tokens Postgres
    stores WHOLE. `to_tsvector('simple','example.com')` is the single lexeme 'example.com', so
    querying `example | com` matches nothing, and exact search for hostnames, URLs, paths and
    dotted filenames silently stopped working.

No database needed: these are pure string functions, and the lexeme claim above is what
`to_tsvector` does (verified separately against a live store).

    python3 tests/test_query_hygiene.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "ingest", "search.py"), encoding="utf-8").read()
ns = {"re": re}
exec(compile(src[src.index("_STOP = set("):src.index("def search(")], "search.py", "exec"), ns)
_no_neg, _lex_query = ns["_no_neg"], ns["_lex_query"]

failures, ran = [], 0


def check(name, cond):
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


print("compound tokens survive whole (Postgres stores them as one lexeme):")
for q in ("example.com", "docs/operations/deploy-rules.md", "search.py", "v1.2.3",
          "ci.internal.example.org"):
    check(f"{q!r} is not split", _lex_query(q) == q and _no_neg(q) == q)
check("a compound is kept even though it is short",
      _lex_query("a.b") == "a.b")
check("a compound is kept even though a part is a stopword",
      _lex_query("docs/plan/on.md") == "docs/plan/on.md")
check("a compound inside a sentence keeps both it and its neighbours",
      _lex_query("смотри example.com сейчас") == "смотри example.com сейчас")
check("a stopword next to a compound is still dropped",
      _lex_query("на example.com") == "example.com")

print("the two original defects stay fixed:")
check("a leading dash cannot become a negated lexeme", "-" not in _no_neg("-secret деплой"))
check("stopwords are dropped from the simple leg", _lex_query("the on of a") == "zzz-no-lexemes-zzz")
check("short plain tokens are dropped", _lex_query("as is my ok") == "zzz-no-lexemes-zzz")
check("an all-stopword query cannot match everything",
      _lex_query("и в на с") == "zzz-no-lexemes-zzz")
check("ordinary words are kept", _lex_query("деплоится воркер") == "деплоится воркер")

print(f"\n{ran - len(failures)}/{ran} checks passed"
      + (f"; FAILED: {', '.join(failures)}" if failures else ""))
sys.exit(1 if failures else 0)
