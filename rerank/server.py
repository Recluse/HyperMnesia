#!/usr/bin/env python3
"""Cross-encoder reranker for HyperMnesia doc search.

bge-reranker-v2-m3 on Apple MPS (fp32 -- fp16 has attention glitches on MPS). Local
HTTP only (127.0.0.1). The MCP-server's search path POSTs candidate passages here and
reorders by score; if this service is down/cold, search fails open to plain RRF order.

Memory-friendly: the model is LAZY-loaded on first /rerank and UNLOADED after
RERANK_IDLE_SEC (default 600s) with no requests -- so it eats ~3.4GB only while you're
actively searching, ~150MB idle. Cold reload from cache is ~10s (within the caller's
rerank timeout; a cold search just falls open to RRF and the next one is reranked).

  POST /rerank {"query": str, "docs": [str, ...]} -> {"scores": [float, ...]}  (same order)
  GET  /health -> {"status":"ok", "model":..., "device":..., "loaded": bool}

Env: RERANK_MODEL (default BAAI/bge-reranker-v2-m3), PORT (8091), RERANK_MAXLEN (512),
     RERANK_IDLE_SEC (600; 0 disables idle-unload -> always resident).
A cross-encoder rerank of the top RRF candidates measurably improves recall@1.
"""
import gc, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODEL = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
PORT = int(os.environ.get("PORT", "8091"))
MAXLEN = int(os.environ.get("RERANK_MAXLEN", "512"))
IDLE_SEC = int(os.environ.get("RERANK_IDLE_SEC", "600"))
DEV = "mps" if torch.backends.mps.is_available() else "cpu"

_lock = threading.Lock()      # guards load/unload + serializes the single MPS graph
_tok = _model = None
_last_use = 0.0


def _ensure_loaded():
    global _tok, _model
    if _model is None:
        print(f"[rerank] loading {MODEL} on {DEV} ...", flush=True)
        _tok = AutoTokenizer.from_pretrained(MODEL)
        _model = AutoModelForSequenceClassification.from_pretrained(
            MODEL, torch_dtype=torch.float32).to(DEV).eval()
        print("[rerank] loaded", flush=True)


def _unload():
    global _tok, _model
    if _model is not None:
        _tok = _model = None
        gc.collect()
        if DEV == "mps":
            torch.mps.empty_cache()
        print(f"[rerank] idle > {IDLE_SEC}s -- model unloaded, RAM freed", flush=True)


def _reaper():
    while True:
        time.sleep(30)
        if IDLE_SEC > 0 and _model is not None and time.time() - _last_use > IDLE_SEC:
            with _lock:
                if _model is not None and time.time() - _last_use > IDLE_SEC:
                    _unload()


def score(query, docs):
    global _last_use
    if not query or not docs:
        return []
    pairs = [[query, (d or "")[:4000]] for d in docs]
    with _lock:
        _ensure_loaded()
        _last_use = time.time()
        with torch.no_grad():
            inp = _tok(pairs, padding=True, truncation=True, max_length=MAXLEN,
                       return_tensors="pt").to(DEV)
            out = _model(**inp).logits.view(-1).float().cpu().tolist()
        _last_use = time.time()
        return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "model": MODEL, "device": DEV,
                             "loaded": _model is not None})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/rerank":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            p = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, {"scores": score(p.get("query", ""), p.get("docs", []))})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=_reaper, daemon=True).start()
    print(f"[rerank] serving 127.0.0.1:{PORT} (lazy load, idle-unload {IDLE_SEC}s)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
