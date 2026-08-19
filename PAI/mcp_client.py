#!/usr/bin/env python3
"""
mcp_client.py — connects to MULTIPLE MCP servers and merges their tools:

  1) "core"  — mcp_server.py (YOUR tools: bash, web_search, zap_scan, ...) — stdio
  2) "burp"  — Burp Suite's MCP Server extension — SSE, http://127.0.0.1:9876
               (bare URL, NOT /sse — the /sse path fails on the current extension)
               OPTIONAL: only connects if Burp is running with the extension enabled.
               If Burp isn't reachable, we skip it silently — core tools still work.

Sync wrapper (background asyncio loop) so agent.py's sync loop is unaffected.
  client = MCPClient()
  client.list_tools()          -> {name: description}  (merged, both servers)
  client.call(name, args)      -> routed to whichever server owns that tool
  client.is_burp_tool(name)    -> True if the tool came from Burp
  client.close()

FIX: SSE ReadTimeout — Burp's keepalive stream drops after idle periods.
     We now pass timeout=600 to sse_client and auto-reconnect on call() if
     the session has gone stale. The reconnect is transparent to agent.py.

FIX: Integer sanitization — the MCP SDK serializes ints as floats in some
     cases, which breaks Burp's strict integer parsing. We recursively
     convert float-like values to ints before sending.
"""
import os, asyncio, threading, logging, time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

# Suppress the noisy SSE ReadTimeout tracebacks — they're expected on idle.
logging.getLogger("mcp.client.sse").setLevel(logging.CRITICAL)

BASE          = os.path.dirname(os.path.abspath(__file__))
CORE_SERVER   = os.path.join(BASE, "mcp_server.py")
BURP_SSE_URL  = os.environ.get("BURP_MCP_URL", "http://127.0.0.1:9876")
CONNECT_BURP  = os.environ.get("CYBERQWEN_USE_BURP", "1") != "0"
SSE_TIMEOUT   = int(os.environ.get("BURP_SSE_TIMEOUT", "600"))   # seconds

BURP_RETRY_INTERVAL = float(os.environ.get("BURP_RETRY_INTERVAL", "15"))

BLOCK_SETTERS = os.environ.get("CYBERQWEN_BLOCK_BURP_SETTERS", "0") == "1"
BURP_SETTERS  = {"set_project_options", "set_user_options", "set_proxy_intercept_state",
                 "set_task_execution_engine_state", "set_active_editor_contents"}


# ══════════════════════════════════════════════════════════════════════
# INTEGER SANITIZATION — Burp MCP server rejects floats like 5.0 where
# it expects integers. The MCP SDK sometimes serializes ints as floats.
# We recursively walk the args dict/list and convert float->int where
# the float value is integral (e.g. 5.0 -> 5).
# ══════════════════════════════════════════════════════════════════════
def _sanitize_ints(obj):
    if isinstance(obj, dict):
        return {k: _sanitize_ints(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_ints(v) for v in obj]
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


class _ServerConn:
    """One connected MCP server session (stdio or sse)."""
    def __init__(self, name, kind, **kw):
        self.name, self.kind, self.kw = name, kind, kw
        self.session = None
        self.ok      = False
        self.error   = None
        self._task        = None
        self._ready        = None
        self._close_event  = None

    async def connect(self):
        loop = asyncio.get_running_loop()
        self._ready       = loop.create_future()
        self._close_event = asyncio.Event()
        self._task = asyncio.create_task(self._lifetime())
        await self._ready

    async def _lifetime(self):
        try:
            if self.kind == "stdio":
                params = StdioServerParameters(command="python3", args=[self.kw["script"]])
                ctx = stdio_client(params)
            else:
                ctx = sse_client(self.kw["url"], timeout=SSE_TIMEOUT)
            async with ctx as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    self.ok = True
                    if not self._ready.done():
                        self._ready.set_result(True)
                    await self._close_event.wait()
        except Exception as e:
            self.ok, self.error = False, (str(e) or f"{type(e).__name__} (no message)")
            if self._ready and not self._ready.done():
                self._ready.set_result(False)
        finally:
            self.session = None

    async def close(self):
        if not self._task:
            return
        self._close_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except Exception:
            pass


class MCPClient:
    def __init__(self, core_script=CORE_SERVER, burp_url=BURP_SSE_URL, connect_burp=CONNECT_BURP):
        self._loop   = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        self.servers        = {}
        self.tool_owner     = {}
        self.burp_connected  = False
        self.burp_error      = None
        self._burp_url       = burp_url
        self._connect_burp   = connect_burp
        self._tools_cache    = None
        self._burp_last_try  = 0.0

        self._run(self._connect_all(core_script, burp_url, connect_burp))

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _connect_all(self, core_script, burp_url, connect_burp):
        core = _ServerConn("core", "stdio", script=core_script)
        await core.connect()
        if not core.ok:
            raise RuntimeError(f"core MCP server failed: {core.error}")
        self.servers["core"] = core

        if connect_burp:
            await self._do_connect_burp(burp_url)

    async def _do_connect_burp(self, url):
        self._burp_last_try = time.time()
        burp = _ServerConn("burp", "sse", url=url)
        await burp.connect()
        if burp.ok:
            self.servers["burp"] = burp
            self.burp_connected  = True
            self.burp_error      = None
            self._tools_cache    = None
        else:
            self.burp_connected = False
            self.burp_error     = burp.error
            self.servers.pop("burp", None)
            self._tools_cache   = None

    async def _reconnect_burp(self):
        old = self.servers.get("burp")
        if old:
            await old.close()
        await self._do_connect_burp(self._burp_url)

    async def _maybe_retry_burp(self, force=False):
        if not self._connect_burp or self.burp_connected:
            return
        if not force and (time.time() - self._burp_last_try) < BURP_RETRY_INTERVAL:
            return
        await self._do_connect_burp(self._burp_url)

    def list_tools(self):
        self._run(self._maybe_retry_burp())
        if self._tools_cache is not None:
            return self._tools_cache
        merged = {}
        for sname, conn in self.servers.items():
            if not conn.ok:
                continue
            res = self._run(conn.session.list_tools())
            for t in res.tools:
                if sname == "burp" and BLOCK_SETTERS and t.name in BURP_SETTERS:
                    continue
                exposed = t.name
                if sname == "burp" and exposed in merged:
                    exposed = f"burp_{exposed}"
                desc = t.description or ""
                try:
                    props = list((t.inputSchema or {}).get("properties", {}).keys())
                except Exception:
                    props = []
                if props:
                    desc = f"{desc} (args: {', '.join(props)})"
                merged[exposed]            = desc
                self.tool_owner[exposed]   = (sname, t.name)
        self._tools_cache = merged
        return merged

    def is_burp_tool(self, exposed_name):
        owner = self.tool_owner.get(exposed_name)
        return bool(owner and owner[0] == "burp")

    def reconnect_burp_now(self):
        self._run(self._maybe_retry_burp(force=True))
        return self.burp_connected, self.burp_error

    def refresh_tools(self):
        self._tools_cache = None
        return self.list_tools()

    # ── tool call with auto-reconnect + integer sanitization ────────────
    def call(self, exposed_name, arguments):
        owner = self.tool_owner.get(exposed_name)
        if not owner:
            return f"(unknown tool '{exposed_name}')"
        sname, real_name = owner
        conn = self.servers.get(sname)
        if not conn or not conn.ok:
            return f"(server '{sname}' is not connected)"

        # SANITIZE: convert 5.0 -> 5 before sending to MCP server
        clean_args = _sanitize_ints(arguments)

        try:
            res = self._run(conn.session.call_tool(real_name, clean_args))
            out = [getattr(b, "text", str(b)) for b in res.content]
            return "\n".join(out) if out else "(no result)"

        except Exception as e:
            if sname == "burp":
                print(f"  [burp] session dropped ({type(e).__name__}), reconnecting...")
                try:
                    self._run(self._reconnect_burp())
                    if self.burp_connected:
                        conn2 = self.servers.get("burp")
                        res   = self._run(conn2.session.call_tool(real_name, clean_args))
                        out   = [getattr(b, "text", str(b)) for b in res.content]
                        print("  [burp] reconnected OK")
                        return "\n".join(out) if out else "(no result)"
                    else:
                        return f"(burp reconnect failed: {self.burp_error})"
                except Exception as e2:
                    msg = str(e2) or f"{type(e2).__name__} (no message)"
                    return f"(burp tool error after reconnect: {msg})"
            return f"(tool error: {e})"

    def close(self):
        for conn in self.servers.values():
            try:
                self._run(conn.close())
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)


if __name__ == "__main__":
    c = MCPClient()
    print("core connected:", c.servers["core"].ok)
    print("burp connected:", c.burp_connected,
          ("" if c.burp_connected else f"(reason: {c.burp_error})"))
    tools = c.list_tools()
    print(f"\n{len(tools)} tools total:")
    for n in tools:
        prefix = "[burp] " if c.is_burp_tool(n) else "       "
        print(f"  {prefix}{n}")

    if c.burp_connected and "base64_encode" in tools:
        print("\n── Burp smoke test: base64_encode('hello world') ──")
        result = c.call("base64_encode", {"input": "hello world"})
        expected = "aGVsbG8gd29ybGQ="
        status = "✅" if expected in result else "❌"
        print(f"  {status} result: {result}")

    c.close()
