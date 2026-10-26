"""Pluggable LLM step for memory extraction/consolidation. It only needs to turn text into a
short response the caller parses as JSON; nothing about the store depends on the model.

Backends (auto-selected, or force with HM_LLM_BACKEND = openai | ollama | cli):
  * openai : HM_LLM_URL (OpenAI-compatible /v1/chat/completions), HM_LLM_KEY, HM_LLM_MODEL
  * ollama : OLLAMA_URL/api/chat, HM_LLM_MODEL (e.g. 'qwen2.5:7b')
  * cli    : HM_LLM_CMD -- a command; the prompt is appended as an arg, the text goes on stdin,
             the final message is read from stdout (e.g. `codex exec ... -` style tools)
"""
import json, os, signal, subprocess, urllib.request

# The shared settings file, applied before the constants below are computed. Its callers happen
# to import _mem_common first today, which applies it -- but "happens to, in this import order"
# is not a property worth resting the model choice on.
try:
    from _mem_common import load_env_file
    load_env_file()
except Exception:
    pass

URL = os.environ.get("HM_LLM_URL", "").rstrip("/")
KEY = os.environ.get("HM_LLM_KEY", "")
MODEL = os.environ.get("HM_LLM_MODEL", "qwen2.5:7b")
CMD = os.environ.get("HM_LLM_CMD", "")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def _backend():
    b = os.environ.get("HM_LLM_BACKEND", "").lower()
    # "auto" is a VALUE people set, not just the absence of one -- the console offers it, the
    # docs describe it, and returning it verbatim made it match neither branch in complete(),
    # which then fell through to ollama on localhost. A deployment with HM_LLM_URL set would
    # post every extraction to a port with nothing on it, get "" back, and -- the hooks being
    # fail-open -- mark the transcripts processed with nothing written.
    if b and b != "auto":
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


def _cli(argv, user_text, timeout):
    """Run a CLI backend in its own process group and kill the WHOLE group on any exit path.

    subprocess.run(timeout=) kills only the direct child. CLI agents (`codex exec` and the like)
    start helpers of their own, and those survived the timeout as orphans still holding the
    model call -- or, when our caller's deadline fired first, outlived this process entirely.
    """
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, _ = proc.communicate(input=user_text.encode(), timeout=timeout)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        raise
    return out.decode("utf-8", "replace") if proc.returncode == 0 else ""


def complete(system_prompt, user_text, timeout=600):
    """Return the model's text response (or '' on failure -- callers are fail-open)."""
    b = _backend()
    try:
        if b == "cli":
            return _cli(CMD.split() + [system_prompt], user_text, timeout)
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
