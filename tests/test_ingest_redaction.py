#!/usr/bin/env python3
"""A secret in a markdown file must not reach the corpus.

The memory side has scrubbed since it existed, on the reasoning that a credential written into
a store gets re-injected into a prompt later. The DOCUMENT side did not, for no reason anyone
had written down -- and it is the same store and the same later prompt. The markdown a team
keeps (runbooks, incident notes, deployment guides) is exactly where a live token gets pasted
"just for a moment" and then committed.

Nothing downstream can repair it: the chunk text, its tsvector and its embedding are all built
from the file's text, so a secret that gets past this point is searchable, retrievable, and
ends up in a model's context.

Asserted on the emitted SQL, because that is what the ingester produces -- no database and no
embedder needed.

    python3 tests/test_ingest_redaction.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INGEST = os.path.join(ROOT, "ingest", "ingest_repo.py")

failures = []
ran = 0


def check(name, cond):
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def ingest(repo_dir, out):
    res = subprocess.run([sys.executable, INGEST, repo_dir, "testrepo", out],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return open(out, encoding="utf-8").read(), res.stderr


DOC = """# Deployment runbook

Set the CI variable:

    GITLAB_TOKEN=glpat-AbCdEfGhIjKlMnOpQrSt

Connect with `postgresql://svc:hunter2SuperSecret@db.internal:5432/app`, and the AWS key is
AKIAIOSFODNN7EXAMPLE.

The password reset flow is documented separately, and the release process is unchanged.
"""


def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        with open(os.path.join(repo, "runbook.md"), "w", encoding="utf-8") as f:
            f.write(DOC)
        out = os.path.join(tmp, "o.sql")
        sql, report = ingest(repo, out)

        # Each of these is a shape the redactor knows. They must not survive into the SQL --
        # which is what becomes the chunk, the tsvector and the embedding.
        for label, secret in [("a GitLab token", "glpat-AbCdEfGhIjKlMnOpQrSt"),
                              ("a connection-string password", "hunter2SuperSecret"),
                              ("an AWS key id", "AKIAIOSFODNN7EXAMPLE")]:
            check(f"{label} does not reach the corpus", secret not in sql)

        check("and the redaction is visible, not silent",
              "shaped like a secret" in report and "runbook.md" in report)

        # Redact, don't drop: the document is still ingested and still says a secret was there,
        # so the sentence around it stays searchable and the fact is recoverable by a human.
        check("the document is still ingested", "INSERT INTO documents" in sql)
        check("and says where the secret was", "REDACTED:gitlab-pat" in sql)

        # Structured shapes only. A redactor that ate ordinary prose would be turned off by the
        # first person who lost a paragraph to it, which is the real failure mode here.
        check("ordinary prose is untouched",
              "password reset flow" in sql and "release process is unchanged" in sql)

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
