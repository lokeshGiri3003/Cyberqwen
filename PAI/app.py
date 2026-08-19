#!/usr/bin/env python3
"""
app.py — CyberQwen PAI CLI. Drives agent.py (which talks to the model + MCP tools).

Commands:
  /playbooks   list methodology playbooks
  /memory <q>  recall engagement facts
  /clear       clear this session's short-term memory
  /burp        force a Burp reconnect check right now (no need to wait for the
               automatic retry, or to have opened Burp before app.py)
  /quit /exit  leave (shuts the MCP server down cleanly)
"""
from agent import Agent
from memory import memory_recall

BANNER = r"""
   ____      _               ___
  / ___|   _| |__  ___ _ __ / _ \__      _____ _ __
 | |  | | | | '_ \/ _ \ '__| | | \ \ /\ / / _ \ '_ \
 | |__| |_| | |_) |  __/ |  | |_| |\ V  V /  __/ | | |
  \____\__, |_.__/ \___|_|   \__\_\ \_/\_/ \___|_| |_|
       |___/   PAI — red team assistant (authorized use only)
  /playbooks   /memory <q>   /clear   /burp   /quit
"""

def main():
    print(BANNER)
    agent = Agent()                       # connects to the MCP server here
    try:
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            low = line.lower()
            if low in ("/quit", "/exit"):
                break
            if low == "/playbooks":
                print(agent.mcp.call("list_playbooks", {})); continue
            if low == "/clear":
                agent.short.clear(); print("[session memory cleared]"); continue
            if low == "/burp":
                ok, err = agent.mcp.reconnect_burp_now()
                agent.refresh_tools()
                print("[burp] connected" if ok else f"[burp] still not reachable: {err}")
                continue
            if low.startswith("/memory"):
                print(memory_recall(line[7:].strip() or "*")); continue
            try:
                print(f"\nCyberQwen> {agent.chat(line)}")
            except Exception as e:
                print(f"[error] {e}")
    finally:
        agent.close()                     # clean MCP server shutdown
        print("\n[bye]")

if __name__ == "__main__":
    main()
