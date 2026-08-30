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
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", re.S), "private-key"),
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
    # generic name=value / name: value assignment for sensitive keys
    (re.compile(r"(?i)(?P<key>\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|auth[_-]?token|bearer)\b\s*[:=]\s*[\"']?)(?P<secret>[^\s\"']{8,})"), "credential"),
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
