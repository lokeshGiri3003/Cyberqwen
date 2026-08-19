#!/usr/bin/env python3
"""
memory.py — memory for CyberQwen.

ShortTermMemory serves two callers with different needs:
- CLI (app.py, history=None): a stateful buffer on the shared Agent
  singleton — get()/add() — same as before this refactor.
- UI (server.py, history=<thread's messages>): stateless per-request
  trimming of the caller-supplied thread history — trim() — since the
  Agent instance is shared across every thread/user and can't safely hold
  UI conversation state on self.

LongTermMemory (SQLite-backed persistent facts) is kept below but not
wired into the agent's system prompt right now — planned to be replaced
by a RAG system later. memory_save/memory_recall remain callable directly
if anything still invokes them; nothing auto-injects results into the
prompt anymore.
"""

import sqlite3, os, re, time

DB_PATH = os.environ.get("CYBERQWEN_MEMORY",
                         os.path.expanduser("~/.cyberqwen/memory.db"))

IP_RE   = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
CVE_RE  = re.compile(r'CVE-\d{4}-\d{4,7}', re.I)
HOST_RE = re.compile(r'\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\.(?:htb|local|corp|internal|com|net|org|io)\b', re.I)

_STOP = set("the a an of to on in is are was were be been and or for with that this "
            "it its as at by from we you i me my our their has have had do does did "
            "what where when who how why which can could should would".split())

def _tokens(text):
    toks = re.findall(r'[a-zA-Z0-9._:/-]+', (text or "").lower())
    return [t for t in toks if len(t) > 1 and t not in _STOP]

def _entities(text):
    return set(x.lower() for x in
               IP_RE.findall(text or "") + CVE_RE.findall(text or "") + HOST_RE.findall(text or ""))

# ══════════════════════════════════════════════════════════════════════
# SHORT-TERM memory
# ══════════════════════════════════════════════════════════════════════
class ShortTermMemory:
    def __init__(self, max_messages=24):
        self.max_messages = max_messages
        self.messages = []

    def trim(self, messages):
        """UI path: messages already belong to the thread currently open in
        the browser. Stateless — returns the trimmed tail."""
        cleaned = [
            {"role": m.get("role"), "content": m.get("content")}
            for m in (messages or [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if len(cleaned) > self.max_messages:
            cleaned = cleaned[-self.max_messages:]
        return cleaned

    def get(self):
        """CLI path: the single-session stateful buffer."""
        return list(self.messages)

    def add(self, role, content):
        """CLI path."""
        self.messages.append({"role": role, "content": content})
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]

    def clear(self):
        self.messages = []

# ══════════════════════════════════════════════════════════════════════
# LONG-TERM (persistent) memory — SQLite. Not currently used by the agent's
# prompt; kept for direct tool calls and as a base for a future RAG layer.
# ══════════════════════════════════════════════════════════════════════
class LongTermMemory:
    def __init__(self, path=DB_PATH):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories(
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL,
                content TEXT UNIQUE,
                tags    TEXT
            )""")
        self.conn.commit()

    def save(self, content):
        content = (content or "").strip()
        if not content:
            return "(nothing to save)"
        tags = " ".join(sorted(_entities(content)))
        try:
            self.conn.execute("INSERT INTO memories(ts,content,tags) VALUES(?,?,?)",
                              (time.time(), content, tags))
            self.conn.commit()
            return f"saved: {content}"
        except sqlite3.IntegrityError:
            return f"already known: {content}"

    def recall(self, query, k=5):
        rows = self.conn.execute("SELECT content, tags FROM memories").fetchall()
        if not rows:
            return []
        q_tokens = set(_tokens(query))
        q_ents   = _entities(query)
        scored = []
        for content, tags in rows:
            c_tokens = set(_tokens(content))
            c_ents   = set((tags or "").split())
            overlap  = len(q_tokens & c_tokens)
            ent_hits = len(q_ents & c_ents)
            score    = overlap + 3 * ent_hits
            if score > 0:
                scored.append((score, content))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:k]]

    def all(self):
        return [r[0] for r in self.conn.execute(
            "SELECT content FROM memories ORDER BY ts DESC").fetchall()]

    def forget(self, substring):
        cur = self.conn.execute("DELETE FROM memories WHERE content LIKE ?",
                                (f"%{substring}%",))
        self.conn.commit()
        return f"forgot {cur.rowcount} memory item(s) matching {substring!r}"

    def wipe(self):
        self.conn.execute("DELETE FROM memories")
        self.conn.commit()
        return "wiped all memory"

_STORE = None
def store():
    global _STORE
    if _STORE is None:
        _STORE = LongTermMemory()
    return _STORE

def memory_save(content):
    return store().save(content)

def memory_recall(query, k=5):
    hits = store().recall(query, k)
    if not hits:
        return "(no relevant memories)"
    return "\n".join(f"- {h}" for h in hits)

def build_memory_context(query, k=5):
    """Disabled for now — pending a future RAG system."""
    return ""
