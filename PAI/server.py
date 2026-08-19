import asyncio, json, time, uuid, hashlib
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
# 1. Import the Agent class from agent.py
from agent import Agent
from thread_store import store
from rag_chat import ingest_chat

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3002",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 2. Instantiate the agent
agent_instance = Agent()

# Prefixes of the /remember confirmation lines — used to keep earlier save
# artifacts out of the RAG when a conversation is remembered more than once.
REMEMBER_CONFIRM_PREFIXES = (
    "✓ Saved this conversation",
    "Nothing to remember",
    "Couldn't save to knowledge base",
)

def _extract_text(content) -> str:
    """assistant-ui / AI SDK sends message content as either a plain string
    or a list of content parts (e.g. [{"type": "text", "text": "..."}], plus
    possibly image/tool parts). Normalize either shape down to a plain string
    so downstream (agent.chat -> memory._tokens) always gets a str."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if "text" in part and isinstance(part["text"], str):
                    parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return "" if content is None else str(content)

# ══════════════════════════════════════════════════════════════════════
# APPROVAL RESUME DETECTION — scans incoming messages for a resolved
# requestApproval tool-call (the human toolkit tool rendered in the
# browser). If found, extracts the operator's decision and the history
# BEFORE that tool-call/result pair, so agent.py resumes exactly where it
# paused without the model ever seeing the approval plumbing itself.
# ══════════════════════════════════════════════════════════════════════
APPROVAL_TOOL_NAME = "requestApproval"

def _find_pending_approval(messages):
    # GUARD: only resume an approval when the operator just resolved one — i.e.
    # the conversation does NOT end with a fresh user text prompt. If the user
    # typed a new question after leaving an old approval card unanswered, that
    # stale tool-call must NOT be resurrected; treat the new message as a fresh
    # prompt instead. (Fixes: a leftover approval hijacking a later question.)
    if messages:
        last = messages[-1]
        if isinstance(last, dict) and last.get("role") == "user":
            c = last.get("content")
            is_fresh_text = isinstance(c, str) or (
                isinstance(c, list) and not any(
                    isinstance(p, dict) and p.get("type") in ("tool-call", "tool-result")
                    for p in c
                )
            )
            if is_fresh_text:
                return None

    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool-call":
                continue
            if part.get("toolName") != APPROVAL_TOOL_NAME:
                continue
            call_args = part.get("args") or {}
            result = part.get("result")
            call_id = part.get("toolCallId")
            if result is None:
                for m2 in messages[i:]:
                    if not isinstance(m2, dict):
                        continue
                    c2 = m2.get("content")
                    if not isinstance(c2, list):
                        continue
                    for p2 in c2:
                        if (isinstance(p2, dict) and p2.get("type") == "tool-result"
                                and p2.get("toolCallId") == call_id):
                            result = p2.get("result")
            if result is not None:
                approved = bool(result.get("approved")) if isinstance(result, dict) else False
                decision = {
                    "tool_name": call_args.get("tool", ""),
                    "arguments": call_args.get("arguments", {}) or {},
                    "approved": approved,
                }
                return decision, messages[:i]
    return None

def _build_history(prior_messages):
    history = []
    for m in prior_messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        history.append({"role": role, "content": _extract_text(m.get("content", ""))})
    return history

def _last_user_text(prior_messages):
    for m in reversed(prior_messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return _extract_text(m.get("content", ""))
    return ""

# ══════════════════════════════════════════════════════════════════════
# /remember — save the current conversation to the long-term RAG knowledge
# base. Triggered by the user typing "/remember" (optionally
# "/remember <title>") in the chat box. Intercepted here BEFORE the agent
# sees it, so it never reaches the model. Re-running on the same chat
# updates the same entry (rag_chat stores under source="chat:<id>").
# ══════════════════════════════════════════════════════════════════════
def _remember_conversation(messages, thread_id=None):
    """messages includes the trailing /remember command. Returns a status
    string to stream back to the browser."""
    last = messages[-1]
    cmd_text = _extract_text(last.get("content", "")) if isinstance(last, dict) else ""
    title = cmd_text.strip()[len("/remember"):].strip() or None

    convo = []
    for m in messages[:-1]:                       # everything before the command
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(m.get("content", "")).strip()
        if not text:
            continue
        if role == "user" and text.lower().startswith("/remember"):
            continue                              # skip earlier commands
        if role == "assistant" and text.startswith(REMEMBER_CONFIRM_PREFIXES):
            continue                              # skip earlier confirmations
        convo.append({"role": role, "content": text})

    if not convo:
        return "Nothing to remember yet — start a conversation first."

    if not thread_id:
        first_user = next((c["content"] for c in convo if c["role"] == "user"), "")
        thread_id = hashlib.sha1(first_user.encode()).hexdigest()[:12]

    try:
        n = ingest_chat(convo, thread_id=thread_id, title=title)
    except Exception as e:
        return f"Couldn't save to knowledge base: {e}"
    return (f"✓ Saved this conversation to the long-term knowledge base "
            f"({n} chunk{'s' if n != 1 else ''}). Source: chat:{thread_id}")

async def stream_agent_response(messages: list, thread_id: str = None):
    if not messages:
        yield "(empty prompt received — nothing to process)"
        return

    # /remember intercept — save chat to RAG instead of calling the model
    last = messages[-1]
    last_text = _extract_text(last.get("content", "")) if isinstance(last, dict) else ""
    if last_text.strip().lower().startswith("/remember"):
        yield _remember_conversation(messages, thread_id)
        return

    pending = _find_pending_approval(messages)
    if pending:
        decision, prior_messages = pending
        user_prompt = _last_user_text(prior_messages)
        history = _build_history(prior_messages)
        res = agent_instance.chat(user_prompt, history=history, pending_decision=decision)
    else:
        last_msg = messages[-1]
        user_prompt = _extract_text(last_msg.get("content", "")) if isinstance(last_msg, dict) else str(last_msg)
        if not user_prompt.strip():
            yield "(empty prompt received — nothing to process)"
            return
        history = _build_history(messages[:-1])
        res = agent_instance.chat(user_prompt, history=history)

    # agent.chat() returns a dict instead of text when a gated tool call
    # needs operator approval — send it whole so the frontend can render
    # the requestApproval tool-call UI instead of plain text.
    if isinstance(res, dict) and res.get("__approval_request__"):
        yield json.dumps(res)
        return

    if hasattr(res, "__iter__") and not isinstance(res, (str, dict)):
        for chunk in res:
            yield str(chunk)
            await asyncio.sleep(0.01)
    elif hasattr(res, "__aiter__"):
        async for chunk in res:
            yield str(chunk)
    else:
        yield str(res)

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    data = await request.json()
    messages = data.get("messages", [])
    thread_id = data.get("threadId")          # optional; None -> content-hash fallback
    return StreamingResponse(
        stream_agent_response(messages, thread_id),
        media_type="text/plain"
    )

# ══════════════════════════════════════════════════════════════════════
# THREAD LIST — backs assistant-ui's RemoteThreadListAdapter.
# Matches the adapter contract exactly: list/initialize/rename/
# archive/unarchive/delete, plus a title endpoint and message
# load/append for the ThreadHistoryAdapter.
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/threads")
async def list_threads():
    return {"threads": store().list_threads()}

@app.post("/api/threads")
async def create_thread(request: Request):
    data = await request.json()
    thread_id = data.get("threadId") or str(uuid.uuid4())
    thread = store().create_thread(thread_id)
    return thread

@app.get("/api/threads/{thread_id}")
async def get_thread(thread_id: str):
    thread = store().get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    return thread

@app.patch("/api/threads/{thread_id}")
async def rename_thread(thread_id: str, request: Request):
    data = await request.json()
    store().rename_thread(thread_id, data.get("title", ""))
    return {"ok": True}

@app.post("/api/threads/{thread_id}/archive")
async def archive_thread(thread_id: str):
    store().set_archived(thread_id, True)
    return {"ok": True}

@app.post("/api/threads/{thread_id}/unarchive")
async def unarchive_thread(thread_id: str):
    store().set_archived(thread_id, False)
    return {"ok": True}

@app.delete("/api/threads/{thread_id}")
async def delete_thread(thread_id: str):
    store().delete_thread(thread_id)
    return {"ok": True}

@app.post("/api/threads/{thread_id}/title")
async def generate_title(thread_id: str, request: Request):
    """Simple local title: first ~50 chars of the first user message.
    No extra model call — keeps this fast and avoids another Ollama round trip
    just to name a conversation."""
    data = await request.json()
    messages = data.get("messages", [])
    first_user_text = ""
    for m in messages:
        if m.get("role") == "user":
            first_user_text = _extract_text(m.get("content", ""))
            break
    title = (first_user_text.strip()[:50] or "New conversation")
    if len(first_user_text.strip()) > 50:
        title += "..."
    store().rename_thread(thread_id, title)
    return {"title": title}

@app.get("/api/threads/{thread_id}/messages")
async def get_messages(thread_id: str):
    return {"messages": store().list_messages(thread_id)}

@app.post("/api/threads/{thread_id}/messages")
async def append_message(thread_id: str, request: Request):
    data = await request.json()
    item = data.get("item", {})
    store().append_message(thread_id, item)
    return {"ok": True}

@app.post("/api/threads/{thread_id}/remember")
def remember(thread_id: str):
    """Save a stored thread to the long-term RAG by id (for a future 'Save to
    knowledge base' button). The /remember chat command uses a separate path
    and does not need this endpoint."""
    items = store().list_messages(thread_id)   # [{"message": {...}, "parentId": ...}, ...]
    messages = []
    for it in items:
        msg = it.get("message", {}) if isinstance(it, dict) else {}
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(msg.get("content", "")).strip()
        if text:
            messages.append({"role": role, "content": text})
    if not messages:
        raise HTTPException(status_code=400, detail="thread has no messages to save")
    title = (store().get_thread(thread_id) or {}).get("title")
    n = ingest_chat(messages, thread_id=thread_id, title=title)
    return {"ok": True, "chunks": n}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
