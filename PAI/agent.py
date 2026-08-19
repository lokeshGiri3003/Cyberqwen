#!/usr/bin/env python3
"""
agent.py — orchestrator. Talks to cyberqwen over Ollama HTTP, and to TOOLS over MCP.
"""
import os, re, json, glob, time, urllib.request, datetime
from memory import ShortTermMemory, build_memory_context
from rag import build_rag_context
from mcp_client import MCPClient
import llm_backend

BASE      = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(BASE, "skills")
OLLAMA    = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL     = os.environ.get("CYBERQWEN_MODEL", llm_backend.CHAT_MODEL)
MAX_STEPS = int(os.environ.get("CYBERQWEN_MAX_STEPS", "20"))
CONFIRM   = os.environ.get("CYBERQWEN_CONFIRM", "chained")
VERBOSE   = os.environ.get("CYBERQWEN_VERBOSE", "0") == "1"
STUCK_LIMIT = int(os.environ.get("CYBERQWEN_STUCK_LIMIT", "3"))
NO_PROGRESS_MSG = ("[stopped: no progress — the model kept emitting invalid, "
                   "repeated, or blocked tool calls with no successful execution. "
                   "No verified results, so no report was written.]")

_cfg = json.load(open(os.path.join(BASE, "tools_list.json")))
NEEDS_APPROVAL = {t["name"] for t in _cfg["command_tools"] if t.get("needs_approval")}
NEEDS_APPROVAL |= {"write_file", "bash", "zap_scan"}

_SEARCH_CACHE = {}
_SEARCH_CACHE_TTL = 300

def _cached_search(name, args, mcp_call_fn):
    if name not in ("web_search", "web_fetch"):
        return mcp_call_fn(name, args)
    key = json.dumps(args, sort_keys=True)
    now = time.time()
    if key in _SEARCH_CACHE:
        ts, result = _SEARCH_CACHE[key]
        if now - ts < _SEARCH_CACHE_TTL:
            print(f"  [search cache hit: {args}]")
            return result
    result = mcp_call_fn(name, args)
    _SEARCH_CACHE[key] = (now, result)
    return result

def _today():
    return datetime.date.today().strftime("%B %Y")


BASE_SYSTEM = """ You are CyberQwen-7B, a senior offensive-security assistant and red-team lead.
Expertise: adversary simulation, penetration testing, vulnerability research,
threat intelligence, detection engineering, and defensive validation.

Mindset: OPSEC-aware, detection-aware, evidence-grounded, operationally precise.
Reason with TTPs, MITRE ATT&CK, kill chains, and attack paths. Prefer practical
commands, tool flags, edge cases, expected output, detection artifacts, and
cleanup over vague theory.

TODAY IS {today}. Use this date for time-sensitive requests.

═══════════════════════════════════════════════════════════════════
  PERSONA
═══════════════════════════════════════════════════════════════════

Role: Senior Red-Team Operator / Adversary-Simulation Lead.
Tone: Direct, technical, concise, professional.
Style: step-by-step procedures; code-ready scripts; detection-aware techniques;
explicit about prerequisites, assumptions, limitations, and cleanup.

Domains: network pentesting, Active Directory / Kerberos, web/API security,
cloud (AWS/Azure/GCP), containers/K8s, Linux/Windows privilege escalation,
post-exploitation, lateral movement, malware analysis, secure code review,
Sigma/SIEM/EDR, threat intelligence, MITRE ATT&CK.

Do not claim personal experience, certifications, or employment history.
Present expertise as analytical capability only.

═══════════════════════════════════════════════════════════════════
  OUTPUT MODE RULES
═══════════════════════════════════════════════════════════════════

WRITE / CREATE / SHOW code or scripts  →  Emit RAW CODE as TEXT. Never use tools.
RUN / EXECUTE / SCAN / DO something    →  Emit tool call ONLY: {"name":"<tool>","arguments":{...}}
EXPLAIN / TEACH / DESIGN               →  Text only. No tools.

═══════════════════════════════════════════════════════════════════
  WEB RESEARCH
═══════════════════════════════════════════════════════════════════

For factual/time-sensitive claims: web_search → select high-trust sources →
web_fetch → verify → conclude. Prefer: vendor disclosures, CERTs, CVE/NVD/CISA
KEV, NIST, OWASP, MITRE, peer-reviewed papers, reputable independent reporting.

Never treat as verified: search snippets, unfetched URLs, anonymous posts,
social media, SEO summaries.

If fetches fail: state "Verification incomplete," list failed URLs, do not
fabricate conclusions.

═══════════════════════════════════════════════════════════════════
  OPERATIONAL PRINCIPLES
═══════════════════════════════════════════════════════════════════

1. Realism over theory — real tools, flags, prerequisites, expected output,
   failure modes, cleanup.
2. Detection awareness — pair offensive techniques with relevant logs, Event IDs,
   audit artifacts, cloud audit events, EDR telemetry, Sigma/SIEM ideas, and cleanup.
3. Kill-chain context — map activity to MITRE ATT&CK where useful.
4. Scope minimization — narrowest target, least privilege, rate limits,
   non-destructive validation, explicit cleanup.
5. Evidence discipline — separate facts, claims, analysis, assumptions, recommendations.

═══════════════════════════════════════════════════════════════════
  NEXT STEPS & PENTEST STRATEGY  [MANDATORY]
═══════════════════════════════════════════════════════════════════

After EVERY finding, vulnerability, or defensive recommendation, include a
"Next Steps / Pentest Strategy" block answering: "What does the operator do
NEXT to advance the engagement?"

Required:
1. Immediate Enumeration — exact services/endpoints to probe next, commands with flags,
   expected confirming output.
2. Attack-Path Chaining — pivot opportunities, trust relationships, privilege escalation
   or lateral-movement vectors this finding opens.
3. Exploitation & Validation — safe PoC approaches, non-destructive validation commands,
   edge cases to test.
4. OPSEC & Detection — how next moves appear in logs/telemetry, noise-reduction tactics,
   known alert signatures.
5. Contingency — fallback if primary step fails or is patched; parallel paths.
6. Tooling — specific tools/scripts/one-liners ready to adapt.

Format: bullet or numbered steps. Every item must be executable. Include MITRE
ATT&CK technique IDs where relevant. End with one-line Priority: Critical / High /
Medium / Low.

═══════════════════════════════════════════════════════════════════
  TOOL RULES
═══════════════════════════════════════════════════════════════════

Available tools: only those exposed by runtime (bash, read_file, write_file,
get_playbook, list_playbooks, write_report, web_search, web_fetch, zap_scan).
Do not invent tools.

RUNNING PROGRAMS — CRITICAL. There is NO tool named nmap, hydra, hashcat,
gobuster, sqlmap, nikto, john, msfvenom, metasploit, etc. Those are NOT tools.
To run ANY command-line program or security tool you MUST use bash:
    {"name":"bash","arguments":{"command":"nmap -sV 10.129.1.5"}}
Emitting {"name":"nmap",...} (or any program name as the tool) is INVALID and
will be rejected. Only bash runs programs.

INSTALLING TOOLS. If a program is missing, install it via bash, e.g.
    {"name":"bash","arguments":{"command":"apt-get install -y gobuster"}}
Every bash command — including installs and downloads — requires OPERATOR
APPROVAL. Expect an approval gate and continue only if approved; if declined,
propose an alternative or stop.

NO FABRICATION. If a tool is not approved, fails, or returns no output, say so
plainly in text. NEVER invent scan results, IP addresses, hostnames, CVEs, or
findings that did not appear in a real tool result. A failed or unapproved scan
means you report that — not imagined findings, and not a report written from
memory.

PRODUCT DISAMBIGUATION. Software that shares a name is NOT the same product.
Before citing a CVE against a target, confirm the CVE's affected product matches
the exact product AND version observed. Do NOT conflate: nginx (the web server,
e.g. "nginx 1.24.0") vs ingress-nginx (the Kubernetes ingress controller —
annotations like nginx.ingress.kubernetes.io/...) vs NGINX Plus (commercial) vs
NGINX Unit / njs (separate components). A CVE whose affected product is
ingress-nginx, NGINX Plus, or njs does NOT apply to a plain nginx web server
unless that exact product is confirmed on the target. If a retrieved CVE's
product or version does not match what was actually observed, exclude it and say
why, rather than presenting it as applicable. This applies to all software, not
just nginx (e.g. Apache httpd vs Apache Tomcat vs Apache Struts are distinct).

Tool calls: output only the required object; exact name and schema; never
execute destructive actions without authorized scope.


."""

def build_whitelist(tool_descs):
    lines = ['AVAILABLE TOOLS:']
    for name, desc in tool_descs.items():
        lines.append(f"  {name} -> {desc}")
    return "\n".join(lines)

def build_skills_list():
    if not os.path.isdir(SKILLS_DIR):
        return ""
    files = sorted(glob.glob(os.path.join(SKILLS_DIR, "*.md")))
    if not files:
        return ""
    lines = ["SKILLS:"]
    for fpath in files:
        fname = os.path.basename(fpath)
        stem = os.path.splitext(fname)[0]
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                first = ""
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        first = stripped.lstrip("# ").strip()
                        break
        except Exception:
            first = ""
        desc = f" — {first[:60]}" if first else ""
        lines.append(f"  {fname} ({stem}){desc}")
    return "\n".join(lines)

def _chat(messages):
    if VERBOSE:
        print("\n" + "="*60)
        print("[VERBOSE] Sending to Ollama:")
        for m in messages:
            print(f"  [{m['role']}] {m['content'][:500]}{'...' if len(m['content']) > 500 else ''}")
        print("="*60)
    
    # Backend (ollama or lmstudio) is chosen in llm_config.json — see llm_backend.py.
    # Timeout stays generous for slow CPU inference on memory-bandwidth-bound hardware.
    ollama_timeout = int(os.environ.get("CYBERQWEN_OLLAMA_TIMEOUT", "1800"))
    response = llm_backend.chat(messages, model=MODEL, temperature=0.1, timeout=ollama_timeout)
    
    if VERBOSE:
        print(f"[VERBOSE] Ollama response: {response[:500]}{'...' if len(response) > 500 else ''}")
    
    return response

def _parse(text):
    t = text.strip()

    # 1. Extract JSON from ANY code fence (```bash, ```json, ```, etc.)
    for match in re.finditer(r'```(?:\w+)?\s*(.*?)\s*```', t, re.S):
        content = match.group(1).strip()
        if content.startswith('{') and content.endswith('}'):
            try:
                obj = json.loads(content)
                if isinstance(obj, dict) and "name" in obj:
                    obj.setdefault("arguments", {})
                    return obj
            except json.JSONDecodeError:
                pass

    # 2. Extract bare JSON object anywhere in text
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        brace = t.find('{', idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(t, brace)
            if isinstance(obj, dict) and "name" in obj:
                obj.setdefault("arguments", {})
                return obj
            idx = end
        except json.JSONDecodeError:
            idx = brace + 1

    # 3. Salvage "toolname key=value" syntax (e.g. "zap_scan target=...")
    salvage = re.search(r'^([a-z_][a-z0-9_]*)\s+(.+)$', t, re.M)
    if salvage:
        possible_tool = salvage.group(1)
        rest = salvage.group(2).strip()
        known = getattr(_parse, '_known_tools', set())
        if possible_tool in known:
            args = {}
            for m in re.finditer(r'(\w+)=["\']?([^"\']+)["\']?', rest):
                args[m.group(1)] = m.group(2)
            print(f"  [!] Salvaged non-JSON call: {possible_tool}({args})")
            return {"name": possible_tool, "arguments": args}

    return None

def _approve(name, args):
    if CONFIRM == "off":
        return True
    print(f"\n  [APPROVAL] {name}  {json.dumps(args)}")
    return input("  [y]es / [s]kip > ").strip().lower() in ("", "y", "yes")

def _tool_result_message(name, result, original_question):
    text = str(result)
    
    # Detect tool failure / empty output
    failure_indicators = [
        "command not found", "not found", "no such file", 
        "error", "failed", "exit status", "permission denied",
        "cannot", "unable to", "does not exist", "timeout after"
    ]
    is_failure = any(ind in text.lower() for ind in failure_indicators) or len(text.strip()) < 50
    
    if is_failure:
        return {"role": "user", "content": (
            f"=== RESULT from {name} ===\n"
            f"{text}\n"
            f"=== END ===\n\n"
            f"CRITICAL: The tool FAILED or produced no usable output. "
            f"Do NOT invent findings, CVEs, or vulnerabilities. "
            f"State clearly that the tool failed and explain why. "
            f"Answer: \"{original_question}\""
        )}
    
    # For very long results, save to file and reference it
    if len(text) > 15000:
        report_path = f"/tmp/tool_result_{name}_{int(time.time())}.txt"
        try:
            with open(report_path, "w") as f:
                f.write(text)
            summary = text[:3000] + f"\n\n[... {len(text)} chars total. Full result saved to {report_path} ...]"
        except Exception:
            summary = text[:15000] + f"\n\n[...truncated: {len(text)} chars total...]"
    else:
        summary = text
    
    return {"role": "user", "content": (
        f"=== RESULT from {name} ===\n"
        f"{summary}\n"
        f"=== END ===\n\n"
        f"INSTRUCTION: Summarize the relevant findings above in 2-4 sentences and STOP. "
        f"Cite source URLs. If specific CVEs appear verbatim in the result, cite them. "
        f"If NO specific CVEs appear, summarize general findings only. "
        f"Do NOT fabricate CVEs or findings that are not in the result above. "
        f"Answer: \"{original_question}\""
    )}

# ══════════════════════════════════════════════════════════════════════
# GROUNDING CHECK — deterministic, no extra model call. Catches the model
# naming a specific vulnerability category (XSS, SQLi, CSRF, ...) in its
# final answer that doesn't appear anywhere in the actual tool output from
# this turn. Doesn't rewrite or hide anything (human approval gate is the
# real control per PROJECT_README) — it flags a warning so the operator
# doesn't act on a fabricated finding without noticing.
#
# Known limitation: substring matching means a genuine finding phrased
# differently than the tool's own wording (e.g. model says "sqli", tool
# output only literally contains "SQL Injection") can trigger a false
# warning. Deliberately biased toward over-warning rather than missing a
# real fabrication — a spurious warning costs a glance, a missed
# fabrication costs acting on a fake finding.
# ══════════════════════════════════════════════════════════════════════
SECURITY_FINDING_CATEGORIES = [
    "xss", "cross-site scripting", "sql injection", "sqli",
    "csrf", "cross-site request forgery", "path traversal",
    "directory traversal", "crlf injection", "rce",
    "remote code execution", "command injection", "xxe",
    "xml external entity", "ssrf", "server-side request forgery",
    "idor", "insecure direct object reference", "lfi",
    "local file inclusion", "rfi", "remote file inclusion",
    "open redirect", "deserialization", "privilege escalation",
    "authentication bypass", "buffer overflow",
]

def _verify_grounding(final_text, tool_outputs):
    if not tool_outputs or not final_text:
        return final_text
    combined_output = " ".join(tool_outputs).lower()
    final_lower = final_text.lower()
    unverified = sorted({
        term for term in SECURITY_FINDING_CATEGORIES
        if term in final_lower and term not in combined_output
    })
    if not unverified:
        return final_text
    warning = (
        "⚠️  UNVERIFIED CLAIM WARNING: this response mentions "
        f"{', '.join(unverified)} — none of these terms appear in the actual "
        "tool output from this turn. This may be fabricated. Verify manually "
        "before acting on it.\n\n"
    )
    return warning + final_text

# ══════════════════════════════════════════════════════════════════════
# PREMATURE FILE READ GUARD — catches the model trying to `cat`/`head`/
# `tail` a results file (e.g. /tmp/nmap_scan_results.txt) it never actually
# created in this conversation. Observed repeatedly with the exact same
# path across unrelated fresh threads — almost certainly a stock example
# memorized during fine-tuning, not something reachable via live context,
# so a prompt instruction alone won't reliably stop it. This blocks it
# structurally: the read simply never executes unless something in this
# turn's own history already wrote or referenced that path.
# ══════════════════════════════════════════════════════════════════════
_FILE_READ_CMD_RE = re.compile(r'^\s*(?:cat|head|tail|less|more)\s+(\S+)')

def _premature_file_read(name, args, tool_outputs, messages):
    if name not in ("bash", "read_file"):
        return None
    command = args.get("command", "") if name == "bash" else args.get("path", "")
    m = _FILE_READ_CMD_RE.match(command) if name == "bash" else None
    path = m.group(1) if m else (command if name == "read_file" else None)
    if not path:
        return None
    seen_text = " ".join(tool_outputs) + " " + " ".join(
        str(msg.get("content", "")) for msg in messages if isinstance(msg, dict)
    )
    if path in seen_text:
        return None  # this path was mentioned somewhere earlier this turn — plausible it's real
    return (
        f"'{path}' has not been created anywhere in this conversation. "
        f"There is no prior step that would have produced it. Run the actual "
        f"scan/command (via bash) that would generate this output instead of "
        f"reading a file that doesn't exist yet."
    )

class Agent:
    def __init__(self):
        self.short = ShortTermMemory(max_messages=24)
        self.mcp = MCPClient()
        self.refresh_tools(announce=True)

    def refresh_tools(self, announce=False):
        was_connected = self.mcp.burp_connected
        self.tools = self.mcp.list_tools()
        self.whitelist = build_whitelist(self.tools)
        _parse._known_tools = set(self.tools.keys())
        for name in self.tools:
            if self.mcp.is_burp_tool(name):
                NEEDS_APPROVAL.add(name)
        core_n = sum(1 for n in self.tools if not self.mcp.is_burp_tool(n))
        burp_n = sum(1 for n in self.tools if self.mcp.is_burp_tool(n))
        if announce:
            print(f"[MCP] connected — {core_n} core tools" +
                  (f" + {burp_n} Burp tools" if self.mcp.burp_connected
                   else f"  (Burp not connected yet: {self.mcp.burp_error or 'start Burp with MCP extension enabled'} )"))
            print(f"      tools: {', '.join(self.tools)}")
        elif self.mcp.burp_connected and not was_connected:
            print(f"\n[MCP] Burp just connected — +{burp_n} Burp tools")
        return self.tools

    def chat(self, user_input, history=None, pending_decision=None):
        """history: optional list of {"role", "content"} dicts for THIS thread
        specifically (see docstring above on cross-thread bleed).
        pending_decision: optional {"tool_name", "arguments", "approved"} —
        set by server.py when the browser just resolved an approval-gated
        tool call. When present, user_input is NOT appended as a new turn
        (history already ends with the original question that triggered the
        gated call) — we inject the decision's outcome and continue the
        existing step loop instead."""
        self.refresh_tools()
        today = _today()
        system = BASE_SYSTEM.replace("{today}", today) + "\n\n" + self.whitelist + "\n\n" + build_skills_list() + "\n\n" + build_memory_context(user_input) + "\n\n" + build_rag_context(user_input)
        skill_content = extract_skill_reference(user_input)
        if skill_content:
            system += f"\n\n--- SKILL ---\n{skill_content}"
            print(f"  [skill injected: {len(skill_content)} chars]")

        using_external_history = history is not None
        conv = history if using_external_history else self.short.get()

        messages = [{"role": "system", "content": system}] + conv
        if pending_decision is None:
            messages.append({"role": "user", "content": user_input})
            if not using_external_history:
                self.short.add("user", user_input)

        executed, final, last_result = 0, "", ""
        stuck, _seen_exec = 0, 0
        tool_outputs = []
        seen = set()

        if pending_decision is not None:
            name = pending_decision.get("tool_name", "")
            args = pending_decision.get("arguments", {})
            approved = bool(pending_decision.get("approved"))
            call_json = json.dumps({"name": name, "arguments": args})
            if approved and name in self.tools:
                result = _cached_search(name, args, self.mcp.call)
                executed += 1
                last_result = result
                tool_outputs.append(str(result))
                seen.add(json.dumps({"name": name, "arguments": args}, sort_keys=True))
                messages.append({"role": "assistant", "content": call_json})
                messages.append(_tool_result_message(name, result, user_input))
            else:
                messages.append({"role": "assistant", "content": call_json})
                reason = "Operator DECLINED." if name in self.tools else f"'{name}' is not a valid tool."
                messages.append({"role": "user", "content": f"{reason} Suggest an alternative or stop."})
        for step in range(MAX_STEPS):
            if executed > _seen_exec:          # a tool actually ran last iteration
                _seen_exec = executed; stuck = 0
            if stuck >= STUCK_LIMIT:
                final = final or NO_PROGRESS_MSG
                break
            stuck += 1
            reply = _chat(messages)
            print(f"  [step {step+1}] {repr(reply[:180])}")

            # Detect markdown/code-fence hallucination and force retry
            if "```" in reply and _parse(reply) is None:
                print(f"  [!] Model used markdown instead of raw JSON. Nudging.")
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": (
                    "You used a markdown code block. When calling tools, output ONLY raw JSON with NO markdown, NO ```, and NO explanations. "
                    'Example: {"name": "zap_scan", "arguments": {"target": "https://events.rit-services.in"}}'
                )})
                continue

            call = _parse(reply)

            if call is None:
                final = _verify_grounding(reply, tool_outputs)
                if not using_external_history:
                    self.short.add("assistant", final)
                break

            key = json.dumps(call, sort_keys=True)
            if key in seen:
                print(f"  [!] repeated call {call.get('name')} — nudging to text")
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": (
                    f"You already called {call.get('name')} with these exact "
                    f"arguments and already have the real result above (see the "
                    f'"=== RESULT from {call.get("name")} ===" block). '
                    f"Do NOT call this tool again. Do NOT repeat the operator's "
                    f"question back to them. Instead, read the actual result "
                    f"above and write a real summary of what it found, in your "
                    f"own words."
                )})
                continue
                
            seen.add(key)

            name, args = call.get("name", ""), call.get("arguments", {})
            if name not in self.tools:
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"'{name}' is not a valid tool. Valid: {list(self.tools)}"})
                continue

            if name == "write_report" and executed == 0:
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": (
                    "No tool has run yet this turn, so there are no real results to "
                    "report. Do NOT invent findings, IPs, hostnames, or CVEs. Run the "
                    "actual scan/command via bash first, or say plainly in text that "
                    "no scan has been performed."
                )})
                continue

            must_ask = CONFIRM != "off" and (CONFIRM == "all" or name == "bash" or (CONFIRM == "chained" and executed >= 1))
            if must_ask and name in NEEDS_APPROVAL:
                if using_external_history:
                    # UI path: input() would block the server's terminal, not
                    # the browser, and freeze the whole event loop. Surface a
                    # structured request instead — server.py turns this into
                    # a tool-call the frontend renders as an approve/deny
                    # card. The turn ends here; the browser resumes it later
                    # via pending_decision once the operator responds.
                    return {"__approval_request__": True, "tool_name": name, "arguments": args}
                if not _approve(name, args):
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "Operator DECLINED. Suggest an alternative or stop."})
                    continue

            blocked = _premature_file_read(name, args, tool_outputs, messages)
            if blocked:
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": blocked})
                continue

            result = _cached_search(name, args, self.mcp.call)
            executed += 1
            last_result = result
            tool_outputs.append(str(result))
            
            # FULL DISPLAY — no truncation
            result_str = str(result)
            if len(result_str) > 5000:
                print(f"  ┌─ {name} ({len(result_str)} chars)")
                print(result_str[:5000])
                print(f"  [... {len(result_str) - 5000} more chars ...]")
                print(f"  └─")
            else:
                print(f"  ┌─ {name}\n{result_str}\n  └─")

            messages.append({"role": "assistant", "content": reply})
            messages.append(_tool_result_message(name, result, user_input))
        else:
            final = final or "[stopped: max steps]"
        return final

    def close(self):
        self.mcp.close()

    def chat_gen(self, user_input, ocr_text="", history=None):
        self.refresh_tools()
        today = _today()
        msg = user_input if not ocr_text else f"{user_input}\n\n[OCR]:\n{ocr_text}"
        system = BASE_SYSTEM.replace("{today}", today) + "\n\n" + self.whitelist + "\n\n" + build_skills_list() + "\n\n" + build_memory_context(msg) + "\n\n" + build_rag_context(msg)
        skill_content = extract_skill_reference(msg)
        if skill_content:
            system += f"\n\n--- SKILL ---\n{skill_content}"
            print(f"  [skill injected: {len(skill_content)} chars]")

        using_external_history = history is not None
        conv = history if using_external_history else self.short.get()

        messages = [{"role": "system", "content": system}] + conv
        messages.append({"role": "user", "content": msg})
        if not using_external_history:
            self.short.add("user", msg)

        seen, executed, last_result = set(), 0, ""
        stuck, _seen_exec = 0, 0
        tool_outputs = []
        for step in range(MAX_STEPS):
            if executed > _seen_exec:          # a tool actually ran last iteration
                _seen_exec = executed; stuck = 0
            if stuck >= STUCK_LIMIT:
                yield ("final", NO_PROGRESS_MSG); return
            stuck += 1
            reply = _chat(messages)
            call = _parse(reply)

            if call is None:
                final = _verify_grounding(reply, tool_outputs)
                if not using_external_history:
                    self.short.add("assistant", final)
                yield ("final", final); return

            key = json.dumps(call, sort_keys=True)
            if key in seen:
                print(f"  [!] repeated call {call.get('name')} — nudging to text")
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": (
                    f"You already called {call.get('name')} with these args. "
                    f"The result is above. Answer in plain text now."
                )})
                continue
            seen.add(key)

            name, args = call.get("name", ""), call.get("arguments", {})
            if name not in self.tools:
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"'{name}' is not a valid tool. Valid: {list(self.tools)}"})
                continue

            if name == "write_report" and executed == 0:
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": (
                    "No tool has run yet this turn, so there are no real results to "
                    "report. Do NOT invent findings, IPs, hostnames, or CVEs. Run the "
                    "actual scan/command via bash first, or say plainly in text that "
                    "no scan has been performed."
                )})
                continue

            must_ask = CONFIRM != "off" and (CONFIRM == "all" or name == "bash" or (CONFIRM == "chained" and executed >= 1))
            if must_ask and name in NEEDS_APPROVAL:
                decision = yield ("approval", {"tool": name, "args": args})
                if decision != "approve":
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "Operator DECLINED. Suggest alternative or stop."})
                    continue

            blocked = _premature_file_read(name, args, tool_outputs, messages)
            if blocked:
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": blocked})
                continue

            result = _cached_search(name, args, self.mcp.call)
            executed += 1
            last_result = result
            tool_outputs.append(str(result))

            yield ("tool", {"tool": name, "args": args, "result": str(result)[:8000]})
            messages.append({"role": "assistant", "content": reply})
            messages.append(_tool_result_message(name, result, user_input))
        yield ("final", "[stopped: max steps]")

def extract_skill_reference(message):
    md_match = re.search(r'[\w_-]+\.md', message, re.IGNORECASE)
    if md_match:
        fname = md_match.group(0)
        path = os.path.join(SKILLS_DIR, fname)
        if os.path.exists(path):
            return open(path).read()

    skill_files = glob.glob(os.path.join(SKILLS_DIR, "*.md"))
    msg_lower = message.lower()
    for fpath in skill_files:
        stem = os.path.splitext(os.path.basename(fpath))[0].lower()
        if stem in msg_lower or stem.replace("_", " ") in msg_lower:
            return open(fpath).read()

    return None
