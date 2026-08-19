#!/usr/bin/env python3
"""
web_search.py — Tavily Search API + Playwright/Trafilatura fetch.

Two tools exposed to the MCP server as logic tools:
  web_search(query, search_depth="basic", max_results=5) -> Tavily search with raw content
  web_fetch(url, max_chars=4000) -> Playwright render + Trafilatura article extraction

Env:
  TAVILY_API_KEY — required for web_search. Get free key at tavily.com (1,000 credits/month, no card)
"""
import os, re, json
from urllib.parse import quote_plus

try:
    import requests
except Exception as e:
    requests = None
    _requests_err = str(e)

try:
    from trafilatura import extract
except Exception as e:
    extract = None
    _trafilatura_err = str(e)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except Exception as e:
    sync_playwright = None
    _playwright_err = str(e)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
TAVILY_ENDPOINT = "https://api.tavily.com/search"
FETCH_TIMEOUT = int(os.environ.get("WEB_FETCH_TIMEOUT", "30"))

# ── helpers ───────────────────────────────────────────────────────────

def _env_ok():
    if not TAVILY_API_KEY:
        return False, "(error: TAVILY_API_KEY not set — get a free key at tavily.com, no card required)"
    return True, ""

def _requests_ok():
    if requests is None:
        return False, f"(error: requests module missing — pip install requests: {_requests_err})"
    return True, ""

def _playwright_ok():
    if sync_playwright is None:
        return False, f"(error: playwright module missing — pip install playwright: {_playwright_err})"
    return True, ""

def _trafilatura_ok():
    if extract is None:
        return False, f"(error: trafilatura module missing — pip install trafilatura: {_trafilatura_err})"
    return True, ""

# ── web_search ──────────────────────────────────────────────────────────

def web_search(query: str, search_depth: str = "basic", max_results: int = 5) -> str:
    """Search the web via Tavily API. Returns titles/URLs/snippets and optionally raw content."""
    ok, msg = _requests_ok()
    if not ok:
        return msg
    ok, msg = _env_ok()
    if not ok:
        return msg

    max_results = max(1, min(int(max_results), 10))
    if search_depth not in ("basic", "advanced"):
        search_depth = "basic"

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": True,
        "include_raw_content": True,
    }

    try:
        r = requests.post(TAVILY_ENDPOINT, json=payload, timeout=30)
    except Exception as e:
        return f"(error: Tavily API request failed: {e})"

    if r.status_code == 401:
        return "(error: Tavily API returned 401 — invalid TAVILY_API_KEY)"
    if r.status_code == 429:
        return "(error: Tavily API rate limit hit — free tier is 1,000 credits/month)"
    if r.status_code != 200:
        return f"(error: Tavily API HTTP {r.status_code}: {r.text[:200]})"

    try:
        data = r.json()
    except Exception as e:
        return f"(error: Tavily API returned invalid JSON: {e})"

    answer = data.get("answer", "")
    results = data.get("results", [])
    if not results and not answer:
        return "(no results)"

    lines = [f"Tavily search results for: {query}", "=" * 50]
    if answer:
        lines.append(f"\n[AI Answer] {answer}\n")

    for i, item in enumerate(results, 1):
        title = item.get("title", "(no title)")
        url   = item.get("url", "(no url)")
        score = item.get("score", 0)
        content = item.get("content", "(no snippet)")
        lines.append(f"\n[{i}] {title}  (relevance: {score:.2f})")
        lines.append(f"    URL: {url}")
        lines.append(f"    {content[:300]}{'...' if len(content) > 300 else ''}")
        raw = item.get("raw_content", "")
        if raw:
            lines.append(f"    [Raw content: {len(raw)} chars]")
    return "\n".join(lines)


# ── web_fetch ───────────────────────────────────────────────────────────

def web_fetch(url: str, max_chars: int = 4000) -> str:
    """Fetch a web page with Playwright, extract article text with Trafilatura."""
    ok, msg = _playwright_ok()
    if not ok:
        return msg
    ok, msg = _trafilatura_ok()
    if not ok:
        return msg

    max_chars = int(max_chars)
    html = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=FETCH_TIMEOUT * 1000)
            html = page.content()
            browser.close()
    except PWTimeout:
        return f"(error: page load timed out after {FETCH_TIMEOUT}s — {url})"
    except Exception as e:
        return f"(error: Playwright failed to fetch {url}: {e})"

    if not html:
        return "(error: page returned empty content)"

    try:
        text = extract(html, include_comments=False, include_tables=False,
                       include_images=False, include_links=False,
                       deduplicate=True, target_language="en")
    except Exception as e:
        return f"(error: Trafilatura extraction failed: {e})"

    if not text or not text.strip():
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()

    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[...truncated at {max_chars} chars]"

    header = f"Fetched: {url}\n{'=' * 50}\n"
    return header + text


# ── sanity check ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if not TAVILY_API_KEY:
        print("Set TAVILY_API_KEY first. Get one free at tavily.com (no card required)")
        sys.exit(1)

    print("=== web_search test ===")
    print(web_search("latest CVE 2026", max_results=3))
    print("\n=== web_fetch test ===")
    print(web_fetch("https://en.wikipedia.org/wiki/Model_Context_Protocol", max_chars=2000))
