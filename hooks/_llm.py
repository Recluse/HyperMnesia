"""Pluggable LLM step for memory extraction/consolidation. It only needs to turn text into a
short response the caller parses as JSON; nothing about the store depends on the model.

Backends (auto-selected, or force with HM_LLM_BACKEND = openai | ollama | cli):
  * openai : HM_LLM_URL (OpenAI-compatible /v1/chat/completions), HM_LLM_KEY, HM_LLM_MODEL
  * ollama : OLLAMA_URL/api/chat, HM_LLM_MODEL (e.g. 'qwen2.5:7b')
  * cli    : HM_LLM_CMD -- a command; the prompt is appended as an arg, the text goes on stdin,
             the final message is read from stdout (e.g. `codex exec ... -` style tools)
"""
import json, os, subprocess, urllib.request

URL = os.environ.get("HM_LLM_URL", "").rstrip("/")
KEY = os.environ.get("HM_LLM_KEY", "")
MODEL = os.environ.get("HM_LLM_MODEL", "qwen2.5:7b")
CMD = os.environ.get("HM_LLM_CMD", "")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def _backend():
    b = os.environ.get("HM_LLM_BACKEND", "").lower()
    if b:
        return b
    if URL:
        return "openai"
    if CMD:
        return "cli"
    return "ollama"


def _http(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def complete(system_prompt, user_text, timeout=600):
    """Return the model's text response (or '' on failure -- callers are fail-open)."""
    b = _backend()
    try:
        if b == "cli":
            argv = CMD.split() + [system_prompt]
            p = subprocess.run(argv, input=user_text.encode(),
                               capture_output=True, timeout=timeout)
            return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else ""
        msgs = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}]
        if b == "openai":
            h = {"Authorization": f"Bearer {KEY}"} if KEY else {}
            r = _http(f"{URL}/chat/completions",
                      {"model": MODEL, "messages": msgs, "temperature": 0}, h, timeout)
            return r["choices"][0]["message"]["content"]
        # ollama
        r = _http(f"{OLLAMA}/api/chat",
                  {"model": MODEL, "messages": msgs, "stream": False,
                   "options": {"temperature": 0}}, {}, timeout)
        return r["message"]["content"]
    except Exception:
        return ""
