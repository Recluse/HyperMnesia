#!/usr/bin/env python3
"""Ingester: walk a repo's tracked *.md, structural-chunk by heading, emit SQL.

Generates a self-contained .sql that idempotently reingests one repo's documents+chunks,
computing the composite fts (<HM_FTS_LANG> || simple) inline. No embeddings (the embedder
fills them after). Pipe the output into psql:  python ingest_repo.py <dir> <repo> out.sql

Env: HM_FTS_LANG (default 'english') -- the Postgres text-search config for stemming; 'simple'
is always added alongside so exact tokens (code identifiers, IDs) match regardless of language.
"""
import re
import sys, os, re, hashlib, subprocess

FTS_LANG = os.environ.get("HM_FTS_LANG", "english")
if not re.fullmatch(r"[a-z_]+", FTS_LANG):   # it is interpolated into SQL literals
    FTS_LANG = "english"

HEADING = re.compile(r'^(#{1,6})\s+(.*)')
TARGET_TOKENS = 400       # split sections larger than this by paragraph
HARD_MAX_TOKENS = 500     # below this, never split (keep small sections whole)
MAX_FILE_BYTES = 600_000  # skip giant generated/dumped md


def approx_tokens(text):
    """Script-adaptive token estimate (Metronix trick): Latin ~4 chars/token,
    Cyrillic/CJK ~2 chars/token, blended by the non-Latin alpha ratio. A naive
    len/4 under-counts our Russian corpus ~2x, so heading sections were chunked at
    ~800 tokens (double target) and could blow the Ollama bge-m3 context on embed."""
    n = len(text)
    if n == 0:
        return 0
    alpha = non_latin = 0
    for ch in text:
        if ch.isalpha():
            alpha += 1
            if ord(ch) > 0x024F:      # beyond Latin Extended-B
                non_latin += 1
    ratio = non_latin / alpha if alpha else 0.0
    chars_per_token = 2.0 * ratio + 4.0 * (1 - ratio)
    return max(1, int(n / chars_per_token))
_SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', 'target', 'dist', 'build',
              'out', '__pycache__', '.next', 'vendor', '.cache', '.idea', '.vscode',
              'coverage', 'bin', 'obj', '.pytest_cache', 'site-packages'}


def list_md(repo_dir):
    """(commit, [rel paths]) -- tracked *.md if a git repo, else a filtered os.walk."""
    try:
        commit = subprocess.check_output(["git", "-C", repo_dir, "rev-parse", "HEAD"],
                                         stderr=subprocess.DEVNULL).decode().strip()
        files = subprocess.check_output(["git", "-C", repo_dir, "ls-files", "*.md"],
                                        stderr=subprocess.DEVNULL).decode().splitlines()
    except Exception:
        commit, files = "nogit", []
        for root, dirs, fs in os.walk(repo_dir):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for fn in fs:
                if fn.lower().endswith(".md"):
                    files.append(os.path.relpath(os.path.join(root, fn), repo_dir).replace("\\", "/"))
    # drop anything under a skip dir (covers tracked-but-vendored too)
    return commit, [f for f in files if not (set(f.replace("\\", "/").split("/")) & _SKIP_DIRS)]


def dq(s: str) -> str:
    """Dollar-quote a string with a tag guaranteed absent from it."""
    tag = "m"
    while f"${tag}$" in s:
        tag += "m"
    return f"${tag}${s}${tag}$"


def sqlstr(s):
    return "NULL" if s is None else "'" + s.replace("'", "''") + "'"


def split_long(text):
    if approx_tokens(text) <= HARD_MAX_TOKENS:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        if cur and approx_tokens(cur + "\n\n" + para) > TARGET_TOKENS:
            out.append(cur.strip()); cur = ""
        cur = (cur + "\n\n" + para) if cur else para
    if cur.strip():
        out.append(cur.strip())
    return out or [text]


def chunk_md(content):
    """-> list of (heading_path, text)."""
    chunks, stack, buf = [], [], []

    def flush():
        text = "\n".join(buf).strip()
        buf.clear()
        if not text:
            return
        hp = " > ".join(t for _, t in stack)
        for piece in split_long(text):
            chunks.append((hp, piece))

    for line in content.split("\n"):
        m = HEADING.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return chunks


def doc_type(path):
    p = path.lower(); base = os.path.basename(p)
    if base.startswith("readme"):                       return "readme"
    if "runbook" in p or "playbook" in p or "release" in base: return "runbook"
    if "/adr" in p or base.startswith("adr"):           return "adr"
    if "plan" in p or "spec" in p or "design" in base:  return "spec"
    return "note"


def title_of(content, path):
    for line in content.split("\n"):
        m = HEADING.match(line)
        if m:
            return m.group(2).strip()
    return os.path.splitext(os.path.basename(path))[0]


def main():
    repo_dir, repo, out = sys.argv[1], sys.argv[2], sys.argv[3]
    # The repo name is the scope key; the MCP server sanitizes its own scope to [A-Za-z0-9._-],
    # so ingest must use the same alphabet or the two won't agree. Reject rather than silently mangle.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        sys.exit(f"repo name {repo!r} must match [A-Za-z0-9._-] (the MCP server scopes to this)")
    real_root = os.path.realpath(repo_dir)
    commit, files = list_md(repo_dir)

    n_docs = n_chunks = 0
    with open(out, "w", encoding="utf-8") as f:
        # Per-repo idempotent reingest: drop only THIS repo's docs (chunks cascade);
        # never touch components/constraints/relationships or other repos.
        f.write(f"BEGIN;\nDELETE FROM documents WHERE repo = {sqlstr(repo)};\n")
        for rel in files:
            ap = os.path.join(repo_dir, rel)
            # Don't follow symlinks: a committed `x.md -> /etc/secret` would otherwise be read
            # and stored as a repo document. Skip links and anything resolving outside the repo.
            if os.path.islink(ap) or not os.path.realpath(ap).startswith(real_root + os.sep):
                continue
            try:
                raw = open(ap, "rb").read()
            except OSError:
                continue
            if len(raw) > MAX_FILE_BYTES:      # skip giant generated/dumped md
                continue
            content = raw.decode("utf-8", "replace")
            chs = chunk_md(content)
            chash = hashlib.sha256(raw).hexdigest()
            tok = approx_tokens(content)
            dt, ttl = doc_type(rel), title_of(content, rel)
            relp = rel.replace("\\", "/")
            n_docs += 1
            doc_ins = (f"INSERT INTO documents (repo,path,doc_type,title,git_commit,"
                       f"content_hash,token_count) VALUES ({sqlstr(repo)},{sqlstr(relp)},"
                       f"{sqlstr(dt)},{sqlstr(ttl)},{sqlstr(commit)},{sqlstr(chash)},{tok})")
            if not chs:
                f.write(doc_ins + ";\n")
                continue
            vals = []
            for i, (hp, text) in enumerate(chs):
                n_chunks += 1
                hp_sql = sqlstr(hp) if hp else "''"
                vals.append(f"({hp_sql},{i},{dq(text)},{approx_tokens(text)})")
            f.write(
                f"WITH d AS ({doc_ins} RETURNING id)\n"
                f"INSERT INTO chunks (document_id,heading_path,ordinal,content,token_count,fts)\n"
                f"SELECT d.id, v.hp, v.ord, v.txt, v.tok,\n"
                f"       to_tsvector('{FTS_LANG}', v.txt) || to_tsvector('simple', v.txt)\n"
                f"FROM d, (VALUES\n  " + ",\n  ".join(vals) +
                "\n) AS v(hp,ord,txt,tok);\n")
        f.write("COMMIT;\n")
        # Re-ingest churns the whole repo's chunk set; refresh planner stats so the
        # HNSW/FTS cost estimates don't drift after a bulk DELETE+INSERT.
        f.write("ANALYZE chunks;\n")
    sys.stderr.write(f"{repo}: {n_docs} docs, {n_chunks} chunks -> {out}\n")


if __name__ == "__main__":
    main()
