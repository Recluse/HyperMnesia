#!/usr/bin/env python3
"""Ingester: walk a repo's tracked *.md, structural-chunk by heading, emit SQL.

Generates a self-contained .sql that idempotently reingests one repo's documents+chunks,
computing the composite fts (<HM_FTS_LANG> || simple) inline. No embeddings (the embedder
fills them after). Pipe the output into psql:  python ingest_repo.py <dir> <repo> out.sql

INCREMENTAL (--known-hashes FILE): by default the emitted SQL replaces the whole repo, which
drops every chunk (FK cascade) and therefore every embedding, so a one-file edit costs a full
re-embed. Pass a TSV of `path<TAB>content_hash` describing what the DB already holds and only
new/changed files are re-emitted; unchanged documents keep their chunks and their embeddings,
vanished ones are deleted. Produce that file however you can reach the DB -- the ingester
still needs no database connection of its own:

    psql "$DATABASE_URL" -tAF$'\t' \
      -c "SELECT path, content_hash FROM documents WHERE repo='myrepo'" > known.tsv
    python ingest_repo.py ~/code/myrepo myrepo out.sql --known-hashes known.tsv

Take that snapshot from the database you are about to load into: the emitted SQL asserts that
the repo still holds exactly as many documents as the file describes and aborts if it does
not, because a snapshot from elsewhere would mark documents "already stored" that were never
there -- a corpus with holes that only shows up as missing search results.

--walk enumerates the tree with os.walk instead of `git ls-files`, for a directory that is
git-ignored or otherwise untracked. Without it such a directory ingests as zero documents,
which a full ingest turns into "delete everything stored for this repo"; that case now exits
non-zero instead.

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


def list_md(repo_dir, walk=False):
    """(commit, [rel paths]) -- tracked *.md if a git repo, else a filtered os.walk.

    `walk=True` forces the filesystem walk. It has to be explicit rather than inferred: the
    auto-detect asks "is this a git repo", but the caller means "find the files", and a
    directory that IS inside a repo yet fully .gitignored answers yes to the first and returns
    nothing for the second -- `git -C` also walks up to the enclosing repo, so a subdirectory
    is enough to take the git branch. Working notes and scratch docs are exactly the material
    people keep out of git and exactly the material worth indexing.

    Walking reports the commit as 'nogit' on purpose: an untracked file's content has no
    relationship to HEAD, and freshness checks exempt 'nogit' rather than calling it stale.
    """
    commit, files = "nogit", None
    if not walk:
        try:
            commit = subprocess.check_output(["git", "-C", repo_dir, "rev-parse", "HEAD"],
                                             stderr=subprocess.DEVNULL).decode().strip()
            # -z, not splitlines(). git's core.quotePath defaults to TRUE, so a path with any
            # non-ASCII byte comes back C-escaped and wrapped in double quotes -- the literal
            # 21-character string "\320\224...md" instead of the filename. open() then fails
            # with ENOENT, the document is skipped, and on the incremental path that same quoted
            # string is what lands in the on-disk set: the REAL path is missing from it, so it is
            # treated as deleted and pruned from the store, embeddings included, while the
            # warning printed says the opposite. A NUL-separated listing is never quoted, and a
            # newline in a filename cannot split a record either.
            files = subprocess.check_output(["git", "-C", repo_dir, "ls-files", "-z", "*.md"],
                                            stderr=subprocess.DEVNULL).decode("utf-8", "surrogateescape")
            files = [f for f in files.split("\0") if f]
        except Exception:
            commit, files = "nogit", None
    if files is None:
        files = []
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


def _hard_slice(para):
    """Cut a single paragraph that is itself over the ceiling, on line boundaries.

    Splitting on blank lines alone leaves an unbounded chunk whenever the text has none: a
    generated table, a pasted log, minified JSON, one long fenced block. That chunk is then
    stored and full-text indexed IN FULL while the embedder silently truncates it to its context
    window -- TEI is called with truncate=true and Ollama defaults to it -- so the tail is
    findable by exact word and invisible to the vector leg, and nothing anywhere reports a
    partial embedding. Lines, not characters: a mid-token cut would corrupt both legs.
    """
    if approx_tokens(para) <= HARD_MAX_TOKENS:
        return [para]
    out, cur = [], []
    for line in para.split("\n"):
        if cur and approx_tokens("\n".join(cur + [line])) > TARGET_TOKENS:
            out.append("\n".join(cur)); cur = []
        cur.append(line)
        # A single line over the ceiling (minified JSON, one enormous table row) cannot be split
        # further on structure, so cut it on characters rather than emit it whole.
        while approx_tokens("\n".join(cur)) > HARD_MAX_TOKENS and len(cur) == 1:
            budget = max(1, int(len(cur[0]) * TARGET_TOKENS / max(1, approx_tokens(cur[0]))))
            out.append(cur[0][:budget]); cur = [cur[0][budget:]]
            if not cur[0]:
                cur = []
                break
    if cur and "\n".join(cur).strip():
        out.append("\n".join(cur))
    return out or [para]


def split_long(text):
    if approx_tokens(text) <= HARD_MAX_TOKENS:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        for piece in _hard_slice(para):
            if cur and approx_tokens(cur + "\n\n" + piece) > TARGET_TOKENS:
                out.append(cur.strip()); cur = ""
            cur = (cur + "\n\n" + piece) if cur else piece
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

    fence = None            # the ``` or ~~~ run that opened the current code block, if any
    for line in content.split("\n"):
        # A shell comment, a Python comment or a C preprocessor directive inside a fenced block
        # starts with '#' and matched HEADING, so every `# comment` in an example split the
        # chunk mid-fence and pushed a line of code onto the heading path. The result was
        # unbalanced fences in the stored text and heading_path values like
        # "Install > #!/bin/sh" -- shown to the agent as the document's structure.
        stripped = line.lstrip()
        if fence is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[0] * 3
                buf.append(line)
                continue
        else:
            if stripped.startswith(fence):
                fence = None
            buf.append(line)
            continue
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


def read_known_hashes(path):
    """{doc path: content_hash} from a `path<TAB>hash` TSV -- what the DB already holds."""
    known = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            p, _, h = line.partition("\t")
            if h:
                known[p.strip()] = h.strip()
    return known


def main():
    argv = sys.argv[1:]
    known = {}
    if "--known-hashes" in argv:
        i = argv.index("--known-hashes")
        known = read_known_hashes(argv[i + 1])
        del argv[i:i + 2]
    incremental = "--known-hashes" in sys.argv
    walk = "--walk" in argv
    argv = [a for a in argv if a != "--walk"]
    repo_dir, repo, out = argv[0], argv[1], argv[2]
    # The repo name is the scope key; the MCP server sanitizes its own scope to [A-Za-z0-9._-],
    # so ingest must use the same alphabet or the two won't agree. Reject rather than silently mangle.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        sys.exit(f"repo name {repo!r} must match [A-Za-z0-9._-] (the MCP server scopes to this)")
    real_root = os.path.realpath(repo_dir)
    commit, files = list_md(repo_dir, walk=walk)
    # A full ingest opens with DELETE FROM documents WHERE repo = <tag>, so writing an empty
    # corpus does not merely index nothing -- it deletes everything this repo already had, and
    # exits 0 while doing it. The likeliest cause is enumeration, not an empty tree: a
    # git-ignored directory inside a repo makes `git ls-files` return nothing while both git
    # calls succeed. Refuse, and name the way out.
    if not files:
        sys.exit(f"{repo}: no *.md found under {repo_dir} -- refusing to write an empty corpus "
                 f"(applying it would delete every document already stored for '{repo}'). "
                 f"If the directory is git-ignored or untracked, re-run with --walk.")

    n_docs = n_chunks = n_kept = 0
    on_disk, replaced, bodies, unreadable, oversize, missing = set(), set(), [], [], [], []
    for rel in files:
        ap = os.path.join(repo_dir, rel)
        relp = rel.replace("\\", "/")
        # Don't follow symlinks: a committed `x.md -> /etc/secret` would otherwise be read
        # and stored as a repo document. Skip links and anything resolving outside the repo.
        # A deliberate exclusion (symlink, oversize) SHOULD drop the stored document, and does,
        # because it never reaches on_disk below.
        if os.path.islink(ap) or not os.path.realpath(ap).startswith(real_root + os.sep):
            continue
        try:
            raw = open(ap, "rb").read()
        except OSError as exc:
            # NOT an exclusion: the file is right there, we just could not read it this time
            # (EACCES, EIO, too many open files). Letting it fall through to `gone` would DELETE
            # a perfectly good document, its chunks and its embeddings on a transient error, so
            # count it as present and leave the stored row alone.
            #
            # But only if it IS right there. "Present but unreadable" and "this path does not
            # exist" are different, and conflating them let a mangled enumeration entry protect
            # itself: the bogus name went into on_disk, the real document was absent from it and
            # therefore pruned, and the printed warning claimed the row had been preserved.
            # Anything that does not exist is a bug in the enumeration, not a transient error.
            if os.path.exists(ap):
                unreadable.append((relp, exc))
                on_disk.add(relp)
            else:
                missing.append((relp, exc))
            continue
        if len(raw) > MAX_FILE_BYTES:      # skip giant generated/dumped md
            oversize.append(relp)
            continue
        chash = hashlib.sha256(raw).hexdigest()
        on_disk.add(relp)
        # Unchanged content means unchanged chunks, and chunks carry the embeddings -- the
        # whole point of the incremental path is to leave those rows alone.
        if known.get(relp) == chash:
            n_kept += 1
            continue
        if relp in known:
            replaced.add(relp)          # known but changed -> its old row must go first
        content = raw.decode("utf-8", "replace")
        chs = chunk_md(content)
        tok = approx_tokens(content)
        dt, ttl = doc_type(rel), title_of(content, rel)
        n_docs += 1
        doc_ins = (f"INSERT INTO documents (repo,path,doc_type,title,git_commit,"
                   f"content_hash,token_count) VALUES ({sqlstr(repo)},{sqlstr(relp)},"
                   f"{sqlstr(dt)},{sqlstr(ttl)},{sqlstr(commit)},{sqlstr(chash)},{tok})")
        if not chs:
            bodies.append(doc_ins + ";\n")
            continue
        vals = []
        for i, (hp, text) in enumerate(chs):
            n_chunks += 1
            hp_sql = sqlstr(hp) if hp else "''"
            vals.append(f"({hp_sql},{i},{dq(text)},{approx_tokens(text)})")
        bodies.append(
            f"WITH d AS ({doc_ins} RETURNING id)\n"
            f"INSERT INTO chunks (document_id,heading_path,ordinal,content,token_count,fts)\n"
            f"SELECT d.id, v.hp, v.ord, v.txt, v.tok,\n"
            f"       to_tsvector('{FTS_LANG}', v.txt) || to_tsvector('simple', v.txt)\n"
            f"FROM d, (VALUES\n  " + ",\n  ".join(vals) +
            "\n) AS v(hp,ord,txt,tok);\n")

    # Known to the DB but no longer in the tree.
    gone = sorted(p for p in known if p not in on_disk)
    # Everything whose row must go before the inserts below: changed files and deleted ones.
    doomed = sorted(replaced | set(gone))

    with open(out, "w", encoding="utf-8") as f:
        f.write("BEGIN;\n")
        if incremental:
            # The TSV is trusted as ground truth about the DB, and a wrong one fails SILENTLY:
            # a document listed with its current hash is treated as already stored, so if the
            # DB never had that row it is simply never inserted -- an incomplete corpus that
            # answers "no results" instead of erroring. Snapshot and apply must therefore
            # describe the same rows; if they don't, abort the transaction rather than write a
            # corpus with holes in it.
            f.write("DO $do$ DECLARE n int; BEGIN\n"
                    f"  SELECT count(*) INTO n FROM documents WHERE repo = {sqlstr(repo)};\n"
                    f"  IF n <> {len(known)} THEN RAISE EXCEPTION\n"
                    f"    'known-hashes describes % documents but repo {repo} holds % -- "
                    f"stale or wrong snapshot, refusing incremental ingest', {len(known)}, n;\n"
                    "  END IF;\nEND $do$;\n")
            # freshness.py flags documents whose git_commit != HEAD, so leaving untouched rows
            # at their old commit would report the entire corpus as stale after any commit.
            # Their CONTENT is current as of this commit; only their chunks were not rewritten.
            f.write(f"UPDATE documents SET git_commit = {sqlstr(commit)} "
                    f"WHERE repo = {sqlstr(repo)};\n")
            # Drop only what is being replaced or has disappeared (chunks cascade); every other
            # document -- and its embeddings -- survives untouched.
            if doomed:
                f.write(f"DELETE FROM documents WHERE repo = {sqlstr(repo)} AND path IN (\n  "
                        + ",\n  ".join(sqlstr(p) for p in doomed) + ");\n")
        else:
            # Per-repo idempotent reingest: drop only THIS repo's docs (chunks cascade);
            # never touch components/constraints/relationships or other repos.
            f.write(f"DELETE FROM documents WHERE repo = {sqlstr(repo)};\n")
        for body in bodies:
            f.write(body)
        f.write("COMMIT;\n")
        # Re-ingest churns the repo's chunk set; refresh planner stats so the
        # HNSW/FTS cost estimates don't drift after a bulk DELETE+INSERT.
        if not incremental or bodies or gone:
            f.write("ANALYZE chunks;\n")
    for relp, exc in unreadable:
        sys.stderr.write(f"{repo}: WARNING could not read {relp} ({exc}) -- keeping whatever is "
                         f"already stored for it rather than deleting it\n")
    for relp in oversize:
        sys.stderr.write(f"{repo}: {relp} is over {MAX_FILE_BYTES} bytes -- excluded, and any "
                         f"stored copy will be removed\n")
    for relp, _ in missing:
        # Listed by the enumeration and absent from disk. The ordinary cause is a file deleted
        # from the working tree but not yet staged: `git ls-files` reads the index, so it still
        # names it. Treating that as deleted is CORRECT, and it is what happens -- the path
        # never entered on_disk, so it falls into `gone` and is pruned.
        #
        # (An earlier version exited non-zero here on the theory that any such path meant a
        # broken listing. That was wrong twice over: it broke ingest for anyone mid-edit with an
        # unstaged deletion, and the message claimed to be "refusing to emit SQL" from a point
        # after the SQL had already been written.)
        sys.stderr.write(f"{repo}: {relp} is listed by git but not on disk -- treating it as "
                         f"deleted; its stored document will be removed\n")
    if incremental:
        sys.stderr.write(f"{repo}: {n_docs} docs re-emitted ({n_chunks} chunks), "
                         f"{n_kept} unchanged kept, {len(gone)} removed -> {out}\n")
    else:
        sys.stderr.write(f"{repo}: {n_docs} docs, {n_chunks} chunks -> {out}\n")


if __name__ == "__main__":
    main()
