"""Secret redaction for memory capture.

Personal memory is distilled from raw coding-session transcripts, which routinely contain API
keys, tokens and connection strings. We store the *distilled* text (and a verbatim source
excerpt), so a leaked credential would be persisted and later re-injected into a prompt. This
scrubs structured secrets before anything is written to `mem.*`.

Policy: **redact, don't drop.** "the OpenAI key is sk-..." becomes "the OpenAI key is
[REDACTED:openai-key]" — the fact that a key exists survives; the secret does not. Patterns are
structured (known key shapes + `name=value` assignments), so ordinary prose ("the password reset
flow") is untouched. This is a best-effort filter, not a guarantee — it catches the common shapes,
not every possible secret.
"""
import re

# (compiled pattern, label). Order matters only for readability; all are applied.
_PATTERNS = [
    # A private key block of any kind (RSA, EC, DSA, OPENSSH, ENCRYPTED, PGP "PRIVATE KEY BLOCK"),
    # from BEGIN to END -- or to the end of the text: a key cut short (a transcript turn clipped
    # mid-key, a paste missing its last line) is still a key.
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
                r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S), "private-key"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "anthropic-key"),
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"), "openai-key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai-key"),
    (re.compile(r"gh[posru]_[A-Za-z0-9]{30,}"), "github-token"),
    (re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"), "gitlab-pat"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "slack-token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws-key"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "google-key"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "jwt"),
    # connection string with an inline password: scheme://user:PASSWORD@host -> keep everything but the secret
    (re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:)(?P<secret>[^\s:/@]+)(?P<at>@)"), "url-password"),
    # An HTTP credential header, then a bare bearer token.
    (re.compile(r"(?i)(?P<key>\b(?:proxy-)?authorization\s*:\s*(?:bearer|basic|token|digest)\s+)"
                r"(?!\[REDACTED:)(?P<secret>[A-Za-z0-9._~+/=:-]{8,})"), "credential"),
    (re.compile(r"(?P<key>\bBearer\s+)(?!\[REDACTED:)(?P<secret>[A-Za-z0-9._~+/=-]{16,})"), "credential"),
    # name=value, name: value and "name": "value" for a sensitive name -- with any prefix on the
    # name (AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, CLIENT_SECRET, PGPASSWORD) and a closing quote
    # after it (JSON, quoted YAML). The name must END in the sensitive word, so max_tokens,
    # TOKEN_URL or password_reset_url are left alone; a value an earlier rule already redacted
    # (GITLAB_TOKEN=[REDACTED:gitlab-pat]) keeps its more specific label.
    (re.compile(r"(?i)(?P<key>\b[A-Za-z0-9_.-]*?(?:password|passwd|passphrase|secret|token|"
                r"api[_-]?key|access[_-]?key|private[_-]?key|credentials?)[\"']?\s*[:=]\s*[\"']?)"
                r"(?!\[REDACTED:)(?P<secret>[^\s\"',;}]{6,})"), "credential"),
    # The libpq variables, whose values can be shorter than the six characters asked of the
    # generic rule above: PGPASSWORD=<literal> is what people type at a shell.
    (re.compile(r"(?i)(?P<key>\bPG(?:PASSWORD|PASSFILE|SSLKEY)\s*=\s*[\"\']?)"
                r"(?P<secret>[^\s\"\']{4,})"), "credential"),
]


def scrub(text):
    """Replace structured secrets in `text` with [REDACTED:<label>]. Returns text unchanged if
    None/empty or nothing matched."""
    if not text:
        return text
    out = text
    for rx, label in _PATTERNS:
        if label == "url-password":
            out = rx.sub(lambda m: f"{m.group('pre')}[REDACTED:url-password]{m.group('at')}", out)
        elif label == "credential":
            out = rx.sub(lambda m: f"{m.group('key')}[REDACTED:credential]", out)
        else:
            out = rx.sub(f"[REDACTED:{label}]", out)
    return out


def _selfcheck():
    cases = [
        ("the OpenAI key is sk-abcdefghijklmnopqrstuvwxyz012345", "openai-key"),
        ("export ANTHROPIC=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA", "anthropic-key"),
        ("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "github-token"),
        ("db at postgresql://hm:s3cr3tpass@host:5432/db", "url-password"),
        ("PASSWORD=hunter2hunter2 in the env", "credential"),
        # prefixed forms count: PGPASSWORD is what this repo's own CI sets
        ("PGPASSWORD=hunter2hunter2", "credential"),
        ("AKIAIOSFODNN7EXAMPLE is the key", "aws-key"),
    ]
    for text, label in cases:
        red = scrub(text)
        assert f"[REDACTED:{label}]" in red, f"{label!r} not redacted in: {text!r} -> {red!r}"
        assert "sk-abcdef" not in red and "hunter2" not in red and "s3cr3t" not in red, red
    # ordinary prose must survive untouched
    for benign in ["the password reset flow is broken",
                   "we discussed the auth token design last week",
                   "secret sauce of the algorithm is memoization"]:
        assert scrub(benign) == benign, f"false positive: {benign!r} -> {scrub(benign)!r}"
    # url keeps host + user, drops only the secret
    assert "hm:" in scrub("postgresql://hm:s3cr3tpass@host/db") and "@host" in scrub("postgresql://hm:s3cr3tpass@host/db")
    print("ok")


if __name__ == "__main__":
    _selfcheck()
