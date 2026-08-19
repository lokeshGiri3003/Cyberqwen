#!/usr/bin/env python3
"""
rag_chat.py — MANUALLY add a chat conversation into CyberQwen's RAG store.

Nothing here runs automatically. You invoke it when you decide a conversation is
worth remembering — either:
  • from Python / app.py:   ingest_chat(messages, thread_id="t42", title="AD enum")
  • from the CLI:           python3 rag_chat.py --json thread.json --thread t42

Re-saving the same thread_id UPDATES it (old chunks for that thread are removed
first via rag's forget_source), so a growing conversation is refreshed, not
duplicated. Stored under source="chat:<thread_id>" so build_rag_context cites it
clearly and you can later drop it with rag.store().forget_source("chat:<id>").

Keep this file in the PAI project root next to rag.py.
"""

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rag  # shared store / embedding / schema

ROLE_LABEL = {"user": "User", "assistant": "Assistant",
              "system": "System", "tool": "Tool"}


def format_chat(messages, title=None, when=None,
                include_system=False, include_tool=True):
    """Turn a list of {role, content} into one readable transcript string."""
    when = when or date.today().isoformat()
    lines = [f"# Chat: {title} ({when})" if title else f"# Chat ({when})"]
    for m in messages:
        role = (m.get("role") or "").lower()
        if role == "system" and not include_system:
            continue
        if role == "tool" and not include_tool:
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{ROLE_LABEL.get(role, role.title())}: {content}")
    return "\n\n".join(lines)


def ingest_chat(messages, thread_id, title=None, when=None, update=True):
    """Add or refresh one conversation in the RAG knowledge base.
    `messages` is a list of {'role': ..., 'content': ...} dicts.
    Returns the number of chunks stored."""
    text = format_chat(messages, title=title, when=when)
    if not text.strip():
        return 0
    source = f"chat:{thread_id}"
    st = rag.store()
    if update:
        st.forget_source(source)      # remove prior version -> no duplicates
    return st.ingest(text, source=source, doc_date=(when or date.today().isoformat()))


def ingest_text(text, thread_id, title=None, when=None, update=True):
    """Manual add for a plain transcript / notes blob (no role structure)."""
    body = (f"# {title} ({when or date.today().isoformat()})\n\n{text}"
            if title else text)
    source = f"chat:{thread_id}"
    st = rag.store()
    if update:
        st.forget_source(source)
    return st.ingest(body, source=source, doc_date=(when or date.today().isoformat()))


def _load_messages(path):
    """Accept either a JSON list of {role,content} or {'messages':[...]}"""
    data = json.load(open(path))
    if isinstance(data, dict) and "messages" in data:
        return data["messages"]
    if isinstance(data, list):
        return data
    raise ValueError("JSON must be a list of {role,content} or have a 'messages' key")


def main():
    ap = argparse.ArgumentParser(description="Manually add chat history to CyberQwen's RAG.")
    ap.add_argument("--thread", required=True, help="stable thread id (dedup/update key)")
    ap.add_argument("--json", help="path to JSON: list of {role,content} or {messages:[...]}")
    ap.add_argument("--text", help="path to a plain-text transcript/notes file")
    ap.add_argument("--title", default=None)
    ap.add_argument("--date", default=None, help="ISO date; defaults to today")
    ap.add_argument("--no-update", action="store_true",
                    help="append instead of replacing this thread's existing chunks")
    args = ap.parse_args()

    if not (args.json or args.text):
        ap.error("give --json or --text")

    if args.json:
        msgs = _load_messages(args.json)
        n = ingest_chat(msgs, args.thread, title=args.title,
                        when=args.date, update=not args.no_update)
    else:
        n = ingest_text(open(args.text).read(), args.thread, title=args.title,
                        when=args.date, update=not args.no_update)

    print(f"Saved chat:{args.thread} to RAG — {n} chunk(s).")
    print("Store stats:", rag.store().stats())


if __name__ == "__main__":
    main()
