#!/usr/bin/env python3
"""EMBED_BACKEND=openai: any server with an OpenAI-style /v1/embeddings API -- LM Studio,
llama.cpp's llama-server, vLLM, LocalAI -- can be the embedder.

A throwaway local server stands in for it: it checks the request shape the API defines, answers
with the vectors out of order (the API carries an `index` per row and does not promise the list
order), and, when a key is configured, refuses a request without the bearer header. The doctor's
reachability check goes to /v1/models on the same server.

    python3 tests/test_embed_backends.py
"""
import _isolate  # noqa: F401  (first: a throwaway HOME and HYPERMNESIA_HOME)
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
failures, ran = [], 0
seen = []


def check(name, cond, detail=""):
    global ran
    ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(name)


class Fake(BaseHTTPRequestHandler):
    key = ""

    def log_message(self, *args):
        pass

    def _authorized(self):
        return not Fake.key or self.headers.get("Authorization") == f"Bearer {Fake.key}"

    def do_GET(self):
        ok = self.path == "/v1/models" and self._authorized()
        self.send_response(200 if ok else 401)
        self.end_headers()
        self.wfile.write(b'{"data": [{"id": "text-embedding-bge-m3"}]}' if ok else b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append((self.path, body, self.headers.get("Authorization")))
        if self.path != "/v1/embeddings" or not self._authorized():
            self.send_response(401)
            self.end_headers()
            return
        rows = [{"object": "embedding", "index": i, "embedding": [float(i)] * 1024}
                for i in range(len(body["input"]))]
        out = json.dumps({"object": "list", "data": list(reversed(rows)),
                          "model": body["model"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)


def load(name, path, env):
    os.environ.update(env)
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    env = {"EMBED_BACKEND": "openai", "OPENAI_EMBED_URL": base + "/",
           "EMBED_MODEL": "text-embedding-bge-m3", "OPENAI_EMBED_KEY": ""}
    try:
        print("== the OpenAI-style backend ==")
        common = load("common_openai", ROOT / "ingest" / "_common.py", env)
        vecs = common.embed_texts(["first", "second", "third"])
        path, body, auth = seen[-1]
        check("posts to <base>/embeddings (a trailing slash on the base is fine)",
              path == "/v1/embeddings", path)
        check("with the model id and the inputs, as the API defines them",
              body == {"model": "text-embedding-bge-m3", "input": ["first", "second", "third"]},
              str(body))
        check("and no Authorization header when no key is set", auth is None, str(auth))
        check("vectors come back in input order, whatever order the server listed them in",
              [v[0] for v in vecs] == [0.0, 1.0, 2.0] and all(len(v) == 1024 for v in vecs))
        check("a single query embeds the same way", len(common.embed_query("a question")) == 1024)
        check("nothing to embed asks the server nothing", common.embed_texts([]) == [])

        Fake.key = "sk-local-test"
        keyed = load("common_openai_key", ROOT / "ingest" / "_common.py",
                     {**env, "OPENAI_EMBED_KEY": "sk-local-test"})
        keyed.embed_texts(["x"])
        check("a configured key goes as a bearer header", seen[-1][2] == "Bearer sk-local-test",
              str(seen[-1][2]))

        print("== the doctor checks the same server ==")
        doctor = load("doctor_openai", ROOT / "ci" / "doctor.py",
                      {**env, "OPENAI_EMBED_KEY": "sk-local-test"})
        out = []
        doctor.check_embedder(out)
        text = json.dumps(out, default=str)
        check("doctor reports the OpenAI-compatible embedder reachable",
              "Embedder reachable (OpenAI-compatible)" in text, text)
        Fake.key = "another-key"
        out = []
        doctor.check_embedder(out)
        check("and unreachable when the server refuses it",
              "Embedder unreachable (OpenAI-compatible)" in json.dumps(out, default=str))

        print("== the settings file may name it ==")
        common_src = (ROOT / "hooks" / "_mem_common.py").read_text(encoding="utf-8")
        body = common_src[common_src.index("_SETTABLE = frozenset(("):]
        body = body[:body.index("))")]
        check("OPENAI_EMBED_URL and OPENAI_EMBED_KEY are settable",
              '"OPENAI_EMBED_URL"' in body and '"OPENAI_EMBED_KEY"' in body)
    finally:
        server.shutdown()

    print(f"\n{ran - len(failures)}/{ran} checks passed"
          + (f"; FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
