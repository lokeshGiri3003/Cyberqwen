#!/usr/bin/env python3
"""
rag.py — local knowledge base for CyberQwen, same architectural pattern as
memory.py's LongTermMemory: a separate SQLite store, a build_*_context()
function meant to be injected into the system prompt alongside
build_memory_context() in agent.py.

Difference from memory.py: this is semantic (embedding-based) recall over
chunked documents you ingest ahead of time (reports, notes, writeups —
~2 years of cybersecurity content), not short facts saved turn-by-turn
during a live session.

Embeddings come from Ollama's local nomic-embed-text model (already
pulled — 137M params, far cheaper to run than the 7.6B chat model), so
this doesn't add meaningful load to the same CPU bottleneck the chat
model already fights.

For a few years of personal/team content (thousands of chunks, not
millions), brute-force numpy cosine similarity is fast enough — no
vector DB dependency needed.
"""

import sqlite3, os, re, json, time, urllib.request
import numpy as np
import llm_backend

DB_PATH = os.environ.get("CYBERQWEN_RAG_DB",
                         os.path.expanduser("~/.cyberqwen/rag.db"))
OLLAMA_EMBED_URL = os.environ.get("OLLAMA_EMBED_URL", "http://localhost:11434/api/embeddings")
EMBED_MODEL = os.environ.get("CYBERQWEN_EMBED_MODEL", llm_backend.EMBED_MODEL)

# Chunking: word-based, with overlap so a fact split across a chunk boundary
# is still findable from either side.
CHUNK_WORDS = int(os.environ.get("CYBERQWEN_RAG_CHUNK_WORDS", "220"))
CHUNK_OVERLAP = int(os.environ.get("CYBERQWEN_RAG_CHUNK_OVERLAP", "40"))


def _embed(text: str) -> np.ndarray:
    """Call Ollama's local embeddings endpoint. Raises on failure — callers
    decide whether to skip or surface the error."""
    # Backend + embed model come from llm_config.json (see llm_backend.py).
    vec = np.array(llm_backend.embed(text, model=EMBED_MODEL), dtype=np.float32)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _chunk_text(text: str):
    """Split into overlapping word-count chunks. Simple and predictable —
    good enough for reports/notes; swap for a smarter splitter later if
    structured docs (headings, code blocks) need better boundaries."""
    words = text.split()
    if not words:
        return []
    step = max(CHUNK_WORDS - CHUNK_OVERLAP, 1)
    chunks = []
    for start in range(0, len(words), step):
        chunk = " ".join(words[start:start + CHUNK_WORDS])
        if chunk.strip():
            chunks.append(chunk)
        if start + CHUNK_WORDS >= len(words):
            break
    return chunks


class RAGStore:
    def __init__(self, path=DB_PATH):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks(
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                content  TEXT,
                source   TEXT,
                doc_date TEXT,
                ts       REAL,
                embedding BLOB
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
        self.conn.commit()
        self._cache = None  # (ids, contents, sources, dates, matrix) — built lazily

    def _invalidate_cache(self):
        self._cache = None

    def ingest(self, text: str, source: str, doc_date: str = ""):
        """Chunk + embed + store. source is a label (filename/path) shown at
        recall time for citation; doc_date is a free-form string (e.g.
        '2024-03' or '2024-03-15') so recall results can be ordered/filtered
        by recency later if needed. Returns the number of chunks stored."""
        chunks = _chunk_text(text)
        stored = 0
        for chunk in chunks:
            try:
                vec = _embed(chunk)
            except Exception as e:
                print(f"  [rag] embed failed for a chunk of {source}: {e}")
                continue
            self.conn.execute(
                "INSERT INTO chunks(content, source, doc_date, ts, embedding) "
                "VALUES (?, ?, ?, ?, ?)",
                (chunk, source, doc_date, time.time(), vec.tobytes()))
            stored += 1
        self.conn.commit()
        self._invalidate_cache()
        return stored

    def _load_cache(self):
        if self._cache is not None:
            return self._cache
        rows = self.conn.execute(
            "SELECT id, content, source, doc_date, embedding FROM chunks").fetchall()
        if not rows:
            self._cache = ([], [], [], [], np.zeros((0, 0), dtype=np.float32))
            return self._cache
        ids, contents, sources, dates, vecs = [], [], [], [], []
        for cid, content, source, doc_date, blob in rows:
            ids.append(cid)
            contents.append(content)
            sources.append(source)
            dates.append(doc_date)
            vecs.append(np.frombuffer(blob, dtype=np.float32))
        matrix = np.vstack(vecs)
        self._cache = (ids, contents, sources, dates, matrix)
        return self._cache

    def recall(self, query: str, k: int = 5, min_score: float = 0.3):
        """Returns up to k chunks as dicts: {content, source, doc_date, score}.
        min_score filters out weak matches — with cosine similarity on
        normalized vectors, anything below ~0.3 is usually noise, not a
        real match. Returns [] on any embedding failure rather than raising,
        since this sits in the hot path of every applicable turn."""
        ids, contents, sources, dates, matrix = self._load_cache()
        if matrix.shape[0] == 0:
            return []
        try:
            q_vec = _embed(query)
        except Exception as e:
            print(f"  [rag] query embed failed: {e}")
            return []
        scores = matrix @ q_vec  # vectors pre-normalized -> dot product = cosine sim
        top_idx = np.argsort(scores)[::-1][:k]
        results = []
        for i in top_idx:
            score = float(scores[i])
            if score < min_score:
                continue
            results.append({
                "content": contents[i],
                "source": sources[i],
                "doc_date": dates[i],
                "score": score,
            })
        return results

    def stats(self):
        row = self.conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT source) FROM chunks").fetchone()
        return {"chunks": row[0], "documents": row[1]}

    def forget_source(self, source: str):
        cur = self.conn.execute("DELETE FROM chunks WHERE source = ?", (source,))
        self.conn.commit()
        self._invalidate_cache()
        return f"removed {cur.rowcount} chunk(s) from {source!r}"

    def wipe(self):
        self.conn.execute("DELETE FROM chunks")
        self.conn.commit()
        self._invalidate_cache()
        return "wiped RAG knowledge base"


_STORE = None
def store():
    global _STORE
    if _STORE is None:
        _STORE = RAGStore()
    return _STORE


# ── cheap pre-filter: skip embedding entirely for messages unlikely to ──
# ── benefit from RAG (greetings, approvals, very short replies, or   ──
# ── messages that already look like a direct tool-use request handled ──
# ── by BASE_SYSTEM's existing routing). Keeps the embedding call off  ──
# ── the hot path for the majority of turns on this hardware.         ──
_SKIP_PATTERNS = re.compile(
    r'^\s*(hi|hello|hey|thanks|thank you|ok|okay|yes|no|approve|skip|'
    r'deny|cancel|stop|proceed|go|do it|run it)\s*[.!?]?\s*$',
    re.IGNORECASE,
)

def _looks_skippable(user_input: str) -> bool:
    text = (user_input or "").strip()
    if len(text) < 8:
        return True
    if _SKIP_PATTERNS.match(text):
        return True
    return False


def build_rag_context(query: str, k: int = 5) -> str:
    """AUTO-INJECTION: same pattern as memory.py's build_memory_context —
    call every turn, prepend result to the system prompt. Returns "" (no
    cost beyond a cheap regex check) for messages unlikely to need RAG."""
    if _looks_skippable(query):
        return ""
    try:
        hits = store().recall(query, k)
    except Exception as e:
        print(f"  [rag] recall failed, skipping: {e}")
        return ""
    if not hits:
        return ""
    lines = []
    for h in hits:
        date_str = f" ({h['doc_date']})" if h["doc_date"] else ""
        lines.append(f"- [{h['source']}{date_str}] {h['content']}")
    block = "\n".join(lines)
    return (
        "RAG CONTEXT (from your local knowledge base — cite the source shown "
        "in brackets if you use this; if it fully answers the question, "
        "answer directly and do NOT call web_search; only search if this "
        "context is missing or insufficient):\n" + block
    )
