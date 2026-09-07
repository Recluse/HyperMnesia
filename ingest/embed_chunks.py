#!/usr/bin/env python3
"""Fill chunks.embedding with bge-m3 vectors. Resume-friendly (only NULL rows).

Context prefix = "<doc title> > <heading_path>" is prepended so a chunk embeds with its
place in the doc. SEQUENTIAL per worker: bge-m3 is compute-bound, so one batch at a time
saturates a CPU replica; to use N replicas run N sharded jobs (SHARD_N/SHARD_I over id).

Env: DATABASE_URL, EMBED_BACKEND/OLLAMA_URL/TEI_URL (see _common), EMBED_BATCH (16),
     SHARD_N (1), SHARD_I (0), EMBED_REPO (comma-separated repo scope).

SCOPE MATTERS BECAUSE THE CHUNK TEXT IS THE REQUEST BODY. One store serves many repos, and
this job sends the plaintext of every selected chunk to the embedder. That is unremarkable for
a local model and consequential the moment the endpoint is somewhere else -- a hosted API, a
borrowed GPU, a tunnel whose far end you do not administer. Scope the run with EMBED_REPO when
the embedder is not yours; note that a loopback URL proves nothing, since an ssh reverse tunnel
presents a remote service on 127.0.0.1.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import EMBED_MODEL, connect, embed_texts, vec_literal

BATCH = int(os.environ.get("EMBED_BATCH", "16"))
SHARD_N = int(os.environ.get("SHARD_N", "1"))
SHARD_I = int(os.environ.get("SHARD_I", "0"))
REPOS = [r.strip() for r in os.environ.get("EMBED_REPO", "").split(",") if r.strip()]


def main():
    conn = connect()
    cur = conn.cursor()
    shard = f" AND (c.id %% {SHARD_N}) = {SHARD_I}" if SHARD_N > 1 else ""
    where, args = ["c.embedding IS NULL"], []
    if REPOS:
        where.append("d.repo = ANY(%s)"); args.append(REPOS)
    cur.execute(f"""
        SELECT c.id, c.content, COALESCE(c.heading_path,''), COALESCE(d.title,'')
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE {' AND '.join(where)}{shard}
        ORDER BY c.id
    """, args)
    rows = cur.fetchall()
    total = len(rows)
    print(f"shard {SHARD_I}/{SHARD_N}: embedding {total} chunks "
          f"[{'repo in ' + str(REPOS) if REPOS else 'ALL repos'}] (batch={BATCH})", flush=True)

    t0, done, skipped = time.time(), 0, 0
    for i in range(0, total, BATCH):
        batch = rows[i:i + BATCH]
        prefixes, inputs, ids = [], [], []
        for cid, content, heading, title in batch:
            prefix = f"{title} > {heading}".strip(" >") or title or heading
            prefixes.append(prefix)
            inputs.append(f"{prefix}\n{content}" if prefix else content)
            ids.append(cid)
        # one retry, then SKIP the batch (stays NULL -> retried next run) so a single
        # pathological huge-chunk batch can't time out the whole job.
        try:
            vecs = embed_texts(inputs)
        except Exception:
            try:
                time.sleep(3)
                vecs = embed_texts(inputs)
            except Exception as exc2:
                skipped += len(batch)
                print(f"  shard{SHARD_I}: SKIP batch@{i} ({type(exc2).__name__}: {exc2})", flush=True)
                conn.rollback()
                continue
        for cid, prefix, v in zip(ids, prefixes, vecs):
            cur.execute("UPDATE chunks SET embedding = %s::vector, context_prefix = %s, "
                        "embedding_model = %s WHERE id = %s",
                        (vec_literal(v), prefix, EMBED_MODEL, cid))
        conn.commit()
        done += len(batch)
        if done % (BATCH * 10) < BATCH:
            print(f"  shard{SHARD_I}: {done}/{total} ({done/max(time.time()-t0,1e-6):.1f}/s)", flush=True)
    print(f"done shard {SHARD_I}/{SHARD_N}: {done} embedded, {skipped} skipped, {time.time()-t0:.0f}s",
          flush=True)


if __name__ == "__main__":
    main()
