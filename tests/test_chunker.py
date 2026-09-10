#!/usr/bin/env python3
"""The chunker must produce chunks the embedder can actually embed WHOLE, and must not mistake
code for structure.

Two silent failures live here:

  * a block with no blank line in it was never subdivided -- split_long() only split on "\\n\\n",
    so a generated table, a pasted log, minified JSON or one long fenced block became a single
    chunk bounded only by MAX_FILE_BYTES (600 000). That chunk is stored and full-text indexed in
    FULL, while the embedder truncates it to its context window (TEI is called with
    truncate=true; Ollama defaults to it). The tail is then findable by exact word and invisible
    to the vector leg, and nothing reports a partial embedding -- ci/doctor.py only counts
    `embedding IS NULL`.
  * `#` at the start of a line inside a fenced block matched the heading regex, so every
    `# comment` in a shell example split the chunk mid-fence and pushed a line of code onto the
    heading path. Stored text ended with unbalanced fences and headings read "Install > #!/bin/sh".

    python3 tests/test_chunker.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ingest"))
from ingest_repo import chunk_md, split_long, approx_tokens, HARD_MAX_TOKENS  # noqa: E402

failures, ran = [], 0


def check(name, ok, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main():
    print("== nothing leaves the chunker above the embedder's ceiling ==")
    cases = [
        ("200KB with no blank line", "x" * 200_000),
        ("one enormous single line", "y" * 50_000),
        ("a wide table", "\n".join("| col | " + "z" * 400 + " |" for _ in range(400))),
        ("cyrillic, which costs ~2 chars per token", "я" * 60_000),
    ]
    for name, text in cases:
        chunks = split_long(text)
        worst = max(approx_tokens(c) for c in chunks)
        check(f"{name}: every chunk within the ceiling",
              worst <= HARD_MAX_TOKENS, f"worst chunk {worst} tokens > {HARD_MAX_TOKENS}")

    print("\n== and nothing is dropped on the way ==")
    # Character count, not equality: joining re-flows separators. What must not happen is text
    # silently disappearing, which is how a truncating chunker would look from the outside.
    for name, text in cases:
        kept = sum(len(c.replace("\n", "")) for c in split_long(text))
        want = len(text.replace("\n", ""))
        check(f"{name}: no text lost", kept >= want, f"kept {kept} of {want} characters")

    print("\n== short input is left alone ==")
    check("a small section stays one chunk", len(split_long("para one\n\npara two")) == 1)
    check("an empty string does not vanish into an empty list", len(split_long("")) == 1)

    print("\n== code fences are not structure ==")
    doc = ("# Title\n\nintro\n\n```sh\n# not a heading\n#!/bin/sh\necho hi\n```\n\n"
           "## Real heading\n\nbody\n")
    chunks = chunk_md(doc)
    heads = [hp for hp, _ in chunks]
    check("a '#' comment inside a fence does not become a heading",
          not any("not a heading" in h or "/bin/sh" in h for h in heads), str(heads))
    check("the real heading still nests", "Title > Real heading" in heads, str(heads))
    body = "\n".join(t for _, t in chunks)
    check("fences stay balanced in the stored text", body.count("```") % 2 == 0,
          f"{body.count('```')} fence markers")
    check("the fenced content is kept, not dropped", "echo hi" in body)

    print("\n== tildes open a fence too, and an unclosed fence does not eat the document ==")
    doc2 = "# T\n\n~~~\n# inside\n~~~\n\n## After\n\nx\n"
    check("~~~ fences are honoured",
          not any("inside" in h for h, _ in chunk_md(doc2)), str([h for h, _ in chunk_md(doc2)]))
    doc3 = "# T\n\n```\n# inside forever\n\n## Never closed\n"
    check("an unclosed fence still yields the document",
          "inside forever" in "\n".join(t for _, t in chunk_md(doc3)))

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
