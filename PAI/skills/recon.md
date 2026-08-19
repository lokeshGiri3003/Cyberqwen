# Website Reconnaissance — Autonomous Methodology

**Not a tool.** Use the real tools under AVAILABLE TOOLS (web_search,
web_fetch, bash, zap_scan, etc.) per phase below. Never call a tool named
"recon" — it doesn't exist.

**Scope:** Only the target given by the operator. Never expand to
discovered subdomains/IPs without asking first. Active steps still go
through the normal approval gate — this skill doesn't skip it.

**Run phases in order, no per-step confirmation needed.** Briefly report
findings after each phase, then continue.

## Phase 1 — Passive
- web_search: target name/domain — tech stack, breach history, leaked
  configs, public subdomains.
- web_fetch: homepage, /robots.txt, /sitemap.xml.
- Note: hosting/CDN hints, subdomains found (don't touch unauthorized
  ones — list and ask).

## Phase 2 — Infrastructure
- bash: `nmap -sV -T4 --top-ports 1000 <bare host, no scheme>`
- bash: `whatweb <url>` if available, else infer stack from headers.
- Note: open ports/services, web server + version, CMS/framework, TLS
  cert SANs (may reveal more subdomains).

## Phase 3 — Application Surface
- zap_scan on the target URL (spider/baseline) — endpoints, forms,
  params.
- If Burp tools are present in the current tool list, use them for
  authenticated/multi-step flows ZAP's spider can't reach; prefer Burp's
  proxy history/site map over re-spidering from scratch.
- Note: endpoint list, params seen, auth/session mechanisms, exposed
  file types.

## Phase 4 — Content Discovery (if gobuster/ffuf/dirb available)
- Run a directory brute force with a reasonably sized wordlist (avoid
  huge lists on a live site). Flag: /admin, /.git, /.env, /backup,
  /api, /uploads, /wp-admin (if WordPress).
- Don't dump full contents of anything credential-like — flag path only.

## Final Output
One consolidated summary, grounded only in this session's tool results:
- **Infrastructure**: hosting/CDN, ports/services, TLS notes
- **Tech stack**: server, framework/CMS, versions
- **Attack surface**: endpoints, params, auth mechanisms
- **Interesting findings**: exposed paths, subdomains, anomalies
- **Suggested next step**: which follow-up makes sense — wait for the
  operator's go-ahead before starting it.
