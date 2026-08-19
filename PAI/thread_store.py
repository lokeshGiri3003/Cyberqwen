#!/usr/bin/env python3
"""
thread_store.py — SQLite-backed thread + message persistence for the
assistant-ui RemoteThreadListAdapter / ThreadHistoryAdapter.

Same pattern as memory.py's LongTermMemory (separate DB, separate concern:
this is UI conversation history, memory.py is the agent's engagement-fact
recall). Kept as its own file/DB so wiping one never touches the other.
"""
import sqlite3, os, time, json, uuid
from datetime import datetime

DB_PATH = os.environ.get("CYBERQWEN_THREADS_DB",
                         os.path.expanduser("~/.cyberqwen/threads.db"))


class ThreadStore:
    def __init__(self, path=DB_PATH):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS threads(
                id         TEXT PRIMARY KEY,
                title      TEXT,
                archived   INTEGER DEFAULT 0,
                created_at REAL,
                updated_at REAL
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS messages(
                id         TEXT PRIMARY KEY,
                thread_id  TEXT,
                role       TEXT,
                content    TEXT,
                created_at REAL
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id)")
        self.conn.commit()

    # ── threads ──────────────────────────────────────────────────────
    def list_threads(self):
        rows = self.conn.execute(
            "SELECT id, title, archived FROM threads ORDER BY updated_at DESC"
        ).fetchall()
        return [{"id": r[0], "title": r[1], "archived": bool(r[2])} for r in rows]

    def create_thread(self, thread_id, title=None):
        now = time.time()
        self.conn.execute(
            "INSERT OR IGNORE INTO threads(id, title, archived, created_at, updated_at) "
            "VALUES (?, ?, 0, ?, ?)",
            (thread_id, title, now, now))
        self.conn.commit()
        return self.get_thread(thread_id)

    def get_thread(self, thread_id):
        row = self.conn.execute(
            "SELECT id, title, archived FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "title": row[1], "archived": bool(row[2])}

    def rename_thread(self, thread_id, title):
        self.conn.execute(
            "UPDATE threads SET title = ?, updated_at = ? WHERE id = ?",
            (title, time.time(), thread_id))
        self.conn.commit()

    def set_archived(self, thread_id, archived: bool):
        self.conn.execute(
            "UPDATE threads SET archived = ?, updated_at = ? WHERE id = ?",
            (1 if archived else 0, time.time(), thread_id))
        self.conn.commit()

    def delete_thread(self, thread_id):
        self.conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
        self.conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
        self.conn.commit()

    # ── messages ─────────────────────────────────────────────────────
    def list_messages(self, thread_id):
        """Returns a list of {"message": ThreadMessage, "parentId": ...} items,
        exactly as assistant-ui's ExportedMessageRepository.messages expects."""
        rows = self.conn.execute(
            "SELECT content FROM messages WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,)).fetchall()
        out = []
        for (content,) in rows:
            try:
                out.append(json.loads(content))
            except (TypeError, ValueError):
                pass
        return out

    def append_message(self, thread_id, item):
        """item is the full ExportedMessageRepositoryItem: {"message": {...}, "parentId": ...}.
        Stored whole so load() can hand it straight back without reconstruction."""
        message = item.get("message", {})
        message_id = message.get("id") or str(uuid.uuid4())
        role = message.get("role", "user")
        created_at = message.get("createdAt")
        ts = time.time()
        if isinstance(created_at, str):
            try:
                ts = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        content_str = json.dumps(item)
        self.conn.execute(
            "INSERT OR REPLACE INTO messages(id, thread_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, thread_id, role, content_str, ts))
        self.conn.execute(
            "UPDATE threads SET updated_at = ? WHERE id = ?",
            (time.time(), thread_id))
        self.conn.commit()
        return message_id


_STORE = None
def store():
    global _STORE
    if _STORE is None:
        _STORE = ThreadStore()
    return _STORE
