#!/usr/bin/env python3
"""
mcp_server.py — the tool ENGINE.

Transport: stdio.  Run:  python3 mcp_server.py
"""
import os, json, shlex, base64, subprocess, datetime, sys, re
from mcp.server.fastmcp import FastMCP

BASE    = os.path.dirname(os.path.abspath(__file__))

# Default timeout for FAST tools (read_file, web_search, etc.)
# Set KALI_TIMEOUT=0 for no limit on any tool, or KALI_TIMEOUT=60 for 60s default.
# Long-running tools (zap_scan, bash) ignore this and run with NO timeout.
_default_timeout = os.environ.get("KALI_TIMEOUT", "120")
FAST_TIMEOUT = int(_default_timeout) if _default_timeout else None

SKILLS  = os.path.join(BASE, "skills")
CONFIG  = json.load(open(os.path.join(BASE, "tools_list.json")))
TOOLCFG = {t["name"]: t for t in CONFIG["command_tools"]}

# Tools that can run indefinitely (no timeout)
NO_TIMEOUT_TOOLS = {"zap_scan", "bash"}

mcp = FastMCP("cyberqwen-tools")

def _docker(container, command, timeout=None):
    """Run a command in the docker container. timeout=None means NO LIMIT."""
    try:
        cmd = ["docker", "exec", container, "bash", "-c", command]
        
        if timeout is None:
            # NO TIMEOUT — use Popen + communicate() to wait forever
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = proc.communicate()  # Waits indefinitely
            result = ((stdout or "") + (stderr or "")).strip() or "(ran, no output)"
            return result
        else:
            # Fast tools get a timeout
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return ((r.stdout or "") + (r.stderr or "")).strip() or "(ran, no output)"
            
    except FileNotFoundError:
        return "(error: docker not found on PATH)"
    except subprocess.TimeoutExpired:
        limit = f"{timeout}s" if timeout else "unlimited"
        return f"(timeout after {limit} — tool exceeded time limit)"
    except Exception as e:
        return f"(exec error: {e})"

# ══════════════════════════════════════════════════════════════════════
# LARGE-OUTPUT HANDLING — any tool's output over PREVIEW_CHARS gets saved
# whole inside the container and replaced with a preview + pointer, instead
# of being silently truncated (old zap_scan behavior) or dumped whole into
# the model's context (context-window risk regardless of GPU/compute).
# Applies generically via _dispatch() to every command_tool in
# tools_list.json, plus zap_scan below reuses it directly.
# ══════════════════════════════════════════════════════════════════════
PREVIEW_CHARS = int(os.environ.get("KALI_PREVIEW_CHARS", "4000"))

def _write_container_file(container, path, content):
    """Write text into a container file via base64 pipe — same trick write_file() uses."""
    b64 = base64.b64encode(content.encode()).decode()
    write_cmd = f"echo {b64} | base64 -d > {shlex.quote(path)}"
    try:
        r = subprocess.run(["docker", "exec", container, "bash", "-c", write_cmd],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False

def _preview_or_save(container, name, out, source_path=None):
    if len(out) <= PREVIEW_CHARS:
        return out

    # read_file is already reading a known path on disk — don't duplicate the
    # file, just point back at the original and suggest bash for slicing it.
    if name == "read_file" and source_path:
        return (f"{out[:PREVIEW_CHARS]}\n"
                f"[...{len(out) - PREVIEW_CHARS} more chars — this is the full file at "
                f"{source_path} already; use bash (grep/sed/head/tail) to pull specific "
                f"sections instead of re-reading it whole]")

    ts = int(datetime.datetime.now().timestamp())
    path = f"/tmp/tool_output_{name}_{ts}.txt"
    if not _write_container_file(container, path, out):
        return (out[:PREVIEW_CHARS] +
                f"\n[...{len(out) - PREVIEW_CHARS} more chars, save to disk failed, truncated]")
    return (f"{out[:PREVIEW_CHARS]}\n"
            f"[...{len(out) - PREVIEW_CHARS} more chars — full output saved to {path} in "
            f"{container}, call read_file(path='{path}') for the rest]")

def _dispatch(name, kwargs):
    t = TOOLCFG[name]
    if t.get("raw_args"):
        vals = kwargs
    else:
        vals = {k: shlex.quote(str(v)) for k, v in kwargs.items()}
    command = t["command"].format(**vals)
    
    # Long-running tools get NO timeout; fast tools get FAST_TIMEOUT
    timeout = None if name in NO_TIMEOUT_TOOLS else FAST_TIMEOUT
    
    # Log what we're doing
    limit_str = "NO LIMIT" if timeout is None else f"{timeout}s"
    print(f"[MCP-SERVER] Running {name} with timeout={limit_str}", file=sys.stderr, flush=True)
    
    out = _docker(t["container"], command, timeout=timeout)
    return _preview_or_save(t["container"], name, out, source_path=kwargs.get("path"))

for t in CONFIG["command_tools"]:
    _params = ", ".join(f"{a}: str" for a in t["args"])
    _argd   = ", ".join(f'"{a}": {a}' for a in t["args"])
    _src    = f"def _tool_{t['name']}({_params}) -> str:\n    return _dispatch({t['name']!r}, {{{_argd}}})\n"
    _ns = {"_dispatch": _dispatch}
    exec(_src, _ns)
    _fn = _ns[f"_tool_{t['name']}"]
    _fn.__doc__ = t["description"]
    mcp.add_tool(_fn, name=t["name"], description=t["description"])

@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write content to a file in the Kali sandbox."""
    b64 = base64.b64encode(content.encode()).decode()
    return _docker("kali-mcp-sandbox", f"echo {b64} | base64 -d > {shlex.quote(path)} && echo 'wrote {path}'", timeout=FAST_TIMEOUT)

@mcp.tool()
def get_playbook(name: str) -> str:
    """Load a methodology playbook."""
    p = os.path.join(SKILLS, name if name.endswith(".md") else name + ".md")
    return open(p).read() if os.path.exists(p) else f"(no playbook '{name}')"

@mcp.tool()
def list_playbooks() -> str:
    """List available methodology playbooks."""
    if not os.path.isdir(SKILLS):
        return "(no skills dir)"
    names = [f[:-3] for f in sorted(os.listdir(SKILLS)) if f.endswith(".md")]
    return "\n".join(f"- {n}" for n in names) if names else "(no playbooks)"

@mcp.tool()
def zap_scan(target: str) -> str:
    """Run an OWASP ZAP baseline web-app vulnerability scan against a target URL.
    Use this for web app security scanning (XSS, injection, misconfig checks)
    instead of Burp (Burp Community has no CLI/API and cannot be automated).
    Returns alert counts by risk level plus a preview of the report — not
    necessarily the full report, which is saved on disk in kali-mcp-sandbox
    and readable via read_file if it's large."""
    container = "kali-mcp-sandbox"
    target_q = shlex.quote(target)

    scan_cmd = (
        f"export PATH=$PATH:/usr/local/bin && "
        f"which zap.sh >/dev/null 2>&1 || echo 'ERROR: zap.sh not found in PATH'; "
        f"zap.sh -cmd -quickurl {target_q} -quickout /tmp/zap_report.html -quickprogress 2>&1 | tail -15; "
        f"echo '---SCAN DONE---'"
    )
    # Scan itself: unbounded, same as before (I/O/target-bound, not compute-bound)
    _docker(container, scan_cmd, timeout=None)

    # ZAP's report embeds a base64 PNG logo inside a multi-line <img ...> tag.
    # A line-based sed 's/<[^>]*>//g' can't match a tag that opens on one line
    # and closes several lines later, so the raw base64 leaked straight through
    # as ~40KB of noise. Strip img tags with a DOTALL-aware pass (python3, not
    # line-based) BEFORE the normal tag strip, so that noise never reaches the
    # model at all.
    report_cmd = (
        "cat /tmp/zap_report.html 2>/dev/null | "
        'python3 -c "import re,sys; '
        "t=sys.stdin.read(); "
        "t=re.sub(r'<img.*?>', '', t, flags=re.DOTALL); "
        "t=re.sub(r'<[^>]*>', '', t); "
        'print(t)" | '
        "grep -v '^\\s*$'"
    )
    report = _docker(container, report_cmd, timeout=FAST_TIMEOUT)

    if not report or report.startswith("(error"):
        return "(no report generated — zap_report.html missing or empty)"

    # Severity counts: report.count("High") anywhere in the full stripped text
    # was wildly inflated (column headers, repeated labels, CWE/WASC reference
    # text all contain these words). Scope the count to just the "Summary of
    # Alerts" section, which ZAP always renders as a compact Risk Level /
    # Number of Alerts table — far fewer false hits there.
    summary_section = report
    if "Summary of Alerts" in report:
        start = report.index("Summary of Alerts")
        end = report.index("Insights", start) if "Insights" in report[start:] else start + 500
        summary_section = report[start:end]

    counts = {}
    for lvl in ("High", "Medium", "Low", "Informational"):
        m = re.search(rf'{lvl}\s*\n?\s*(\d+)', summary_section)
        counts[lvl] = int(m.group(1)) if m else 0
    header = (
        f"ZAP scan complete for {target}. Per the report's own Summary of Alerts table — "
        f"High: {counts['High']}, Medium: {counts['Medium']}, "
        f"Low: {counts['Low']}, Informational: {counts['Informational']}. "
        f"Only report vulnerability types that literally appear by name in the alert "
        f"details below — do not infer or assume categories (XSS, SQLi, CSRF, etc.) "
        f"that aren't explicitly listed.\n"
    )
    return header + _preview_or_save(container, "zap_scan", report)

@mcp.tool()
def write_report(title: str = "Engagement Report") -> str:
    """Draft a pentest report skeleton."""
    try:
        from memory import store
        facts = store().all()
    except Exception:
        facts = []
    findings = "\n".join(f"- {f}" for f in facts) if facts else "- (none in memory)"
    return f"# {title}\nDate: {datetime.date.today().isoformat()}\n\n## Findings\n{findings}\n"

# ══════════════════════════════════════════════════════════════════════
# WEB SEARCH / FETCH — class-wrapped to prevent ANY name collisions
# ══════════════════════════════════════════════════════════════════════
class _WebTools:
    """Namespace to isolate web_search/web_fetch from @mcp.tool() names."""
    def __init__(self):
        self._search_fn = None
        self._fetch_fn = None
        self._err = None
        self._load()

    def _load(self):
        try:
            import web_search as _ws
            self._search_fn = _ws.web_search
            self._fetch_fn = _ws.web_fetch
        except Exception as e:
            self._err = str(e)

    def search(self, query, max_results=5):
        if self._err:
            return f"(error: web_search module failed to load: {self._err})"
        if not self._search_fn:
            return "(error: web_search not available)"
        return self._search_fn(query, "basic", max_results)

    def fetch(self, url, max_chars=4000):
        if self._err:
            return f"(error: web_fetch module failed to load: {self._err})"
        if not self._fetch_fn:
            return "(error: web_fetch not available)"
        return self._fetch_fn(url, max_chars)

_WEB = _WebTools()

@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for CURRENT or verifiable info. NOT for concepts you already know."""
    return _WEB.search(query, max_results)

@mcp.tool()
def web_fetch(url: str, max_chars: int = 4000) -> str:
    """Fetch and read the text of one web page."""
    return _WEB.fetch(url, max_chars)

if __name__ == "__main__":
    mcp.run()
