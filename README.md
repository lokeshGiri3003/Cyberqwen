# CyberQwen

**An autonomous AI agent for ethical penetration testing and cybersecurity assessment.**

CyberQwen PAI runs a locally-hosted language model that plans and executes security
tooling through a controlled loop. Its design goal is autonomy **without** giving up
three things that AI security assistants usually sacrifice:

1. **Operator control** — every command is gated behind a human approve/deny decision before it runs.
2. **Evidence grounding** — the agent cannot report findings that no tool actually produced.
3. **Confidentiality** — the model runs on your own hardware; engagement data never leaves it.

> ⚠️ For **authorized** security testing only. Run it against systems you own or have explicit written permission to test.

---

## Table of contents
- [Architecture](#architecture)
- [Safety & grounding](#safety--grounding-the-core-differentiators)
- [Requirements](#requirements)
- [Setup](#setup)
- [Configuration](#configuration)
- [Model backend: Ollama or LM Studio](#model-backend-ollama-or-lm-studio)
- [Knowledge base (RAG)](#knowledge-base-rag)
- [Running](#running)
- [Tool system](#tool-system)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
                    ┌─────────────────────────────┐
  Browser  ◄──────► │  Next.js UI (assistant-ui)  │   :3000   cyberqwen-ui-app
                    │  approval card, thread list │
                    └──────────────┬──────────────┘
                                   │ HTTP  /api/chat
                    ┌──────────────▼──────────────┐
                    │  server.py  (FastAPI :8000) │   host, in .venv
                    │  stream, /remember, threads │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │  agent.py  (reasoning loop)  │
                    │  approval gate · grounding   │
                    │  no-progress breaker · RAG   │
                    └───┬───────────────┬──────────┘
          llm_backend   │               │  MCP (stdio)
        ┌───────────────▼──┐   ┌────────▼────────────┐
        │ Ollama / LM Studio│   │  mcp_server.py      │  tool engine
        │  cyberqwen model  │   │  (FastMCP, stdio)   │
        └───────────────────┘   └────────┬────────────┘
                                          │ docker exec
                          ┌───────────────▼───────────────┐
                          │  kali-mcp-sandbox (container)  │  all tools run here
                          └────────────────────────────────┘

  RAG:  ingest_sources.py / rag_chat.py ──► rag.db (SQLite + nomic-embed vectors)
        build_rag_context() injects matches into the prompt each turn
  Also: Burp Suite MCP (optional, SSE :9876) merged in by mcp_client.py
```

**Components**

| File | Role |
|---|---|
| `agent.py` | Orchestration loop: prompts the model, parses tool calls, enforces the approval gate, grounding, breaker, and RAG/memory injection. |
| `server.py` | FastAPI backend: streams responses to the UI, handles `/remember`, thread CRUD, and approval-resume detection. |
| `mcp_server.py` | Tool engine (FastMCP over stdio). Exposes `bash`, `read_file`, `write_file`, `zap_scan`, `web_search`, `web_fetch`, playbooks, `write_report`. Runs commands inside `kali-mcp-sandbox`. |
| `mcp_client.py` | Connects to the core tool server (stdio) **and** optional Burp Suite MCP (SSE), merges their tools, auto-reconnects. |
| `llm_backend.py` | Single place that talks to either Ollama or LM Studio, selected by `llm_config.json`. |
| `rag.py` | Local semantic knowledge base (SQLite + `nomic-embed-text`). `build_rag_context()` injects matches each turn. |
| `ingest_sources.py` | Fills the RAG store from live sources: CVEs, CISA KEV, security news RSS. |
| `rag_chat.py` | Manually save a conversation into the RAG store. |
| `thread_store.py` | SQLite persistence of chat threads/messages for the assistant-ui thread list. |
| `memory.py` | Short-term conversation memory + engagement-fact recall. |
| `web_search.py` / `search.py` | Tavily search + Playwright/trafilatura page fetch (trust-ranked). |
| `frontend/` | Next.js + assistant-ui app (approval toolkit, custom runtime adapter). |

---

## Safety & grounding (the core differentiators)

- **Pre-execution approval gate.** Every `bash` command (and `write_file`) pauses the loop and shows the operator an **Approve / Deny** card before running. Installs and downloads count as bash, so they're gated too. Nothing with side effects runs unapproved.
- **Grounding verifier (`_verify_grounding`).** After the agent writes a conclusion, its security claims are checked against the session's actual tool output; unsupported terms are flagged as unverified.
- **No fabrication.** The system prompt forbids inventing findings/IPs/CVEs when a tool fails or returns nothing, and `write_report` is **structurally blocked when no tool has executed** — so the agent can't produce a report from thin air.
- **No-progress circuit breaker.** Consecutive steps that produce no successful execution (invalid / repeated / blocked calls) trip a breaker (`CYBERQWEN_STUCK_LIMIT`, default 3). Any successful tool run resets it, so real multi-step chains are never interrupted.
- **Premature file-read guard.** Blocks reading a results file that was never created earlier in the conversation (a known model hallucination).
- **Local by default.** Model + embeddings run on your hardware; target data stays on-site.

---

## Requirements

- **Python 3.10+**
- **Docker** (for the Kali sandbox, search sandbox, and UI container)
- **Ollama** *or* **LM Studio** (see [Model backend](#model-backend-ollama-or-lm-studio))
- The **CyberQwen GGUF** model file + its `Modelfile`
- (Optional) **Burp Suite** with the MCP Server extension, for +24 Burp tools
- A **Tavily API key** for web search (free tier at tavily.com)

---

## Setup

Layout — `setup.sh` lives in the parent dir; the app is in `PAI/`:

```
cyberqwen/
├── setup.sh
├── Modelfile
├── CyberQwen2.5-Coder-7B-v2.Q4_K_M.gguf
├── requirements.txt
├── env.example
└── PAI/            ← application code (.venv, frontend/, search_container/, ...)
```

Run it (idempotent — safe to re-run):

```bash
./setup.sh                     # venv + deps, ~/.cyberqwen data dir, .env,
                               # ollama create <model>, embedder pull, 3 containers
./setup.sh --with-kali-tools   # also apt-install nmap/hydra/gobuster/etc. in Kali
./setup.sh --skip-docker | --skip-ollama | --skip-ui
```

It creates the model with `ollama create <name> -f Modelfile` and writes the matching
`CYBERQWEN_MODEL` into `.env` so runtime name and created name always agree.

---

## Configuration

### `llm_config.json` — pick your backend (the one file most users touch)

```json
{ "backend": "ollama" }   // change to "lmstudio" to run on non-NVIDIA GPUs
```

Each backend has its own url/model block in the file. Nothing else in the code needs editing.

### `config.json` (search container) — Tavily key

```json
{ "tavily_api_key": "tvly-...", "web_fetch_timeout": 30 }
```

`web_search.py` reads the key from here, **not** from an environment variable.
Keep `config.json` out of version control.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CYBERQWEN_BACKEND` | (from config) | `ollama` or `lmstudio`; overrides `llm_config.json`. |
| `CYBERQWEN_MODEL` | config `chat_model` | Exact model name (must match `ollama list`). |
| `CYBERQWEN_CONFIRM` | `chained` | `off` \| `chained` \| `all`. bash is always gated regardless. |
| `CYBERQWEN_MAX_STEPS` | `20` | Max reasoning steps per turn. |
| `CYBERQWEN_STUCK_LIMIT` | `3` | No-progress breaker threshold. |
| `CYBERQWEN_OLLAMA_TIMEOUT` | `1800` | Per-generation timeout (s); high for slow CPU inference. |
| `CYBERQWEN_RAG_DB` | `~/.cyberqwen/rag.db` | RAG vector store. |
| `CYBERQWEN_THREADS_DB` | `~/.cyberqwen/threads.db` | Chat history store. |
| `CYBERQWEN_USE_BURP` | `1` | Set `0` to skip Burp entirely. |
| `BURP_MCP_URL` | `http://127.0.0.1:9876` | Burp MCP endpoint. |
| `KALI_TIMEOUT` | `120` | Default timeout for fast tools (bash/zap have none). |

---

## Model backend: Ollama or LM Studio

Both are supported; switch with one word in `llm_config.json`.

- **Ollama** (default) — best on NVIDIA GPUs or CPU. Talks `/api/chat`.
- **LM Studio** — for **AMD / Intel Arc / Apple Silicon** users with no NVIDIA. Uses
  Vulkan/Metal/ROCm and exposes an OpenAI-compatible server.

To use LM Studio:
1. Set `"backend": "lmstudio"` in `llm_config.json`.
2. In LM Studio → Developer → **Start Server**, load the same `CyberQwen…Q4_K_M.gguf` (no reconversion).
3. Copy its exact model name from the server panel into `lmstudio.chat_model`.

By default, `lmstudio` mode keeps **embeddings on Ollama** (`"embeddings_via": "ollama"`)
because your `rag.db` was built with `nomic-embed-text` vectors — mixing embedders would
break similarity search. Change it only if you have no Ollama at all (then re-ingest).

---

## Knowledge base (RAG)

The agent searches a local vector store each turn and answers from it, citing the source,
before reaching for web search.

**Populate it from live sources:**
```bash
python3 ingest_sources.py --cve                 # HIGH+CRITICAL CVEs (recommended)
python3 ingest_sources.py --kev                 # CISA Known-Exploited (small, high value)
python3 ingest_sources.py --all                 # CVE + KEV + security news RSS
python3 ingest_sources.py --cve --min-cvss 0    # every severity (~90k, slow)
```
Ingest is **idempotent and resumable** — re-running skips what's already stored, so a
cancelled run just continues. It's slow on CPU; run it detached:
```bash
nohup python3 ingest_sources.py --all > ingest.log 2>&1 &
```

**Save a conversation into the KB** — type in the chat box:
```
/remember                 # saves the current chat
/remember AD enum notes   # saves with a title
```
Re-saving the same chat updates it (no duplicates). Stored under `chat:<id>`.

**Verify retrieval works:**
```bash
python3 -c "import rag; [print(round(h['score'],3), h['source']) for h in rag.store().recall('remote code execution', k=5)]"
```

---

## Running

Start order matters for Burp (it connects once, at agent startup):

```bash
# 1. (optional) start Burp Suite + enable its MCP extension FIRST for +24 tools
# 2. containers
docker start kali-mcp-sandbox cyberqwen-search cyberqwen-ui-app

# 3. backend (host, in the venv)
cd PAI
source .venv/bin/activate
set -a; . ./.env; set +a
uvicorn server:app --host 0.0.0.0 --port 8000

# 4. open the UI
#    http://localhost:3000
```

Rebuilding the UI after frontend changes (note: `restart` alone keeps the old image):
```bash
docker build -t cyberqwen-ui frontend/ && \
docker rm -f cyberqwen-ui-app && \
docker run -d --name cyberqwen-ui-app -p 3000:3000 cyberqwen-ui
```

---

## Tool system

- **One executor: `bash`.** There is no `nmap`/`hydra`/`hashcat` tool — the model runs
  every program through `bash` and may install missing ones (`apt-get install …`),
  all under the approval gate. This removed a whole class of "invalid tool" loops.
- **Other tools:** `read_file`, `write_file`, `get_playbook`, `list_playbooks`,
  `zap_scan` (OWASP ZAP), `write_report`, `web_search`, `web_fetch`.
- **Execution sandbox:** everything runs inside the `kali-mcp-sandbox` container via
  `docker exec`, isolated from the host.
- **Burp Suite (optional):** if Burp + its MCP extension are running before startup,
  `mcp_client.py` merges ~24 Burp tools into the same namespace. Burp Community has no
  CLI, so `zap_scan` is the automatable web-scan path.
- **Large outputs** are saved to a file inside the container and previewed, not dumped
  whole into the model context.

---

## Project structure

```
PAI/
├── agent.py            # reasoning loop, safety gates, RAG/memory injection
├── server.py           # FastAPI backend (chat stream, /remember, threads)
├── mcp_server.py       # tool engine (FastMCP, stdio)
├── mcp_client.py       # core + Burp MCP client
├── llm_backend.py      # Ollama / LM Studio switch
├── llm_config.json     # ← pick backend here
├── rag.py              # semantic knowledge base
├── ingest_sources.py   # CVE / KEV / news → RAG
├── rag_chat.py         # manual chat → RAG
├── thread_store.py     # chat history (SQLite)
├── memory.py           # short-term memory
├── web_search.py       # Tavily search / fetch
├── tools_list.json     # tool definitions
├── skills/             # methodology playbooks (*.md)
├── search_container/   # search sandbox (Dockerfile, config.json)
└── frontend/           # Next.js + assistant-ui
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **404 on every turn** | `CYBERQWEN_MODEL` ≠ the name in `ollama list`. Match them exactly (tags/case matter). |
| **Only 9 core tools, no Burp** | Burp/MCP extension wasn't running at agent startup. Start Burp first, then the backend. |
| **`web_search` fails: TAVILY key** | Add your key to the search container's `config.json`. |
| **UI changes don't appear** | `docker restart` keeps the old image. Rebuild **and recreate** the container (see [Running](#running)); hard-refresh the browser (Ctrl-Shift-R). |
| **Approval shows as text, not a card** | Stale UI container (recreate it) or the toolkit isn't registered in `RuntimeProvider.tsx`. |
| **Agent loops / thrashes** | The no-progress breaker stops it after `CYBERQWEN_STUCK_LIMIT` wasted steps; check Ollama/LM Studio is reachable. |
| **Ingest interrupted** | Safe — it's committed per-document and resumes on re-run. |

---

## Status & notes

- Aroviq (a third-party runtime firewall) was evaluated and **removed** — on its shipped
  version it added latency/false-positives without real protection for this use case.
  The safety surface is the approval gate + grounding + guards above.
- **License:** _TODO — add your license._
- **Authors / credits:** _TODO._
