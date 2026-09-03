#!/usr/bin/env python3
"""Detached memory extractor -- consumes the capture queue, distills durable
memories from session transcripts via a pluggable LLM (see _llm.py), writes them
to mem.* with provenance. Run manually or from cron/systemd/launchd; NOT from a blocking hook.

    python3 hooks/mem_extract.py [--dry-run] [--limit N]

Idempotent: processed transcript paths are recorded in ~/.claude/mem-queue.done;
a lock file serializes concurrent runs. A write-time novelty gate (is_duplicate:
exact-substring + tight semantic paraphrase with a negation guard) drops repeats so
they don't pile up; deeper semantic consolidation is the cron agent's job.

    python3 hooks/mem_extract.py --selfcheck   # pure-logic checks, no DB
"""
import json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mem_common import mem_ops, defang
from _llm import complete

QUEUE = os.path.expanduser("~/.claude/mem-queue.jsonl")
DONE = os.path.expanduser("~/.claude/mem-queue.done")
LOCK = os.path.expanduser("~/.claude/mem-extract.lock")
TAIL_CHARS = 60_000       # transcript tail given to the extractor

PROMPT = """You are a long-term-memory extractor for an AI coding agent. Below is a slice of a \
work-session transcript. Extract ONLY durable, non-derivable knowledge useful in FUTURE sessions:
- the user's preferences and stated "how to work" directives (preference)
- stable facts about the user, their hardware, projects, infrastructure (semantic)
- decisions with their reason (episodic)
- future intentions / plans / open tasks (prospective)
- established procedures "how to do X here" (procedural)

Do NOT save: transient task details, code contents, anything obvious from the repo, common \
knowledge, or secrets / tokens / passwords.

Do NOT store personal data about THIRD PARTIES. Memories are about the user. For anyone else \
(colleagues, clients, testers, contractors) never record names, contact details, a role tied to \
a name, health, location or other personal information. If the fact is useful without the \
person, store it with a role instead of a name ("the project's tester", "a network engineer"). \
If it only makes sense with the name, do not store it at all.

Answer STRICTLY as a JSON array (may be empty []), no prose, no markdown fences, each item:
{"type":"preference|semantic|episodic|prospective|procedural","content":"one self-contained \
sentence","importance":0.0-1.0,"confidence":0.0-1.0,"project":"repo name or null"}
Each content must stand alone (substitute the specifics). Max 6 items. Do NOT invent facts not in \
the transcript; if there is nothing to extract, return []. "type" must be exactly one of the five. \
Output must parse with a strict JSON parser."""


def transcript_text(path):
    """User+assistant text turns from a Claude Code transcript JSONL."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                m = j.get("message") or {}
                role, content = m.get("role"), m.get("content")
                if role not in ("user", "assistant"):
                    continue
                if isinstance(content, str):
                    txt = content
                else:
                    txt = " ".join(b.get("text", "") for b in (content or [])
                                   if isinstance(b, dict) and b.get("type") == "text")
                txt = txt.strip()
                if txt:
                    out.append(f"[{role}] {txt[:2000]}")
    except OSError:
        return None
    return "\n".join(out)[-TAIL_CHARS:] or None


def extract(text):
    raw = (complete(PROMPT, text) or "").strip()
    if not raw:
        raise RuntimeError("LLM returned empty (rate-limited/unreachable?) -- not marking done")
    # JSON-salvage: some models wrap output in <think>...</think> or ```json fences.
    raw = re.sub(r"(?is)<think>.*?</think>", "", raw)
    raw = re.sub(r"(?im)^\s*```(?:json)?\s*$", "", raw).strip()
    # Only a genuinely parseable list (incl. a real `[]`) may mark the transcript done; anything
    # else raises so main() retries next run instead of silently marking done with 0 saved.
    # Detect "no array present", NOT limit-phrase substrings -- a legit memory's content could
    # contain words like "quota" and must not be misread as a rate-limit reply.
    start, end = raw.find("["), raw.rfind("]")
    if start >= 0 and end > start:
        try:
            items = json.loads(raw[start:end + 1])
        except ValueError:
            items = None
        if isinstance(items, list):
            return [i for i in items
                    if isinstance(i, dict) and i.get("content")
                    and i.get("type") in ("preference", "semantic", "episodic",
                                           "prospective", "procedural")]
    # no parseable array -> error/limit/refusal, not a real empty result
    raise RuntimeError(f"LLM returned no JSON array (rate-limit/refusal?) -- "
                       f"not marking done: {raw[:200]!r}")


# Novelty gate: skip an extracted item we effectively already hold, so pure repeats don't pile up
# for the O(n^2) consolidator to merge later (Vestige-style write-time gating). Kept SAFE against
# the classic failure -- a correction ("X" -> "not X") is near-identical in embedding space, so a
# blind cosine gate would silently drop the update. Two guards: (1) a very TIGHT distance so only
# near-verbatim paraphrases trip it, (2) a negation-polarity check that keeps anything that flips a
# negation vs its nearest match. Genuine semantic near-dups (reworded, same polarity) still go to
# the consolidator, which reads meaning; this only stops the exact/near-exact restatements.
NOVELTY_MAXDIST = float(os.environ.get("MEM_NOVELTY_MAXDIST", "0.12"))
_NEG = re.compile(r"\b(?:not|no|never|don'?t|doesn'?t|isn'?t|aren'?t|can'?t|won'?t|"
                  r"без|нет|не|ни|нельзя)\b", re.I)


def _neg_differs(a, b):
    return bool(_NEG.search(a)) != bool(_NEG.search(b))


# Salient = the tokens a correction actually changes: numbers, identifiers/paths, and proper
# nouns. A true paraphrase reorders words but keeps these identical; a correction ("prefers Zed"
# -> "prefers Cursor", "1000 credits" -> "2000 credits") does not. Without this, a preference
# change that carries no negation word was silently swallowed by the paraphrase skip.
_SALIENT = re.compile(r"\b(?:\w*\d[\w.]*|[A-Za-z_][\w.-]*[._-][\w.-]*|[A-Z][A-Za-z0-9]{2,})\b")


def _salient_differs(a, b):
    sa = {t.lower() for t in _SALIENT.findall(a)}
    sb = {t.lower() for t in _SALIENT.findall(b)}
    return bool(sa ^ sb)


def is_duplicate(content):
    cand = content.strip().lower()
    if not cand:
        return False
    # (1) conservative exact-substring vs the top-5 candidates (not just #1): catches an exact
    # repeat even when it isn't the single closest hit.
    out = mem_ops("search", {"query": content[:400], "k": 5}, timeout=15) or ""
    for line in out.strip().splitlines():
        if ") " not in line:
            continue
        existing = line.split(") ", 1)[-1].strip().lower()
        if existing and (cand in existing or existing in cand):
            return True
    # (2) tight semantic paraphrase, with the negation guard.
    near = mem_ops("nearest", {"content": content[:400]}, timeout=15)
    try:
        hit = json.loads(near) if near else {}
    except Exception:
        hit = {}
    near_txt = hit.get("content") or ""
    if hit and hit.get("distance", 1.0) < NOVELTY_MAXDIST \
            and not _neg_differs(cand, near_txt.lower()) \
            and not _salient_differs(content, near_txt):
        return True
    return False


REEXTRACT_BYTES = 10 * 1024 * 1024   # re-extract a session each +10MB of growth: the big
                                     # per-project sessions are ONE transcript appended for
                                     # days, so path-only dedup extracted them once and never
                                     # revisited their new work. Key done on path#(size bucket)
                                     # -> a grown session re-enters the queue; transcript_text's
                                     # 60k-char tail feeds the recent turns, is_duplicate guards.


def done_key(tp):
    try:
        return f"{tp}#{os.path.getsize(tp) // REEXTRACT_BYTES}"
    except OSError:
        return tp


def main():
    dry = "--dry-run" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 20

    # stale-lock safe: a killed run (e.g. one blocked on the old TCC prompt) must not
    # deadlock the extractor forever -- ignore a lock older than 2h.
    if os.path.exists(LOCK):
        if time.time() - os.path.getmtime(LOCK) < 7200:
            print("another extract run holds the lock; exiting")
            return
        print("stale lock (>2h) -- removing")
        try:
            os.remove(LOCK)
        except OSError:
            pass
    open(LOCK, "w").close()
    try:
        done = set()
        if os.path.exists(DONE):
            done = set(open(DONE, encoding="utf-8").read().splitlines())
        queue, seen = [], set()
        if os.path.exists(QUEUE):
            for line in open(QUEUE, encoding="utf-8"):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                tp = r.get("transcript_path")
                k = done_key(tp) if tp else None
                if k and k not in done and k not in seen:
                    seen.add(k)
                    queue.append(r)
        print(f"{len(queue)} transcript(s) pending")
        for rec in queue[:limit]:
            tp = rec["transcript_path"]
            text = transcript_text(tp)
            if text is None:                 # unreadable right now -> retry next run, do NOT mark done
                print(f"  unreadable (will retry): {tp}")
                continue
            if len(text) < 500:              # genuinely tiny -> nothing durable, mark done
                print(f"  skip (tiny): {tp}")
                with open(DONE, "a", encoding="utf-8") as f:
                    f.write(done_key(tp) + "\n")
                continue
            try:
                items = extract(text)
            except Exception as e:
                print(f"  extract FAILED (will retry next run): {tp}: {e}")
                continue
            saved, failed = 0, 0
            for it in items:
                if is_duplicate(it["content"]):
                    print(f"  dup, skip: {it['content'][:80]}")
                    continue
                if dry:
                    print(f"  [dry] {it['type']}: {it['content'][:100]}")
                    saved += 1
                    continue
                r = mem_ops("write", {
                    # untrusted transcript-derived content: defang tags/control chars, and
                    # clamp so the model can't self-assign importance=1.0 to pin the profile.
                    "type": it["type"], "content": defang(it["content"]),
                    "importance": min(0.7, max(0.0, float(it.get("importance", 0.5)))),
                    "confidence": min(0.8, max(0.0, float(it.get("confidence", 0.7)))),
                    "project": it.get("project") or None,
                    "source": {"source_type": "assistant_inference",
                               "channel": "extractor",
                               "session_id": rec.get("session_id"),
                               "excerpt": f"session {rec.get('ts')} cwd={rec.get('cwd')}"},
                }, timeout=30)
                if r:
                    saved += 1
                    print(f"  {r.strip()}: {it['content'][:90]}")
                else:
                    failed += 1
                    print(f"  write FAILED (will retry): {it['content'][:80]}")
            print(f"  {tp}: {saved}/{len(items)} saved" + (f", {failed} FAILED" if failed else ""))
            # Only mark done when nothing failed -- a transient DB/embedder error must not
            # permanently lose extracted items (a done transcript is never re-extracted).
            if not dry and failed == 0:
                with open(DONE, "a", encoding="utf-8") as f:
                    f.write(done_key(tp) + "\n")
            elif failed:
                print(f"  NOT marking done -- {failed} write(s) failed, will retry next run")
    finally:
        os.remove(LOCK)


def _selfcheck():
    # the negation guard is the load-bearing safety bit: a correction ("X" -> "not X") is
    # near-identical in embedding space, so without this a tight-distance gate would drop it.
    assert _neg_differs("owner prefers zed", "owner does not prefer zed")
    assert _neg_differs("нельзя пушить", "можно пушить")
    assert not _neg_differs("owner prefers zed", "the owner likes zed")
    assert not _neg_differs("both say no here", "no also here")
    assert 0.0 < NOVELTY_MAXDIST < 0.5
    print("ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        main()
