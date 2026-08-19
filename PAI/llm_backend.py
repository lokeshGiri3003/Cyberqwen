#!/usr/bin/env python3
"""
llm_backend.py — one place that reads llm_config.json and knows how to talk to
either Ollama or LM Studio (OpenAI-compatible). Both agent.py and rag.py use it,
so a user only edits llm_config.json ("backend": "ollama" | "lmstudio").
"""
import os, json, urllib.request

_BASE = os.path.dirname(os.path.abspath(__file__))
_CFG_PATH = os.environ.get("CYBERQWEN_LLM_CONFIG", os.path.join(_BASE, "llm_config.json"))

def _load():
    try:
        cfg = json.load(open(_CFG_PATH))
    except Exception:
        cfg = {}
    # env override wins, else config, else ollama
    backend = os.environ.get("CYBERQWEN_BACKEND", cfg.get("backend", "ollama")).lower()
    if backend not in ("ollama", "lmstudio"):
        backend = "ollama"
    section = cfg.get(backend, {})
    return backend, section, cfg

BACKEND, _SEC, _CFG = _load()

CHAT_URL   = _SEC.get("chat_url",  "http://localhost:11434/api/chat")
EMBED_URL  = _SEC.get("embed_url", "http://localhost:11434/api/embeddings")
CHAT_MODEL = _SEC.get("chat_model",  "cyberqwen")
EMBED_MODEL= _SEC.get("embed_model", "nomic-embed-text")
# For lmstudio you can keep embeddings on ollama (nomic is already pulled there).
_EMB_VIA   = _SEC.get("embeddings_via", BACKEND)
if _EMB_VIA == "ollama" and BACKEND == "lmstudio":
    EMBED_URL = _CFG.get("ollama", {}).get("embed_url", "http://localhost:11434/api/embeddings")
    EMBED_MODEL = _CFG.get("ollama", {}).get("embed_model", "nomic-embed-text")

_IS_OPENAI = BACKEND == "lmstudio"      # LM Studio speaks OpenAI format


def _post(url, payload, timeout):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def chat(messages, model=None, temperature=0.1, timeout=1800):
    """Return the assistant's text reply. Works for both backends."""
    model = model or CHAT_MODEL
    if _IS_OPENAI:
        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "stream": False}
        resp = _post(CHAT_URL, payload, timeout)
        return resp["choices"][0]["message"]["content"]
    else:  # ollama /api/chat
        payload = {"model": model, "messages": messages, "stream": False,
                   "keep_alive": "10m", "options": {"temperature": temperature}}
        resp = _post(CHAT_URL, payload, timeout)
        return resp["message"]["content"]


def embed(text, model=None, timeout=60):
    """Return the raw embedding vector (list of floats). Works for both backends."""
    model = model or EMBED_MODEL
    openai_embed = _IS_OPENAI and _EMB_VIA != "ollama"
    if openai_embed:
        resp = _post(EMBED_URL, {"model": model, "input": text}, timeout)
        return resp["data"][0]["embedding"]
    else:  # ollama /api/embeddings
        resp = _post(EMBED_URL, {"model": model, "prompt": text}, timeout)
        return resp["embedding"]
